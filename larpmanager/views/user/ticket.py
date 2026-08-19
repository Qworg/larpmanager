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

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.conf import settings as conf_settings
from django.contrib.auth.decorators import login_required
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.db import transaction
from django.db.models import Case, Count, F, Q, Subquery, TextField, Value, When
from django.db.models.functions import Cast, Coalesce, Concat
from django.http import Http404, HttpRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from larpmanager.models.larpmanager import LarpManagerTicket, TicketPriority, TicketStatus, render_transcript_line
from larpmanager.models.ticket_event import TicketEvent
from larpmanager.models.ticket_message import TicketMessage
from larpmanager.models.ticket_transcript_link import TicketTranscriptLink
from larpmanager.utils.auth.permission import has_association_permission
from larpmanager.utils.core.base import get_context
from larpmanager.utils.publication.api import get_client_ip
from larpmanager.utils.ticket_events import emit_ticket_event

# Statuses considered "active" (not done). The staff list defaults to these.
ACTIVE_TICKET_STATUSES = (TicketStatus.OPEN, TicketStatus.WORKING)

# Shown when a staff member submits a stale form (optimistic-lock conflict, D8).
CONCURRENT_EDIT_MESSAGE = "This ticket was modified by someone else. Review the latest changes and retry."
MAX_REPLY_LENGTH = 2000

# Shareable transcript link defaults (D5): 24h TTL, capped opens.
TRANSCRIPT_LINK_TTL = timedelta(hours=24)
TRANSCRIPT_LINK_MAX_OPENS = 5

logger = logging.getLogger(__name__)


def filter_tickets_by_status(queryset: Any, status: str) -> Any:
    """Apply the ``?status=`` filter to a ticket queryset.

    ``active`` expands to open/working, ``all`` and unknown values apply no
    filter, and any other valid status filters exactly.
    """
    if status == "done":
        return queryset.filter(status=TicketStatus.DONE)
    if status == "active":
        return queryset.filter(status__in=ACTIVE_TICKET_STATUSES)
    if status in TicketStatus.values:
        return queryset.filter(status=status)
    return queryset


def filter_tickets_by_stranded(queryset: Any, stranded: str) -> Any:
    """Apply the ``?stranded=`` filter to a ticket queryset."""
    if stranded == "true":
        return queryset.filter(stranded=True)
    if stranded == "false":
        return queryset.filter(stranded=False)
    return queryset


def apply_ticket_search(queryset: Any, query: str) -> Any:
    """Apply the ``?q=`` full-text search over ticket and message vectors."""
    if not query:
        return queryset.order_by("-created")
    search_query = SearchQuery(query, config="english")
    message_ticket_ids = TicketMessage.objects.filter(search_vector=search_query).values("ticket_id")
    return (
        queryset.annotate(rank=Coalesce(SearchRank(F("search_vector"), search_query), Value(0.0)))
        .filter(Q(search_vector=search_query) | Q(id__in=Subquery(message_ticket_ids)))
        .order_by("-rank", "-created")
    )


def ticket_status_counts(association_id: int, member: Any = None) -> dict[str, int]:
    """Return total/active/done ticket counts, optionally scoped to a member."""
    queryset = LarpManagerTicket.objects.filter(association_id=association_id)
    if member is not None:
        queryset = queryset.filter(member=member)
    return queryset.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(status__in=ACTIVE_TICKET_STATUSES)),
        done=Count("id", filter=Q(status=TicketStatus.DONE)),
    )


@login_required
def tickets(request: HttpRequest) -> Any:
    """Display support tickets.

    Admins see every ticket for the association and default to active
    (open/working) statuses; regular members see only their own tickets across
    all statuses.

    Args:
        request: The HTTP request object.

    Returns:
        Rendered ticket list template.

    """
    context = get_context(request)
    is_admin = bool(context.get("is_admin"))

    status = request.GET.get("status", "").strip()
    if not status:
        status = "active" if is_admin else "all"

    queryset = LarpManagerTicket.objects.filter(association_id=context["association_id"]).select_related(
        "member", "association"
    )

    member = context.get("member")
    if not is_admin:
        # A member-less actor must not match member=None tickets (None == None).
        queryset = queryset.filter(member=member) if member is not None else queryset.none()

    query = request.GET.get("q", "").strip()
    stranded = request.GET.get("stranded", "").strip()
    context["query"] = query
    context["status"] = status
    context["stranded"] = stranded
    if is_admin:
        context["counts"] = ticket_status_counts(context["association_id"])
    elif member is not None:
        context["counts"] = ticket_status_counts(context["association_id"], member)
    else:
        context["counts"] = {"total": 0, "active": 0, "done": 0}
    queryset = filter_tickets_by_stranded(queryset, stranded)
    context["tickets"] = apply_ticket_search(filter_tickets_by_status(queryset, status), query)
    return render(request, "larpmanager/member/tickets.html", context)


