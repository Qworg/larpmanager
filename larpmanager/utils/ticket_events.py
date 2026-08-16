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
"""Helpers for emitting ticket lifecycle events."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.utils import timezone

from larpmanager.models.ticket_event import TicketEvent

if TYPE_CHECKING:
    from larpmanager.models.larpmanager import LarpManagerTicket
    from larpmanager.models.member import Member

# Event types that are pure history markers: they are never delivered to the bot,
# so they are emitted with applied_at/acked_at already set.
HISTORY_ONLY_EVENT_TYPES = {
    TicketEvent.EventType.CREATED,
    TicketEvent.EventType.CHANNEL_SYNCED,
    TicketEvent.EventType.ACCESS_DENIED,
}


def emit_ticket_event(  # noqa: PLR0913
    ticket: LarpManagerTicket,
    event_type: str,
    *,
    source: str,
    from_status: str | None = None,
    to_status: str | None = None,
    actor_discord_id: int | None = None,
    actor_member: Member | None = None,
    payload: dict[str, Any] | None = None,
    applied_and_acked: bool = False,
) -> TicketEvent:
    """Create a TicketEvent for a ticket mutation.

    ``source="discord"`` events and history-only types (created, channel_synced,
    access_denied) are emitted with ``applied_at``/``acked_at`` already set so they
    never surface in the outbox. ``source="api"`` events are left unacked for the
    bot outbox cursor to deliver.
    """
    now = timezone.now()
    already_applied = applied_and_acked or source == TicketEvent.Source.DISCORD or event_type in HISTORY_ONLY_EVENT_TYPES

    return TicketEvent.objects.create(
        ticket=ticket,
        event_type=event_type,
        source=source,
        from_status=from_status,
        to_status=to_status,
        actor_discord_id=actor_discord_id,
        actor_member=actor_member,
        payload=payload or {},
        acked_at=now if already_applied else None,
        applied_at=now if already_applied else None,
    )
