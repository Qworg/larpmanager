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

from typing import TYPE_CHECKING, Any

from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.http import JsonResponse
from django.shortcuts import render
from django_ratelimit.decorators import ratelimit

from larpmanager.models.larpmanager import LarpManagerTicket
from larpmanager.utils.core.base import check_association_context
from larpmanager.utils.publication.api import get_client_ip
from larpmanager.views.user.ticket import (
    apply_ticket_search,
    filter_tickets_by_status,
    filter_tickets_by_stranded,
    ticket_poll_rate_limited_response,
    ticket_status_counts,
)

if TYPE_CHECKING:
    from django.http import HttpRequest

# Rows per page: bounds the query/render cost (and the number of live-poller
# roots on the page) regardless of how many tickets the association has.
TICKETS_PAGE_SIZE = 50

# The batch poller (FIX-WEB-3) accepts at most one page worth of uuids, so a
# request can never cost more than TICKETS_PAGE_SIZE row lookups regardless of
# what a caller sends.
MAX_BATCH_STATE_UUIDS = TICKETS_PAGE_SIZE

# Rate limit for the staff dashboard's batch poller (FIX-WEB-3/FIX-WEB-2): one
# dashboard tab issues ONE request per shared ticketPoller tick (~5s baseline)
# regardless of how many rows are on the page, i.e. ~12 req/min per tab. This
# ceiling gives headroom for several staff dashboard tabs open at once (e.g.
# up to ~10) plus the poller's error backoff, while MAX_BATCH_STATE_UUIDS
# bounds the per-request cost so a higher ceiling doesn't cost more per hit.
TICKET_BATCH_POLL_RATE = "120/m"


def _ticket_batch_poll_key(_group: str, request: HttpRequest) -> str:
    """Rate-limit key for the batch poller: user id (this endpoint has no single ticket to scope to)."""
    user_id = request.user.pk if request.user.is_authenticated else "anon"
    return f"user:{user_id}"


def _ticket_batch_poll_ip_key(_group: str, request: HttpRequest) -> str:
    """IP-side counterpart to ``_ticket_batch_poll_key``."""
    return f"ip:{get_client_ip(request)}"


@login_required
def exe_tickets(request: HttpRequest) -> Any:
    """Staff dashboard listing every ticket for the association.

    Not filtered by ``member`` so API-created tickets (``member=None``) are
    visible. Defaults to active (open/working) statuses with a search box.
    Results are paginated (``?page=``) so an association with a large ticket
    history renders a bounded page instead of every ticket at once.
    """
    context = check_association_context(request, "exe_tickets")

    status = request.GET.get("status", "").strip() or "active"
    query = request.GET.get("q", "").strip()
    stranded = request.GET.get("stranded", "").strip()

    queryset = LarpManagerTicket.objects.filter(association_id=context["association_id"]).select_related(
        "member", "association"
    )
    queryset = apply_ticket_search(filter_tickets_by_status(queryset, status), query)
    queryset = filter_tickets_by_stranded(queryset, stranded)

    paginator = Paginator(queryset, TICKETS_PAGE_SIZE)
    try:
        page = paginator.page(request.GET.get("page"))
    except PageNotAnInteger:
        page = paginator.page(1)
    except EmptyPage:
        page = paginator.page(paginator.num_pages)

    context["status"] = status
    context["query"] = query
    context["stranded"] = stranded
    context["counts"] = ticket_status_counts(context["association_id"])
    context["tickets"] = page
    return render(request, "larpmanager/exe/tickets.html", context)


@login_required
@ratelimit(key=_ticket_batch_poll_ip_key, rate=TICKET_BATCH_POLL_RATE, method="GET", block=False)
@ratelimit(key=_ticket_batch_poll_key, rate=TICKET_BATCH_POLL_RATE, method="GET", block=False)
def exe_ticket_states_batch(request: HttpRequest) -> JsonResponse:
    """Batch state snapshot for the staff dashboard's live poller (FIX-WEB-3).

    Restores live status/priority refresh for ``exe_tickets`` without the
    original per-row polling: the page's single shared poller fetches every
    ticket currently visible on the page in ONE request instead of one per
    row. Access control mirrors ``exe_tickets`` exactly (same association +
    permission check) so a caller can never read another association's
    tickets by uuid-guessing. ``?uuids=`` is a comma-separated list, capped at
    MAX_BATCH_STATE_UUIDS (one page worth) regardless of how many are sent.
    """
    if getattr(request, "limited", False):
        return ticket_poll_rate_limited_response(
            request,
            exe_ticket_states_batch,
            TICKET_BATCH_POLL_RATE,
            ip_key=_ticket_batch_poll_ip_key,
            user_key=_ticket_batch_poll_key,
        )

    context = check_association_context(request, "exe_tickets")

    raw_uuids = [value for value in request.GET.get("uuids", "").split(",") if value]
    uuids = raw_uuids[:MAX_BATCH_STATE_UUIDS]
    if not uuids:
        return JsonResponse({"tickets": {}})

    tickets = LarpManagerTicket.objects.filter(uuid__in=uuids, association_id=context["association_id"])
    states = {
        str(ticket.uuid): {
            "status": ticket.status,
            "priority": ticket.priority,
            "status_display": ticket.get_status_display(),
            "priority_display": ticket.get_priority_display(),
            "version": ticket.version,
        }
        for ticket in tickets
    }
    return JsonResponse({"tickets": states})