def _ticket_detail_context(request: HttpRequest, context: dict, ticket: LarpManagerTicket) -> dict:
    """Populate the shared context used by the ticket detail page."""
    context["ticket"] = ticket
    context["can_manage_tickets"] = _is_ticket_staff(request, context)
    context["ticket_events"] = ticket.events.select_related("actor_member").order_by("created", "id")
    context["discord_guild_id"] = getattr(conf_settings, "DISCORD_GUILD_ID", None)
    context["status_choices"] = TicketStatus.choices
    context["priority_choices"] = TicketPriority.choices
    context["ticket_messages"] = list(ticket.messages.order_by(Coalesce("sent_at", "created"), "id"))
    context["last_message_id"] = context["ticket_messages"][-1].id if context["ticket_messages"] else 0
    context["transcript_links"] = _active_transcript_links(request, ticket) if context["can_manage_tickets"] else []
    return context


def _active_transcript_links(request: HttpRequest, ticket: LarpManagerTicket) -> list[dict[str, Any]]:
    """Return active transcript links with their absolute share URLs."""
    links = ticket.transcript_links.filter(expires_at__gt=timezone.now(), open_count__lt=F("max_opens")).order_by(
        "-created"
    )
    return [
        {
            "object": link,
            "absolute_url": request.build_absolute_uri(reverse("transcript_share", kwargs={"token": link.token})),
            "remaining_opens": link.max_opens - link.open_count,
        }
        for link in links
    ]


def _is_ticket_staff(request: HttpRequest, context: dict) -> bool:
    """Return True when the actor may view/manage every ticket (admin or exe_tickets)."""
    return bool(context.get("is_admin")) or has_association_permission(request, context, "exe_tickets")


def _get_ticket_accessible(request: HttpRequest, context: dict, ticket_uuid: str) -> LarpManagerTicket:
    """Fetch a ticket scoped to the association, enforcing owner/staff access."""
    ticket = (
        LarpManagerTicket.objects.filter(uuid=ticket_uuid, association_id=context["association_id"])
        .select_related("member")
        .first()
    )

    if ticket is None:
        raise Http404

    actor_member = context.get("member")
    # Staff may open any ticket (including member=None API tickets); otherwise the
    # actor must own it. A member-less actor owns nothing (guards None == None).
    if not _is_ticket_staff(request, context) and (actor_member is None or ticket.member_id != actor_member.id):
        emit_ticket_event(
            ticket,
            TicketEvent.EventType.ACCESS_DENIED,
            source=TicketEvent.Source.API,
            actor_member=actor_member,
            payload={"path": request.path, "ip": get_client_ip(request)},
        )
        raise Http404

    return ticket


def _get_ticket_for_staff(request: HttpRequest, context: dict, ticket_uuid: str) -> LarpManagerTicket:
    """Fetch a ticket for a staff mutation, raising 404 for non-staff users."""
    ticket = (
        LarpManagerTicket.objects.filter(uuid=ticket_uuid, association_id=context["association_id"])
        .select_related("member")
        .first()
    )

    if ticket is None or not _is_ticket_staff(request, context):
        raise Http404

    return ticket


def _parse_base_version(request: HttpRequest) -> tuple[int | None, str | None]:
    """Parse the optimistic-lock ``base_version`` form field, returning (value, error)."""
    raw = request.POST.get("base_version", "").strip()
    if not raw:
        return None, "Missing base version"
    try:
        return int(raw), None
    except ValueError:
        return None, "Invalid base version"


