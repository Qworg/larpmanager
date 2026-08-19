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
from django.shortcuts import render

from larpmanager.models.larpmanager import LarpManagerTicket
from larpmanager.utils.core.base import check_association_context
from larpmanager.views.user.ticket import (
    apply_ticket_search,
    filter_tickets_by_status,
    filter_tickets_by_stranded,
    ticket_status_counts,
)

if TYPE_CHECKING:
    from django.http import HttpRequest


@login_required
def exe_tickets(request: HttpRequest) -> Any:
    """Staff dashboard listing every ticket for the association.

    Not filtered by ``member`` so API-created tickets (``member=None``) are
    visible. Defaults to active (open/working) statuses with a search box.
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

    context["status"] = status
    context["query"] = query
    context["stranded"] = stranded
    context["counts"] = ticket_status_counts(context["association_id"])
    context["tickets"] = queryset
    return render(request, "larpmanager/exe/tickets.html", context)
