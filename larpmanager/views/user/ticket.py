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
from django.http import Http404, HttpRequest
from django.shortcuts import get_object_or_404, render

from larpmanager.models.larpmanager import LarpManagerTicket
from larpmanager.utils.core.base import get_context


@login_required
def tickets(request: HttpRequest) -> Any:
    """Display support tickets.

    Admins see every ticket for the association; regular members see only
    the tickets they created.

    Args:
        request: The HTTP request object.

    Returns:
        Rendered ticket list template.

    """
    context = get_context(request)

    queryset = LarpManagerTicket.objects.filter(association_id=context["association_id"]).select_related(
        "member", "association"
    )

    if not context.get("is_admin"):
        queryset = queryset.filter(member=context["member"])

    context["tickets"] = queryset.order_by("-created")
    return render(request, "larpmanager/member/tickets.html", context)


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

    ticket = get_object_or_404(
        LarpManagerTicket,
        uuid=ticket_uuid,
        association_id=context["association_id"],
    )

    if not context.get("is_admin") and ticket.member != context["member"]:
        raise Http404

    context["ticket"] = ticket
    context["discord_guild_id"] = getattr(conf_settings, "DISCORD_GUILD_ID", None)
    return render(request, "larpmanager/member/ticket_detail.html", context)