def _emit_update_events(
    ticket: LarpManagerTicket,
    *,
    old_status: str,
    new_status: str,
    old_priority: str,
    new_priority: str,
    old_assigned: int | None,
    new_assigned: int | None,
    actor_member: Any,
) -> None:
    """Emit the TicketEvents describing a staff update.

    A transition into ``done`` is emitted as CLOSED (archive trigger for the bot)
    rather than a bare STATUS_CHANGED.
    """
    if new_status != old_status:
        if new_status == TicketStatus.DONE:
            emit_ticket_event(
                ticket,
                TicketEvent.EventType.CLOSED,
                source=TicketEvent.Source.API,
                from_status=old_status,
                to_status=new_status,
                actor_member=actor_member,
                payload={"to_status": new_status},
            )
        else:
            emit_ticket_event(
                ticket,
                TicketEvent.EventType.STATUS_CHANGED,
                source=TicketEvent.Source.API,
                from_status=old_status,
                to_status=new_status,
                actor_member=actor_member,
                payload={"to_status": new_status},
            )
    if new_priority != old_priority:
        emit_ticket_event(
            ticket,
            TicketEvent.EventType.PRIORITY_CHANGED,
            source=TicketEvent.Source.API,
            actor_member=actor_member,
            payload={"to_priority": new_priority, "from_priority": old_priority},
        )
    if new_assigned != old_assigned:
        emit_ticket_event(
            ticket,
            TicketEvent.EventType.ASSIGNED,
            source=TicketEvent.Source.API,
            actor_member=actor_member,
            payload={"assigned_staff_discord_id": new_assigned},
        )


def _render_detail_with_error(
    request: HttpRequest, context: dict, ticket: LarpManagerTicket, error: str, status: int = 200
) -> Any:
    """Render the ticket detail page with an inline error message."""
    context = _ticket_detail_context(request, context, ticket)
    context["error"] = error
    return render(request, "larpmanager/member/ticket_detail.html", context, status=status)


@login_required
def ticket_detail(request: HttpRequest, ticket_uuid: str) -> Any:
    """Display a single support ticket.

    Accessible to the ticket creator and to association admins.

    Args:
        request: The HTTP request object.
        ticket_uuid: UUID of the ticket to display.

    Returns:
        Rendered ticket detail template.

    Raises:
        Http404: If the ticket does not exist or the user has no access.

    """
    context = get_context(request)
    ticket = _get_ticket_accessible(request, context, ticket_uuid)
    context = _ticket_detail_context(request, context, ticket)
    return render(request, "larpmanager/member/ticket_detail.html", context)


@login_required
@require_POST
def ticket_update(request: HttpRequest, ticket_uuid: str) -> Any:
    """Update ticket status/priority/assignment/analysis as staff (W5).

    The mutation is an atomic conditional update keyed on ``version ==
    base_version``; a stale form loses the race and renders a 409 conflict.
    """
    context = get_context(request)
    ticket = _get_ticket_for_staff(request, context, ticket_uuid)

    base_version, error = _parse_base_version(request)
    if error:
        return _render_detail_with_error(request, context, ticket, error, status=409)

    old_status = ticket.status
    old_priority = ticket.priority
    old_assigned = ticket.assigned_staff_discord_id

    new_status = request.POST.get("status", ticket.status)
    if new_status not in TicketStatus.values:
        new_status = ticket.status
    new_priority = request.POST.get("priority", ticket.priority)
    if new_priority not in TicketPriority.values:
        new_priority = ticket.priority

    assigned_raw = request.POST.get("assigned_staff_discord_id", "").strip()
    if assigned_raw:
        try:
            new_assigned: int | None = int(assigned_raw)
        except ValueError:
            return _render_detail_with_error(request, context, ticket, "Invalid staff Discord ID", status=400)
    else:
        new_assigned = None

    new_analysis = request.POST.get("analysis", "")[:10000]

    new_closed_at = ticket.closed_at or timezone.now() if new_status == TicketStatus.DONE else None

    with transaction.atomic():
        updated = LarpManagerTicket.objects.filter(uuid=ticket.uuid, version=base_version).update(
            version=F("version") + 1,
            status=new_status,
            priority=new_priority,
            assigned_staff_discord_id=new_assigned,
            analysis=new_analysis,
            closed_at=new_closed_at,
            updated=timezone.now(),
        )
        if updated == 0:
            ticket.refresh_from_db()
            return _render_detail_with_error(request, context, ticket, CONCURRENT_EDIT_MESSAGE, status=409)

        _emit_update_events(
            ticket,
            old_status=old_status,
            new_status=new_status,
            old_priority=old_priority,
            new_priority=new_priority,
            old_assigned=old_assigned,
            new_assigned=new_assigned,
            actor_member=context.get("member"),
        )

    return redirect("ticket_detail", ticket_uuid=ticket.uuid)


