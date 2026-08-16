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
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods
from django_ratelimit import ALL
from django_ratelimit.core import get_usage
from django_ratelimit.decorators import ratelimit

from larpmanager.models.association import Association
from larpmanager.models.larpmanager import LarpManagerTicket, TicketPriority, TicketStatus
from larpmanager.utils.publication.api import log_api_access
from larpmanager.views.api_discord import TicketAuth, get_member_by_discord_id, validate_ticket_api_key

logger = logging.getLogger(__name__)

# Sane field-length caps for API mutations (mirror the model constraints).
MAX_SUBJECT_LENGTH = 255
MAX_CONTENT_LENGTH = 5000
MAX_EMAIL_LENGTH = 254
MAX_TRANSCRIPT_LENGTH = 1_000_000


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
        "discord_channel_id": ticket.discord_channel_id,
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
        "transcript": ticket.transcript,
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
        - limit: Max results (default 50)
        - offset: Pagination offset (default 0)

    POST: Create a new ticket
        Required: association_uuid, discord_creator_id, discord_channel_id
        Optional: subject, content, email, priority

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

    if not all([association_uuid, discord_creator_id, discord_channel_id]):
        return JsonResponse(
            {"error": "Missing required fields: association_uuid, discord_creator_id, discord_channel_id"}, status=400
        )

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

    # Idempotent create: return an existing live ticket for the same channel.
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
        ticket = LarpManagerTicket.objects.create(
            association=association,
            member=member,
            discord_channel_id=discord_channel_id_int,
            discord_creator_id=discord_creator_id_int,
            subject=data.get("subject", ""),
            reason=data.get("reason", "Discord Support"),
            content=data.get("content", ""),
            email=data.get("email", member.email if member else None),
            priority=priority,
            status=TicketStatus.OPEN,
        )
    except IntegrityError:
        return JsonResponse({"error": "ticket already exists for this channel"}, status=409)

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
            LarpManagerTicket.objects.filter(deleted__isnull=True).select_related("association", "member"),
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

        allowed_fields = {"status", "priority", "assigned_staff_discord_id", "subject", "content"}
        read_only_fields = {"transcript", "analysis"}
        body_fields = set(data.keys())
        if body_fields & read_only_fields:
            return JsonResponse({"error": "transcript and analysis are read-only"}, status=400)
        unknown_fields = body_fields - allowed_fields
        if unknown_fields:
            return JsonResponse({"error": f"invalid fields: {', '.join(sorted(unknown_fields))}"}, status=400)

        for field, value, max_length in [
            ("subject", data.get("subject"), MAX_SUBJECT_LENGTH),
            ("content", data.get("content"), MAX_CONTENT_LENGTH),
        ]:
            error = _length_error(field, value, max_length)
            if error is not None:
                return error

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

        ticket.save()
        logger.info("Updated ticket %s via Discord API", ticket.uuid)

        return JsonResponse({"ticket": ticket_to_dict(ticket, auth)})

    # DELETE: soft delete the ticket
    ticket.deleted = timezone.now()
    ticket.save()
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

    # Update transcript if provided
    if "transcript" in data:
        error = _length_error("transcript", data["transcript"], MAX_TRANSCRIPT_LENGTH)
        if error is not None:
            return error
        ticket.transcript = data["transcript"]

    # Close the ticket
    ticket.status = TicketStatus.DONE
    ticket.closed_at = timezone.now()
    ticket.save()

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
    ticket.status = TicketStatus.OPEN
    ticket.closed_at = None
    ticket.save()

    logger.info("Reopened ticket %s via Discord API", ticket.uuid)

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
