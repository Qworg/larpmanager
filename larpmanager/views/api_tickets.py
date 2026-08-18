# LarpManager - https://larpmanager.com
# Copyright (C) 2025 Scanagatta Mauro
#
# This file is part of LarpManager and is dual-licensed:
#
# 1. Under the terms of the GNU Affero General Public License (AGPL) version 3,
#    as published by the Free Software Foundation. You may use, modify, and
#    distribute this file under those terms.
#
# 2. Under a commercial license, allowing use in closed-source or proprietary
#    environments without the obligations of the AGPL.
#
# If you have obtained this file under the AGPL, and you make it available over
# a network, you must also make the complete source code available under the same license.
#
# For more information or to purchase a commercial license, contact:
# commercial@larpmanager.com
#
# SPDX-License-Identifier: AGPL-3.0-or-later OR Proprietary
"""Ticket API endpoints for Discord bot integration.

This module provides REST API endpoints for:
- Creating, reading, updating tickets
- Listing tickets with filters
- Closing and reopening tickets
- Listing associations for ticket creation dropdown
"""

from __future__ import annotations

import json
import logging
from typing import Any

from django.conf import settings
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Case, F, Prefetch, Q, Subquery, TextField, Value, When
from django.db.models.functions import Cast, Coalesce, Concat, Greatest
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods
from django_ratelimit import ALL
from django_ratelimit.core import get_usage
from django_ratelimit.decorators import ratelimit

from larpmanager.models.association import Association
from larpmanager.models.larpmanager import LarpManagerTicket, TicketPriority, TicketStatus, render_transcript_line
from larpmanager.models.ticket_event import TicketEvent
from larpmanager.models.ticket_message import TicketMessage
from larpmanager.utils.publication.api import log_api_access
from larpmanager.utils.ticket_events import emit_ticket_event
from larpmanager.views.api_discord import TicketAuth, get_member_by_discord_id, validate_ticket_api_key

logger = logging.getLogger(__name__)

# Sane field-length caps for API mutations (mirror the model constraints).
MAX_SUBJECT_LENGTH = 255
MAX_CONTENT_LENGTH = 5000
MAX_EMAIL_LENGTH = 254
MAX_TRANSCRIPT_LENGTH = 1_000_000
MAX_MESSAGE_CONTENT_LENGTH = 2000
MAX_MESSAGE_ATTACHMENTS = 10
MAX_AUTHOR_NAME_LENGTH = 255
MAX_ACK_IDS = 1000
MAX_IDEMPOTENCY_KEY_LENGTH = 255


def _rate_limited_response(request: HttpRequest, view_func: Any, rate: str) -> JsonResponse:
    """Build a 429 response with a Retry-After header for a rate-limited request."""
    usages = [
        get_usage(request=request, fn=view_func, key="ip", rate=rate, method=ALL, increment=False),
        get_usage(request=request, fn=view_func, key="header:x-api-key", rate=rate, method=ALL, increment=False),
    ]
    time_lefts = [usage["time_left"] for usage in usages if usage and usage.get("time_left") is not None]
    retry_after = max(1, *time_lefts) if time_lefts else 60

    # The auth-time log already recorded a provisional 200; record the real 429.
    auth = getattr(request, "_ticket_auth", None)
    log_api_access(auth.api_key if auth else None, request, 429, action=f"{request.method} {request.path}")

    response = JsonResponse({"error": "rate limited", "retry_after": retry_after}, status=429)
    response["Retry-After"] = str(retry_after)
    return response


def _scoped_queryset(queryset: Any, auth: TicketAuth | None) -> Any:
    """Restrict a ticket queryset to the auth's association when bound."""
    if auth is not None and auth.association is not None:
        queryset = queryset.filter(association=auth.association)
    return queryset


def _association_mismatch(auth: TicketAuth | None, ticket: LarpManagerTicket) -> bool:
    """Return True when the auth is association-bound and the ticket belongs elsewhere."""
    if auth is None or auth.association is None:
        return False
    return ticket.association_id != auth.association.id


def _length_error(field: str, value: Any, max_length: int) -> JsonResponse | None:
    """Return a 400 response when a value exceeds its maximum length."""
    if value is not None and len(str(value)) > max_length:
        return JsonResponse({"error": f"{field} exceeds maximum length of {max_length}"}, status=400)
    return None