@login_required
@require_POST
def ticket_close(request: HttpRequest, ticket_uuid: str) -> Any:
    """Close a ticket as staff via an atomic CAS, emitting a CLOSED event."""
    context = get_context(request)
    ticket = _get_ticket_for_staff(request, context, ticket_uuid)

    base_version, error = _parse_base_version(request)
    if error:
        return _render_detail_with_error(request, context, ticket, error, status=409)

    old_status = ticket.status
    closed_at = timezone.now()

    with transaction.atomic():
        updated = LarpManagerTicket.objects.filter(uuid=ticket.uuid, version=base_version).update(
            version=F("version") + 1,
            status=TicketStatus.DONE,
            closed_at=closed_at,
            updated=closed_at,
        )
        if updated == 0:
            ticket.refresh_from_db()
            return _render_detail_with_error(request, context, ticket, CONCURRENT_EDIT_MESSAGE, status=409)

        emit_ticket_event(
            ticket,
            TicketEvent.EventType.CLOSED,
            source=TicketEvent.Source.API,
            from_status=old_status,
            to_status=TicketStatus.DONE,
            actor_member=context.get("member"),
            payload={},
        )

    return redirect("ticket_detail", ticket_uuid=ticket.uuid)


@login_required
@require_POST
def ticket_reopen(request: HttpRequest, ticket_uuid: str) -> Any:
    """Reopen a closed ticket as staff via an atomic CAS, emitting REOPENED."""
    context = get_context(request)
    ticket = _get_ticket_for_staff(request, context, ticket_uuid)

    base_version, error = _parse_base_version(request)
    if error:
        return _render_detail_with_error(request, context, ticket, error, status=409)

    old_status = ticket.status

    with transaction.atomic():
        updated = LarpManagerTicket.objects.filter(uuid=ticket.uuid, version=base_version).update(
            version=F("version") + 1,
            status=TicketStatus.OPEN,
            closed_at=None,
            updated=timezone.now(),
        )
        if updated == 0:
            ticket.refresh_from_db()
            return _render_detail_with_error(request, context, ticket, CONCURRENT_EDIT_MESSAGE, status=409)

        emit_ticket_event(
            ticket,
            TicketEvent.EventType.REOPENED,
            source=TicketEvent.Source.API,
            from_status=old_status,
            to_status=TicketStatus.OPEN,
            actor_member=context.get("member"),
            payload={},
        )

    return redirect("ticket_detail", ticket_uuid=ticket.uuid)


@login_required
@require_POST
def ticket_reply(request: HttpRequest, ticket_uuid: str) -> Any:
    """Post a web reply to a ticket; persist it and push it to Discord (outbox)."""
    context = get_context(request)
    ticket = _get_ticket_accessible(request, context, ticket_uuid)

    content = (request.POST.get("content") or "").strip()
    if not content:
        return _render_detail_with_error(request, context, ticket, "Reply cannot be empty", status=400)
    if len(content) > MAX_REPLY_LENGTH:
        return _render_detail_with_error(request, context, ticket, "Reply is too long", status=400)

    member = context.get("member")
    author_name = str(member) if member else request.user.username

    message = TicketMessage.objects.create(
        ticket=ticket,
        discord_message_id=None,
        author_discord_id=None,
        author_name=author_name,
        content=content,
        is_bot=False,
        sent_at=timezone.now(),
    )

    TicketMessage.objects.filter(pk=message.pk).update(
        search_vector=SearchVector(
            Cast("content", TextField()),
            Cast("author_name", TextField()),
            config="english",
        )
    )

    line = render_transcript_line(message)
    LarpManagerTicket.objects.filter(pk=ticket.pk).update(
        transcript=Case(
            When(Q(transcript__isnull=True) | Q(transcript=""), then=Value(line, output_field=TextField())),
            default=Concat(F("transcript"), Value("\n" + line), output_field=TextField()),
        ),
    )

    emit_ticket_event(
        ticket,
        TicketEvent.EventType.REPLY,
        source=TicketEvent.Source.API,
        actor_member=member,
        payload={"content": content, "author_name": author_name},
    )

    return redirect("ticket_detail", ticket_uuid=ticket.uuid)


