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

from typing import Any

from django.conf import settings as conf_settings
from django.contrib.auth.decorators import login_required
from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db import transaction
from django.db.models import Count, F, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.http import Http404, HttpRequest, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from larpmanager.models.larpmanager import LarpManagerTicket, TicketPriority, TicketStatus
from larpmanager.models.ticket_event import TicketEvent
from larpmanager.models.ticket_message import TicketMessage
from larpmanager.utils.auth.permission import has_association_permission
from larpmanager.utils.core.base import get_context
from larpmanager.utils.publication.api import get_client_ip
from larpmanager.utils.ticket_events import emit_ticket_event

# Statuses considered "active" (not done). The staff list defaults to these.
ACTIVE_TICKET_STATUSES = (TicketStatus.OPEN, TicketStatus.WORKING)

# Shown when a staff member submits a stale form (optimistic-lock conflict, D8).
CONCURRENT_EDIT_MESSAGE = "This ticket was modified by someone else. Review the latest changes and retry."


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
    context["query"] = query
    context["status"] = status
    if is_admin:
        context["counts"] = ticket_status_counts(context["association_id"])
    elif member is not None:
        context["counts"] = ticket_status_counts(context["association_id"], member)
    else:
        context["counts"] = {"total": 0, "active": 0, "done": 0}
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
    return context


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