def _parse_bool(value: Any) -> bool | None:
    """Parse a JSON boolean or a "true"/"false" string; return None when invalid."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return None


def _event_source(auth: TicketAuth | None) -> str:
    """Derive the TicketEvent source from the authenticating key (D10)."""
    return "discord" if auth is not None and auth.is_bot_key else "api"


def _actor_discord_id(ticket: LarpManagerTicket) -> int | None:
    """Resolve the acting actor for key-authenticated mutations.

    Key-based API auth carries no authenticated member, so the ticket's
    responsible staff (``assigned_staff_discord_id``) is recorded as the actor.
    """
    return ticket.assigned_staff_discord_id


def _require_bot_key(auth: TicketAuth | None) -> JsonResponse | None:
    """Return a 403 response unless the request authenticated with a bot key."""
    if auth is None or not auth.is_bot_key:
        return JsonResponse({"error": "bot key required"}, status=403)
    return None


def event_to_dict(event: TicketEvent) -> dict[str, Any]:
    """Serialize a TicketEvent for the outbox and history endpoints."""
    return {
        "id": event.id,
        "ticket_uuid": str(event.ticket.uuid),
        "event_type": event.event_type,
        "source": event.source,
        "from_status": event.from_status,
        "to_status": event.to_status,
        "actor": event.actor_display(),
        "actor_discord_id": event.actor_discord_id,
        "actor_member_uuid": str(event.actor_member.uuid) if event.actor_member else None,
        "payload": event.payload,
        "created": event.created.isoformat() if event.created else None,
        "applied_at": event.applied_at.isoformat() if event.applied_at else None,
        "acked_at": event.acked_at.isoformat() if event.acked_at else None,
        "attempts": event.attempts,
        "last_error": event.last_error,
    }


def message_to_dict(message: TicketMessage) -> dict[str, Any]:
    """Serialize a TicketMessage for the outbound endpoint response."""
    return {
        "uuid": str(message.uuid),
        "ticket_uuid": str(message.ticket.uuid),
        "discord_message_id": message.discord_message_id,
        "author_discord_id": message.author_discord_id,
        "author_name": message.author_name,
        "content": message.content,
        "attachments": message.attachments,
        "is_bot": message.is_bot,
        "sent_at": message.sent_at.isoformat() if message.sent_at else None,
    }


def _upsert_ticket_message(discord_message_id: int, defaults: dict[str, Any]) -> tuple[TicketMessage, bool]:
    """Atomically upsert a TicketMessage on discord_message_id (conflict = update).

    Returns the persisted message and whether it was newly created.
    """
    try:
        with transaction.atomic():
            message, created = TicketMessage.objects.get_or_create(
                discord_message_id=discord_message_id,
                defaults=defaults,
            )
            if not created:
                for field, value in defaults.items():
                    setattr(message, field, value)
                message.save()
    except IntegrityError:
        # A concurrent insert won the race; fetch and update the committed row.
        with transaction.atomic():
            message = TicketMessage.objects.get(discord_message_id=discord_message_id)
            for field, value in defaults.items():
                setattr(message, field, value)
            message.save()
        created = False
    return message, created


def ticket_to_dict(ticket: LarpManagerTicket, auth: TicketAuth | None = None) -> dict[str, Any]:
    """Convert a ticket model to a dictionary for JSON response.

    Args:
        ticket: The ticket model instance
        auth: Authentication result; controls whether the email field is exposed

    Returns:
        Dictionary representation of the ticket

    """
    data: dict[str, Any] = {
        "uuid": str(ticket.uuid),
        "subject": ticket.subject,
        "reason": ticket.reason,
        "content": ticket.content,
        "status": ticket.status,
        "priority": ticket.priority,
        "version": ticket.version,
        "discord_channel_id": ticket.discord_channel_id,
        "last_synced_message_id": ticket.last_synced_message_id,
        "discord_creator_id": ticket.discord_creator_id,
        "assigned_staff_discord_id": ticket.assigned_staff_discord_id,
        "association": {
            "uuid": str(ticket.association.uuid),
            "name": ticket.association.name,
            "slug": ticket.association.slug,
        }
        if ticket.association
        else None,
        "member": {
            "uuid": str(ticket.member.uuid),
            "name": ticket.member.display_member(),
        }
        if ticket.member
        else None,
        "transcript": ticket.build_transcript(),
        "created_at": ticket.created.isoformat() if ticket.created else None,
        "updated_at": ticket.updated.isoformat() if ticket.updated else None,
        "closed_at": ticket.closed_at.isoformat() if ticket.closed_at else None,
    }
    if auth is not None and (auth.is_bot_key or auth.has_scope("members:read")):
        data["email"] = ticket.email
    return data


@require_GET
@ratelimit(key="ip", rate="120/m", method="GET", block=False)
@ratelimit(key="header:x-api-key", rate="120/m", method="GET", block=False)
def list_associations(request: HttpRequest) -> JsonResponse:
    """List all active associations for ticket creation dropdown.

    Args:
        request: HTTP request object

    Returns:
        JSON response with list of associations

    """
    _auth, error_response = validate_ticket_api_key(request, required_scope="tickets:read")
    if error_response is not None:
        return error_response
    if getattr(request, "limited", False):
        return _rate_limited_response(request, list_associations, "120/m")

    associations = Association.objects.filter(deleted__isnull=True).order_by("name")

    return JsonResponse(
        {
            "associations": [
                {
                    "uuid": str(assoc.uuid),
                    "name": assoc.name,
                    "slug": assoc.slug,
                }
                for assoc in associations
            ]
        }
    )


@csrf_exempt
@require_http_methods(["GET", "POST"])
@ratelimit(key="ip", rate="120/m", method="GET", block=False)
@ratelimit(key="header:x-api-key", rate="120/m", method="GET", block=False)
@ratelimit(key="ip", rate="5/m", method="POST", block=False)
@ratelimit(key="header:x-api-key", rate="5/m", method="POST", block=False)
def tickets_list_create(request: HttpRequest) -> JsonResponse:  # noqa: C901, PLR0911, PLR0912, PLR0915
    """List tickets or create a new ticket.

    GET: List tickets with optional filters
        - status: Filter by status (open, working, done)
        - association_uuid: Filter by association
        - discord_creator_id: Filter by creator's Discord ID
        - q: Full-text search over ticket fields and message contents
        - limit: Max results (default 50)
        - offset: Pagination offset (default 0)

    POST: Create a new ticket
        Required: association_uuid, discord_creator_id
        Optional: discord_channel_id, subject, content, email, priority,
            client_uuid (idempotency key; the Idempotency-Key header wins)

    Args:
        request: HTTP request object

    Returns:
        JSON response with ticket(s)

    """
    required_scope = "tickets:write" if request.method == "POST" else "tickets:read"
    auth, error_response = validate_ticket_api_key(request, required_scope=required_scope)
    if error_response is not None:
        return error_response
    if getattr(request, "limited", False):
        rate = "5/m" if request.method == "POST" else "120/m"
        return _rate_limited_response(request, tickets_list_create, rate)

    if request.method == "GET":
        queryset = _scoped_queryset(
            LarpManagerTicket.objects.filter(deleted__isnull=True)
            .select_related("association", "member")
            .prefetch_related(
                Prefetch("messages", queryset=TicketMessage.objects.order_by(Coalesce("sent_at", "created"), "id"))
            )
            .order_by("-created"),
            auth,
        )

        status = request.GET.get("status")
        if status:
            queryset = queryset.filter(status=status)

        association_uuid = request.GET.get("association_uuid")
        if association_uuid:
            queryset = queryset.filter(association__uuid=association_uuid)

        discord_creator_id = request.GET.get("discord_creator_id")
        if discord_creator_id:
            try:
                discord_creator_id = int(discord_creator_id)
            except ValueError:
                return JsonResponse({"error": "invalid discord_creator_id"}, status=400)
            queryset = queryset.filter(discord_creator_id=discord_creator_id)

        discord_channel_id = request.GET.get("discord_channel_id")
        if discord_channel_id:
            try:
                discord_channel_id = int(discord_channel_id)
            except ValueError:
                return JsonResponse({"error": "invalid discord_channel_id"}, status=400)
            queryset = queryset.filter(discord_channel_id=discord_channel_id)

        discord_only = request.GET.get("discord_only", "false").lower() == "true"
        if discord_only:
            queryset = queryset.filter(discord_channel_id__isnull=False)

        query = request.GET.get("q", "").strip()
        if query:
            search_query = SearchQuery(query, config="english")
            message_ticket_ids = TicketMessage.objects.filter(search_vector=search_query).values("ticket_id")
            queryset = (
                queryset.annotate(rank=Coalesce(SearchRank(F("search_vector"), search_query), Value(0.0)))
                .filter(Q(search_vector=search_query) | Q(id__in=Subquery(message_ticket_ids)))
                .order_by("-rank", "-created")
            )

        try:
            limit = min(int(request.GET.get("limit", 50)), 100)
            offset = int(request.GET.get("offset", 0))
        except ValueError:
            return JsonResponse({"error": "invalid pagination"}, status=400)
        if limit < 0:
            return JsonResponse({"error": "invalid limit"}, status=400)
        if offset < 0:
            return JsonResponse({"error": "invalid offset"}, status=400)

        total = queryset.count()
        tickets = queryset[offset : offset + limit]

        return JsonResponse(
            {
                "tickets": [ticket_to_dict(ticket, auth) for ticket in tickets],
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    # POST: create a new ticket
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    association_uuid = data.get("association_uuid")
    discord_creator_id = data.get("discord_creator_id")
    discord_channel_id = data.get("discord_channel_id")

    if not all([association_uuid, discord_creator_id]):
        return JsonResponse({"error": "Missing required fields: association_uuid, discord_creator_id"}, status=400)

    # Idempotency key: the Idempotency-Key header wins over client_uuid (7a).
    idempotency_key = request.headers.get("Idempotency-Key") or data.get("client_uuid")
    if idempotency_key is not None and not isinstance(idempotency_key, str):
        return JsonResponse({"error": "idempotency key must be a string"}, status=400)
    error = _length_error("idempotency_key", idempotency_key, MAX_IDEMPOTENCY_KEY_LENGTH)
    if error is not None:
        return error

    for field, value, max_length in [
        ("subject", data.get("subject"), MAX_SUBJECT_LENGTH),
        ("content", data.get("content"), MAX_CONTENT_LENGTH),
        ("email", data.get("email"), MAX_EMAIL_LENGTH),
    ]:
        error = _length_error(field, value, max_length)
        if error is not None:
            return error

    try:
        discord_creator_id_int = int(discord_creator_id)
    except (ValueError, TypeError):
        return JsonResponse({"error": "invalid discord_creator_id"}, status=400)

    discord_channel_id_int = None
    if discord_channel_id is not None and discord_channel_id != "":
        try:
            discord_channel_id_int = int(discord_channel_id)
        except (ValueError, TypeError):
            return JsonResponse({"error": "invalid discord_channel_id"}, status=400)

    priority = data.get("priority", TicketPriority.LOW)
    if priority not in TicketPriority.values:
        return JsonResponse({"error": f"Invalid priority: {priority}"}, status=400)

    try:
        association = Association.objects.get(uuid=association_uuid, deleted__isnull=True)
    except Association.DoesNotExist:
        return JsonResponse({"error": "Association not found"}, status=404)

    if auth.association is not None and association.pk != auth.association.pk:
        return JsonResponse({"error": "Forbidden"}, status=403)

    # Idempotent create (7a): a known idempotency key returns the existing ticket.
    if idempotency_key:
        existing_ticket = LarpManagerTicket.objects.filter(
            idempotency_key=idempotency_key,
            deleted__isnull=True,
        ).first()
        if existing_ticket is not None:
            return JsonResponse({"ticket": ticket_to_dict(existing_ticket, auth)})

    # Idempotent create: return an existing live ticket for the same channel.
    if discord_channel_id_int is not None:
        existing_ticket = LarpManagerTicket.objects.filter(
            discord_channel_id=discord_channel_id_int,
            association=association,
            deleted__isnull=True,
        ).first()
        if existing_ticket is not None:
            return JsonResponse({"ticket": ticket_to_dict(existing_ticket, auth)})

    max_open_tickets = getattr(settings, "MAX_OPEN_TICKETS_PER_CREATOR", 5)
    open_ticket_count = (
        LarpManagerTicket.objects.filter(
            discord_creator_id=discord_creator_id_int,
            deleted__isnull=True,
        )
        .exclude(status=TicketStatus.DONE)
        .count()
    )
    if open_ticket_count >= max_open_tickets:
        return JsonResponse({"error": "ticket cap exceeded", "max_open_tickets": max_open_tickets}, status=400)

    member = get_member_by_discord_id(discord_creator_id_int)

    try:
        with transaction.atomic():
            ticket = LarpManagerTicket.objects.create(
                association=association,
                member=member,
                discord_channel_id=discord_channel_id_int,
                discord_creator_id=discord_creator_id_int,
                idempotency_key=idempotency_key or None,
                subject=data.get("subject", ""),
                reason=data.get("reason", "Discord Support"),
                content=data.get("content", ""),
                email=data.get("email", member.email if member else None),
                priority=priority,
                status=TicketStatus.OPEN,
            )
            emit_ticket_event(
                ticket,
                TicketEvent.EventType.CREATED,
                source=_event_source(auth),
                actor_discord_id=discord_creator_id_int,
                actor_member=member,
                payload={
                    "ticket_uuid": str(ticket.uuid),
                    "subject": ticket.subject,
                    "association_uuid": str(association.uuid),
                    "discord_creator_id": discord_creator_id_int,
                },
            )
            # Without a channel id the bot must create the Discord channel and
            # write it back via POST /channel/; the caller polls until populated.
            if discord_channel_id_int is None:
                emit_ticket_event(
                    ticket,
                    TicketEvent.EventType.CHANNEL_CREATE,
                    source=TicketEvent.Source.API,
                    payload={
                        "ticket_uuid": str(ticket.uuid),
                        "subject": ticket.subject,
                        "association_uuid": str(association.uuid),
                        "discord_creator_id": discord_creator_id_int,
                    },
                )
    except IntegrityError:
        # A concurrent create with the same idempotency key or channel won the
        # race; return the committed ticket rather than a 409.
        if idempotency_key:
            existing_ticket = LarpManagerTicket.objects.filter(idempotency_key=idempotency_key).first()
            if existing_ticket is not None:
                return JsonResponse({"ticket": ticket_to_dict(existing_ticket, auth)})
        if discord_channel_id_int is not None:
            existing_ticket = LarpManagerTicket.objects.filter(
                discord_channel_id=discord_channel_id_int, association=association
            ).first()
            if existing_ticket is not None:
                return JsonResponse({"ticket": ticket_to_dict(existing_ticket, auth)})
        return JsonResponse({"error": "ticket already exists"}, status=409)
    logger.info("Created ticket %s via Discord API", ticket.uuid)

    return JsonResponse({"ticket": ticket_to_dict(ticket, auth)}, status=201)


@csrf_exempt
@require_http_methods(["GET", "PATCH", "DELETE"])
@ratelimit(key="ip", rate="120/m", method="GET", block=False)
@ratelimit(key="header:x-api-key", rate="120/m", method="GET", block=False)
@ratelimit(key="ip", rate="30/m", method=["PATCH", "DELETE"], block=False)
@ratelimit(key="header:x-api-key", rate="30/m", method=["PATCH", "DELETE"], block=False)
def ticket_detail(request: HttpRequest, ticket_uuid: str) -> JsonResponse:  # noqa: C901, PLR0911, PLR0912, PLR0915
    """Get, update, or delete a specific ticket.

    GET: Get ticket details
    PATCH: Update ticket fields (status, priority, assignment, subject, content)
    DELETE: Soft delete the ticket

    Args:
        request: HTTP request object
        ticket_uuid: UUID of the ticket

    Returns:
        JSON response with ticket details or success message

    """
    required_scope = "tickets:read" if request.method == "GET" else "tickets:write"
    auth, error_response = validate_ticket_api_key(request, required_scope=required_scope)
    if error_response is not None:
        return error_response
    if getattr(request, "limited", False):
        rate = "120/m" if request.method == "GET" else "30/m"
        return _rate_limited_response(request, ticket_detail, rate)

    if request.method == "GET":
        queryset = _scoped_queryset(
            LarpManagerTicket.objects.filter(deleted__isnull=True)
            .select_related("association", "member")
            .prefetch_related(
                Prefetch("messages", queryset=TicketMessage.objects.order_by(Coalesce("sent_at", "created"), "id"))
            ),
            auth,
        )
        try:
            ticket = queryset.get(uuid=ticket_uuid)
        except (LarpManagerTicket.DoesNotExist, ValidationError):
            return JsonResponse({"error": "Ticket not found"}, status=404)
        return JsonResponse({"ticket": ticket_to_dict(ticket, auth)})

    try:
        ticket = LarpManagerTicket.objects.select_related("association", "member").get(
            uuid=ticket_uuid, deleted__isnull=True
        )
    except (LarpManagerTicket.DoesNotExist, ValidationError):
        return JsonResponse({"error": "Ticket not found"}, status=404)

    if _association_mismatch(auth, ticket):
        return JsonResponse({"error": "Forbidden"}, status=403)

    if request.method == "PATCH":
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON body"}, status=400)

        allowed_fields = {"status", "priority", "assigned_staff_discord_id", "subject", "content", "version"}
        read_only_fields = {"transcript", "analysis"}
        body_fields = set(data.keys())
        if body_fields & read_only_fields:
            return JsonResponse({"error": "transcript and analysis are read-only"}, status=400)
        unknown_fields = body_fields - allowed_fields
        if unknown_fields:
            return JsonResponse({"error": f"invalid fields: {', '.join(sorted(unknown_fields))}"}, status=400)

        # Optimistic lock (D8): PATCH must carry the current version.
        if "version" not in data:
            return JsonResponse({"error": "version required"}, status=400)
        try:
            base_version = int(data["version"])
        except (ValueError, TypeError):
            return JsonResponse({"error": "version must be an integer"}, status=400)
        if base_version != ticket.version:
            return JsonResponse({"error": "concurrent edit", "current_version": ticket.version}, status=409)

        for field, value, max_length in [
            ("subject", data.get("subject"), MAX_SUBJECT_LENGTH),
            ("content", data.get("content"), MAX_CONTENT_LENGTH),
        ]:
            error = _length_error(field, value, max_length)
            if error is not None:
                return error

        old_status = ticket.status
        old_priority = ticket.priority
        old_assigned_staff = ticket.assigned_staff_discord_id
        old_subject = ticket.subject

        if "status" in data:
            new_status = data["status"]
            if new_status not in TicketStatus.values:
                return JsonResponse({"error": f"Invalid status: {new_status}"}, status=400)
            ticket.status = new_status

        if "priority" in data:
            new_priority = data["priority"]
            if new_priority not in TicketPriority.values:
                return JsonResponse({"error": f"Invalid priority: {new_priority}"}, status=400)
            ticket.priority = new_priority

        if "assigned_staff_discord_id" in data:
            assigned_staff = data["assigned_staff_discord_id"]
            if assigned_staff is not None:
                try:
                    assigned_staff = int(assigned_staff)
                except (ValueError, TypeError):
                    return JsonResponse({"error": "invalid assigned_staff_discord_id"}, status=400)
            ticket.assigned_staff_discord_id = assigned_staff

        if "subject" in data:
            ticket.subject = data["subject"]

        if "content" in data:
            ticket.content = data["content"]

        source = _event_source(auth)
        new_status = data.get("status", old_status)
        new_priority = data.get("priority", old_priority)
        assigned_staff = ticket.assigned_staff_discord_id

        with transaction.atomic():
            ticket.version = F("version") + 1
            ticket.save()

            if "status" in data and new_status != old_status:
                emit_ticket_event(
                    ticket,
                    TicketEvent.EventType.STATUS_CHANGED,
                    source=source,
                    from_status=old_status,
                    to_status=new_status,
                    actor_discord_id=_actor_discord_id(ticket),
                    payload={"to_status": new_status},
                )
            if "priority" in data and new_priority != old_priority:
                emit_ticket_event(
                    ticket,
                    TicketEvent.EventType.PRIORITY_CHANGED,
                    source=source,
                    actor_discord_id=_actor_discord_id(ticket),
                    payload={"to_priority": new_priority, "from_priority": old_priority},
                )
            if "assigned_staff_discord_id" in data and assigned_staff != old_assigned_staff:
                emit_ticket_event(
                    ticket,
                    TicketEvent.EventType.ASSIGNED,
                    source=source,
                    actor_discord_id=assigned_staff,
                    payload={"assigned_staff_discord_id": assigned_staff},
                )
            if "subject" in data and data["subject"] != old_subject:
                emit_ticket_event(
                    ticket,
                    TicketEvent.EventType.CHANNEL_UPDATE,
                    source=source,
                    actor_discord_id=_actor_discord_id(ticket),
                    payload={"subject": data["subject"]},
                )

        ticket.refresh_from_db(fields=["version"])
        logger.info("Updated ticket %s via Discord API", ticket.uuid)

        return JsonResponse({"ticket": ticket_to_dict(ticket, auth)})

    # DELETE: soft delete the ticket and archive its Discord channel.
    with transaction.atomic():
        ticket.deleted = timezone.now()
        ticket.version = F("version") + 1
        # keep_deleted=True prevents SafeDeleteModel.save() from resetting deleted.
        ticket.save(keep_deleted=True)
        emit_ticket_event(
            ticket,
            TicketEvent.EventType.DELETED,
            source=_event_source(auth),
            actor_discord_id=_actor_discord_id(ticket),
            payload={},
        )
        # A live channel must be archived by the bot so no orphan remains (7c).
        if ticket.discord_channel_id is not None:
            emit_ticket_event(
                ticket,
                TicketEvent.EventType.CHANNEL_ARCHIVE,
                source=TicketEvent.Source.API,
                actor_discord_id=_actor_discord_id(ticket),
                payload={"discord_channel_id": ticket.discord_channel_id},
            )
    logger.info("Deleted ticket %s via Discord API", ticket.uuid)
    return JsonResponse({"success": True})


@csrf_exempt
@require_http_methods(["POST"])
@ratelimit(key="ip", rate="30/m", method="POST", block=False)
@ratelimit(key="header:x-api-key", rate="30/m", method="POST", block=False)
def ticket_close(request: HttpRequest, ticket_uuid: str) -> JsonResponse:  # noqa: PLR0911
    """Close a ticket with optional transcript.

    POST body:
        - transcript: Optional full transcript text

    Args:
        request: HTTP request object
        ticket_uuid: UUID of the ticket

    Returns:
        JSON response with updated ticket

    """
    auth, error_response = validate_ticket_api_key(request, required_scope="tickets:write")
    if error_response is not None:
        return error_response
    if getattr(request, "limited", False):
        return _rate_limited_response(request, ticket_close, "30/m")

    try:
        ticket = LarpManagerTicket.objects.select_related("association", "member").get(
            uuid=ticket_uuid, deleted__isnull=True
        )
    except (LarpManagerTicket.DoesNotExist, ValidationError):
        return JsonResponse({"error": "Ticket not found"}, status=404)

    if _association_mismatch(auth, ticket):
        return JsonResponse({"error": "Forbidden"}, status=403)

    if request.body:
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON body"}, status=400)
    else:
        data = {}

    # Persist a close-time transcript only when the ticket has zero TicketMessage
    # rows. The scrape is backfill-only: once per-message rows exist, they are
    # authoritative and must never be overwritten (W2 close precedence). The
    # length check is inside the guard so a legacy bot's oversized transcript on
    # an already-row'd ticket is ignored rather than rejected (400).
    if "transcript" in data and not TicketMessage.objects.filter(ticket=ticket).exists():
        error = _length_error("transcript", data["transcript"], MAX_TRANSCRIPT_LENGTH)
        if error is not None:
            return error
        ticket.transcript = data["transcript"]

    # Close the ticket
    old_status = ticket.status
    ticket.status = TicketStatus.DONE
    ticket.closed_at = timezone.now()

    with transaction.atomic():
        ticket.version = F("version") + 1
        ticket.save()

        emit_ticket_event(
            ticket,
            TicketEvent.EventType.CLOSED,
            source=_event_source(auth),
            from_status=old_status,
            to_status=TicketStatus.DONE,
            actor_discord_id=_actor_discord_id(ticket),
            payload={},
        )

    ticket.refresh_from_db(fields=["version"])
    logger.info("Closed ticket %s via Discord API", ticket.uuid)

    return JsonResponse({"ticket": ticket_to_dict(ticket, auth)})


@csrf_exempt
@require_http_methods(["POST"])
@ratelimit(key="ip", rate="30/m", method="POST", block=False)
@ratelimit(key="header:x-api-key", rate="30/m", method="POST", block=False)
def ticket_reopen(request: HttpRequest, ticket_uuid: str) -> JsonResponse:
    """Reopen a closed ticket.

    Args:
        request: HTTP request object
        ticket_uuid: UUID of the ticket

    Returns:
        JSON response with updated ticket

    """
    auth, error_response = validate_ticket_api_key(request, required_scope="tickets:write")
    if error_response is not None:
        return error_response
    if getattr(request, "limited", False):
        return _rate_limited_response(request, ticket_reopen, "30/m")

    try:
        ticket = LarpManagerTicket.objects.select_related("association", "member").get(
            uuid=ticket_uuid, deleted__isnull=True
        )
    except (LarpManagerTicket.DoesNotExist, ValidationError):
        return JsonResponse({"error": "Ticket not found"}, status=404)

    if _association_mismatch(auth, ticket):
        return JsonResponse({"error": "Forbidden"}, status=403)

    if ticket.status != TicketStatus.DONE:
        return JsonResponse({"error": "Ticket is not closed, cannot reopen"}, status=400)

    # Reopen the ticket
    old_status = ticket.status
    ticket.status = TicketStatus.OPEN
    ticket.closed_at = None

    with transaction.atomic():
        ticket.version = F("version") + 1
        ticket.save()

        emit_ticket_event(
            ticket,
            TicketEvent.EventType.REOPENED,
            source=_event_source(auth),
            from_status=old_status,
            to_status=TicketStatus.OPEN,
            actor_discord_id=_actor_discord_id(ticket),
            payload={},
        )

    ticket.refresh_from_db(fields=["version"])
    logger.info("Reopened ticket %s via Discord API", ticket.uuid)

    return JsonResponse({"ticket": ticket_to_dict(ticket, auth)})


@csrf_exempt
@require_http_methods(["POST"])
@ratelimit(key="ip", rate="30/m", method="POST", block=False)
@ratelimit(key="header:x-api-key", rate="30/m", method="POST", block=False)
def ticket_channel_writeback(request: HttpRequest, ticket_uuid: str) -> JsonResponse:  # noqa: PLR0911
    """Write back the Discord channel id after the bot applies a channel_create.

    Bot-key-only. The write is done via QuerySet.update() so the optimistic-lock
    ``version`` is NOT bumped (background write, D8); a history-only marker is
    emitted so the write-back is visible in ticket history but never in the outbox.

    """
    auth, error_response = validate_ticket_api_key(request)
    if error_response is not None:
        return error_response
    bot_error = _require_bot_key(auth)
    if bot_error is not None:
        return bot_error
    if getattr(request, "limited", False):
        return _rate_limited_response(request, ticket_channel_writeback, "30/m")

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    try:
        discord_channel_id = int(data.get("discord_channel_id"))
    except (ValueError, TypeError):
        return JsonResponse({"error": "discord_channel_id must be an integer"}, status=400)

    try:
        ticket = LarpManagerTicket.objects.select_related("association", "member").get(
            uuid=ticket_uuid, deleted__isnull=True
        )
    except (LarpManagerTicket.DoesNotExist, ValidationError):
        return JsonResponse({"error": "Ticket not found"}, status=404)

    LarpManagerTicket.objects.filter(pk=ticket.pk).update(discord_channel_id=discord_channel_id)
    ticket.refresh_from_db(fields=["discord_channel_id"])

    emit_ticket_event(
        ticket,
        TicketEvent.EventType.CHANNEL_SYNCED,
        source=_event_source(auth),
        payload={"ticket_uuid": str(ticket.uuid), "discord_channel_id": discord_channel_id},
    )

    return JsonResponse({"ticket": ticket_to_dict(ticket, auth)})


@require_GET
@ratelimit(key="ip", rate="120/m", method="GET", block=False)
@ratelimit(key="header:x-api-key", rate="120/m", method="GET", block=False)
def ticket_by_channel(request: HttpRequest, channel_id: int) -> JsonResponse:
    """Get a ticket by its Discord channel ID.

    Args:
        request: HTTP request object
        channel_id: Discord channel ID

    Returns:
        JSON response with ticket details

    """
    auth, error_response = validate_ticket_api_key(request, required_scope="tickets:read")
    if error_response is not None:
        return error_response
    if getattr(request, "limited", False):
        return _rate_limited_response(request, ticket_by_channel, "120/m")

    queryset = _scoped_queryset(
        LarpManagerTicket.objects.filter(deleted__isnull=True).select_related("association", "member"),
        auth,
    )
    try:
        ticket = queryset.get(discord_channel_id=channel_id)
    except LarpManagerTicket.DoesNotExist:
        return JsonResponse({"error": "Ticket not found"}, status=404)

    return JsonResponse({"ticket": ticket_to_dict(ticket, auth)})


@require_GET
@ratelimit(key="ip", rate="120/m", method="GET", block=False)
@ratelimit(key="header:x-api-key", rate="120/m", method="GET", block=False)
def ticket_events_outbox(request: HttpRequest) -> JsonResponse:
    """Return undelivered ``source=api`` events for the bot outbox cursor.

    Query params:
        since: Accepted for backward compatibility but ignored for filtering.
        limit: Maximum number of events to return (default 100, capped at 1000).

    """
    auth, error_response = validate_ticket_api_key(request)
    if error_response is not None:
        return error_response
    bot_error = _require_bot_key(auth)
    if bot_error is not None:
        return bot_error
    if getattr(request, "limited", False):
        return _rate_limited_response(request, ticket_events_outbox, "120/m")

    try:
        since = int(request.GET.get("since", 0))
        limit = int(request.GET.get("limit", 100))
    except ValueError:
        return JsonResponse({"error": "invalid cursor"}, status=400)
    if since < 0 or limit < 0:
        return JsonResponse({"error": "invalid cursor"}, status=400)
    limit = min(limit, 1000)

    # The outbox is defined purely by the NULL predicates: an event stays in the
    # outbox until it has been both applied and acked. ``since`` is accepted for
    # backward compatibility but is deliberately NOT used to filter, because
    # ``id > since`` strands any lower-id event that was never applied (a batch
    # ack can advance the cursor past an in-flight, un-applied event).
    events = (
        TicketEvent.objects.filter(
            source=TicketEvent.Source.API,
            applied_at__isnull=True,
            acked_at__isnull=True,
        )
        .select_related("ticket", "actor_member")
        .order_by("id")[:limit]
    )
    return JsonResponse({"events": [event_to_dict(event) for event in events]})


@csrf_exempt
@require_http_methods(["POST"])
@ratelimit(key="ip", rate="120/m", method="POST", block=False)
@ratelimit(key="header:x-api-key", rate="120/m", method="POST", block=False)
def ticket_events_ack(request: HttpRequest) -> JsonResponse:  # noqa: PLR0911
    """Batch-ack outbox events (idempotent; the server writes applied_at too)."""
    auth, error_response = validate_ticket_api_key(request)
    if error_response is not None:
        return error_response
    bot_error = _require_bot_key(auth)
    if bot_error is not None:
        return bot_error
    if getattr(request, "limited", False):
        return _rate_limited_response(request, ticket_events_ack, "120/m")

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    ids = data.get("ids")
    if not isinstance(ids, list) or not all(isinstance(event_id, int) for event_id in ids):
        return JsonResponse({"error": "ids must be a list of integers"}, status=400)
    if len(ids) > MAX_ACK_IDS:
        return JsonResponse({"error": "too many ids"}, status=400)

    now = timezone.now()
    updated = TicketEvent.objects.filter(id__in=ids, source=TicketEvent.Source.API).update(
        acked_at=now,
        applied_at=now,
    )
    return JsonResponse({"acked": updated})


@require_GET
@ratelimit(key="ip", rate="120/m", method="GET", block=False)
@ratelimit(key="header:x-api-key", rate="120/m", method="GET", block=False)
def ticket_events_history(request: HttpRequest, ticket_uuid: str) -> JsonResponse:
    """Return full ticket-scoped event history (all sources, ordered by id)."""
    auth, error_response = validate_ticket_api_key(request, required_scope="tickets:read")
    if error_response is not None:
        return error_response
    if getattr(request, "limited", False):
        return _rate_limited_response(request, ticket_events_history, "120/m")

    # History must be available even for soft-deleted tickets; the default manager
    # hides soft-deleted rows, so use all_objects and drop the deleted__isnull filter.
    queryset = _scoped_queryset(LarpManagerTicket.all_objects.all(), auth)
    try:
        ticket = queryset.get(uuid=ticket_uuid)
    except (LarpManagerTicket.DoesNotExist, ValidationError):
        return JsonResponse({"error": "Ticket not found"}, status=404)

    events = TicketEvent.objects.filter(ticket=ticket).select_related("ticket", "actor_member").order_by("id")
    return JsonResponse({"events": [event_to_dict(event) for event in events]})


@require_GET
@ratelimit(key="ip", rate="120/m", method="GET", block=False)
@ratelimit(key="header:x-api-key", rate="120/m", method="GET", block=False)
def ticket_transcript(request: HttpRequest, ticket_uuid: str) -> JsonResponse:
    """Return the rendered transcript plus per-message rows for a ticket.

    Bot keys and ``tickets:read`` staff keys may access it (mirrors
    ``ticket_events_history``). Messages are returned oldest first; a NULL
    ``sent_at`` sorts by ``created`` (Coalesce).

    """
    auth, error_response = validate_ticket_api_key(request, required_scope="tickets:read")
    if error_response is not None:
        return error_response
    if getattr(request, "limited", False):
        return _rate_limited_response(request, ticket_transcript, "120/m")

    # History-style lookup so the transcript remains available for soft-deleted
    # tickets, matching ticket_events_history.
    queryset = _scoped_queryset(LarpManagerTicket.all_objects.all(), auth)
    try:
        ticket = queryset.get(uuid=ticket_uuid)
    except (LarpManagerTicket.DoesNotExist, ValidationError):
        return JsonResponse({"error": "Ticket not found"}, status=404)

    messages = TicketMessage.objects.filter(ticket=ticket).order_by(Coalesce("sent_at", "created"), "id")
    return JsonResponse(
        {
            "transcript": ticket.build_transcript(),
            "messages": [
                {
                    "discord_message_id": message.discord_message_id,
                    "author_name": message.author_name,
                    "content": message.content,
                    "sent_at": message.sent_at.isoformat() if message.sent_at else None,
                    "is_bot": message.is_bot,
                }
                for message in messages
            ],
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
@ratelimit(key="ip", rate="120/m", method="POST", block=False)
@ratelimit(key="header:x-api-key", rate="120/m", method="POST", block=False)
def ticket_outbound(request: HttpRequest) -> JsonResponse:  # noqa: C901, PLR0911, PLR0912
    """Persist a Discord-originated message (idempotent upsert on discord_message_id).

    Validation: discord_channel_id must resolve to a live ticket (404); content over
    2000 chars or more than 10 attachments are rejected (400). ``deleted: true``
    soft-deletes the matching message and acks (200) without content validation or
    the search-vector/watermark/transcript steps. The upsert is atomic on
    discord_message_id, so a conflicting message is a success (200).

    """
    auth, error_response = validate_ticket_api_key(request)
    if error_response is not None:
        return error_response
    bot_error = _require_bot_key(auth)
    if bot_error is not None:
        return bot_error
    if getattr(request, "limited", False):
        return _rate_limited_response(request, ticket_outbound, "120/m")

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    try:
        discord_channel_id = int(data.get("discord_channel_id"))
        discord_message_id = int(data.get("discord_message_id"))
    except (ValueError, TypeError):
        return JsonResponse({"error": "discord_channel_id and discord_message_id must be integers"}, status=400)

    deleted = data.get("deleted", False)
    if not isinstance(deleted, bool):
        return JsonResponse({"error": "deleted must be a boolean"}, status=400)

    ticket = LarpManagerTicket.objects.filter(discord_channel_id=discord_channel_id, deleted__isnull=True).first()
    if ticket is None:
        return JsonResponse({"error": "Ticket not found"}, status=404)

    if deleted:
        # Soft-delete the matching message (SafeDelete) and ack. The 404 check
        # above still applies; content/author validation and the snapshot,
        # search-vector, and watermark steps are intentionally skipped here.
        TicketMessage.objects.filter(ticket=ticket, discord_message_id=discord_message_id).delete()
        return JsonResponse({"deleted": True})

    content = data.get("content", "")
    if not isinstance(content, str) or len(content) > MAX_MESSAGE_CONTENT_LENGTH:
        return JsonResponse({"error": "content too long"}, status=400)

    attachments = data.get("attachments", [])
    if not isinstance(attachments, list) or len(attachments) > MAX_MESSAGE_ATTACHMENTS:
        return JsonResponse({"error": "too many attachments"}, status=400)

    author_name = data.get("author_name", "")
    if not isinstance(author_name, str) or len(author_name) > MAX_AUTHOR_NAME_LENGTH:
        return JsonResponse({"error": "author_name too long"}, status=400)

    is_bot = _parse_bool(data.get("is_bot", False))
    if is_bot is None:
        return JsonResponse({"error": "is_bot must be a boolean"}, status=400)

    sent_at_dt = None
    sent_at = data.get("sent_at")
    if sent_at:
        sent_at_dt = parse_datetime(sent_at)
        if sent_at_dt is None:
            return JsonResponse({"error": "invalid sent_at"}, status=400)

    author_discord_id = data.get("author_discord_id")
    if author_discord_id is not None:
        try:
            author_discord_id = int(author_discord_id)
        except (ValueError, TypeError):
            return JsonResponse({"error": "invalid author_discord_id"}, status=400)

    defaults = {
        "ticket": ticket,
        "author_discord_id": author_discord_id,
        "author_name": author_name,
        "content": content,
        "attachments": attachments,
        "is_bot": is_bot,
        "sent_at": sent_at_dt,
    }

    message, created = _upsert_ticket_message(discord_message_id, defaults)

    # Populate the per-message search vector (content + author_name).
    TicketMessage.objects.filter(pk=message.pk).update(
        search_vector=SearchVector(
            Cast("content", TextField()),
            Cast("author_name", TextField()),
            config="english",
        )
    )

    if created:
        # Append the rendered line to the transcript snapshot atomically. F()
        # reads the committed value under the row lock, so concurrent appends
        # never lose a line, and version is not bumped (D8). Case/When avoids
        # the leading newline the first append would otherwise prepend to a NULL
        # (or empty) transcript snapshot.
        line = render_transcript_line(message)
        LarpManagerTicket.objects.filter(pk=ticket.pk).update(
            transcript=Case(
                When(
                    Q(transcript__isnull=True) | Q(transcript=""),
                    then=Value(line, output_field=TextField()),
                ),
                default=Concat(F("transcript"), Value("\n" + line), output_field=TextField()),
            ),
        )

    # Advance the reconnect watermark without bumping version (QuerySet.update).
    LarpManagerTicket.objects.filter(pk=ticket.pk).update(
        last_synced_message_id=Greatest(
            Coalesce(F("last_synced_message_id"), Value(0)),
            Value(discord_message_id),
        )
    )

    return JsonResponse({"message": message_to_dict(message)})