@login_required
@require_POST
def ticket_strand(request: HttpRequest, ticket_uuid: str) -> Any:
    """Strand a ticket: archive Discord but keep it alive in LarpManager (staff)."""
    context = get_context(request)
    ticket = _get_ticket_for_staff(request, context, ticket_uuid)

    channel_id = ticket.discord_channel_id
    with transaction.atomic():
        emit_ticket_event(
            ticket,
            TicketEvent.EventType.STRANDED,
            source=TicketEvent.Source.API,
            actor_member=context.get("member"),
            payload={"channel_id": channel_id},
        )
        ticket.stranded = True
        ticket.discord_channel_id = None
        ticket.save(update_fields=["stranded", "discord_channel_id"])

    return redirect("ticket_detail", ticket_uuid=ticket.uuid)


@login_required
@require_POST
def ticket_unstrand(request: HttpRequest, ticket_uuid: str) -> Any:
    """Un-strand a ticket: re-link it to Discord by creating a new channel (staff)."""
    context = get_context(request)
    ticket = _get_ticket_for_staff(request, context, ticket_uuid)

    if not ticket.stranded:
        return redirect("ticket_detail", ticket_uuid=ticket.uuid)

    with transaction.atomic():
        # Set on the instance first so the channel_create event is outbox-delivered.
        ticket.stranded = False
        emit_ticket_event(
            ticket,
            TicketEvent.EventType.CHANNEL_CREATE,
            source=TicketEvent.Source.API,
            actor_member=context.get("member"),
            payload={
                "ticket_uuid": str(ticket.uuid),
                "subject": ticket.subject,
                "association_uuid": str(ticket.association.uuid),
                "discord_creator_id": ticket.discord_creator_id,
            },
        )
        ticket.save(update_fields=["stranded"])

    return redirect("ticket_detail", ticket_uuid=ticket.uuid)


@login_required
@require_POST
def ticket_merge(request: HttpRequest, ticket_uuid: str) -> Any:
    """Merge this ticket into another, reassigning messages (staff)."""
    context = get_context(request)
    source = _get_ticket_for_staff(request, context, ticket_uuid)

    target_uuid = (request.POST.get("merge_into") or "").strip()
    if not target_uuid:
        return _render_detail_with_error(request, context, source, "merge_into is required", status=400)

    target = (
        LarpManagerTicket.objects.filter(
            uuid=target_uuid, association_id=context["association_id"], deleted__isnull=True
        )
        .first()
    )
    if target is None:
        return _render_detail_with_error(request, context, source, "Target ticket not found", status=404)
    if source.pk == target.pk:
        return _render_detail_with_error(request, context, source, "Cannot merge a ticket into itself", status=400)
    if source.merged_into_id is not None:
        return _render_detail_with_error(request, context, source, "Ticket is already merged", status=400)

    source_channel_id = source.discord_channel_id
    source_transcript = source.build_transcript()

    with transaction.atomic():
        TicketMessage.objects.filter(ticket=source).update(ticket=target)
        target.refresh_from_db()
        lines = [
            render_transcript_line(m)
            for m in target.messages.order_by(Coalesce("sent_at", "created"), "id")
        ]
        target.transcript = "\n".join(lines)
        target.save(update_fields=["transcript"])

        source.merged_into = target
        source.status = TicketStatus.DONE
        source.discord_channel_id = None
        source.stranded = False
        source.save(update_fields=["merged_into", "status", "discord_channel_id", "stranded"])

        emit_ticket_event(
            source,
            TicketEvent.EventType.MERGED,
            source=TicketEvent.Source.API,
            actor_member=context.get("member"),
            payload={
                "source_channel_id": source_channel_id,
                "target_uuid": str(target.uuid),
                "target_channel_id": target.discord_channel_id,
                "source_subject": source.subject,
                "source_transcript": source_transcript,
            },
        )

    return redirect("ticket_detail", ticket_uuid=target.uuid)


@login_required
def ticket_state(request: HttpRequest, ticket_uuid: str) -> JsonResponse:
    """Return the live state snapshot for the browser poller (D3)."""
    context = get_context(request)
    ticket = _get_ticket_accessible(request, context, ticket_uuid)

    last_message = ticket.messages.order_by("id").last()
    return JsonResponse(
        {
            "status": ticket.status,
            "priority": ticket.priority,
            "status_display": ticket.get_status_display(),
            "priority_display": ticket.get_priority_display(),
            "version": ticket.version,
            "last_message_id": last_message.id if last_message else None,
            "assigned_staff_discord_id": ticket.assigned_staff_discord_id,
        }
    )


@login_required
def ticket_messages(request: HttpRequest, ticket_uuid: str) -> JsonResponse:
    """Return TicketMessage rows with id greater than ``?after=`` (D3)."""
    context = get_context(request)
    ticket = _get_ticket_accessible(request, context, ticket_uuid)

    after_raw = request.GET.get("after", "").strip()
    try:
        after_id = int(after_raw) if after_raw else 0
    except ValueError:
        return JsonResponse({"error": "invalid after"}, status=400)

    messages = TicketMessage.objects.filter(ticket=ticket, id__gt=after_id).order_by(
        Coalesce("sent_at", "created"), "id"
    )
    return JsonResponse(
        {
            "messages": [
                {
                    "id": message.id,
                    "discord_message_id": message.discord_message_id,
                    "author_name": message.author_name,
                    "author_discord_id": message.author_discord_id,
                    "content": message.content,
                    "attachments": message.attachments,
                    "is_bot": message.is_bot,
                    "sent_at": message.sent_at.isoformat() if message.sent_at else None,
                }
                for message in messages
            ]
        }
    )


@login_required
def transcript_share(request: HttpRequest, token: str) -> Any:
    """Render a read-only transcript for a valid, unexpired share link.

    The token is association-scoped: both the link and its ticket must belong to
    the request's association. Expired, exhausted, revoked, and soft-deleted
    links all 404, and each successful open is logged and counted.
    """
    context = get_context(request)
    link = TicketTranscriptLink.objects.select_related("ticket").filter(token=token).first()
    if link is None:
        raise Http404

    # Association scope: a token never leaks a transcript across tenants.
    if link.association_id != context["association_id"] or link.ticket.association_id != context["association_id"]:
        raise Http404

    if link.is_expired():
        raise Http404

    # Atomically consume one open; a link exhausted by a concurrent reader 404s.
    updated = TicketTranscriptLink.objects.filter(pk=link.pk, open_count__lt=F("max_opens")).update(
        open_count=F("open_count") + 1
    )
    if updated == 0:
        raise Http404

    member = context.get("member")
    logger.info(
        "transcript link opened",
        extra={
            "token": token,
            "link_id": link.id,
            "ticket_id": link.ticket_id,
            "association_id": context["association_id"],
            "member_id": member.id if member else None,
        },
    )

    context["ticket"] = link.ticket
    context["ticket_messages"] = list(link.ticket.messages.order_by(Coalesce("sent_at", "created"), "id"))
    context["transcript"] = link.ticket.build_transcript()
    return render(request, "larpmanager/member/transcript_share.html", context)


@login_required
@require_POST
def ticket_transcript_link_create(request: HttpRequest, ticket_uuid: str) -> Any:
    """Create a shareable transcript link for a ticket as staff."""
    context = get_context(request)
    ticket = _get_ticket_for_staff(request, context, ticket_uuid)

    link = TicketTranscriptLink.objects.create(
        ticket=ticket,
        created_by=context.get("member"),
        expires_at=timezone.now() + TRANSCRIPT_LINK_TTL,
        max_opens=TRANSCRIPT_LINK_MAX_OPENS,
        association_id=context["association_id"],
    )
    logger.info(
        "transcript link created",
        extra={"ticket_id": ticket.id, "link_id": link.id, "member_id": link.created_by_id},
    )
    return redirect("ticket_detail", ticket_uuid=ticket.uuid)


@login_required
@require_POST
def ticket_transcript_link_revoke(request: HttpRequest, ticket_uuid: str) -> Any:
    """Revoke (soft-delete) a shareable transcript link for a ticket as staff."""
    context = get_context(request)
    ticket = _get_ticket_for_staff(request, context, ticket_uuid)

    token = request.POST.get("token", "").strip()
    if token:
        TicketTranscriptLink.objects.filter(ticket=ticket, token=token).delete()
    return redirect("ticket_detail", ticket_uuid=ticket.uuid)
