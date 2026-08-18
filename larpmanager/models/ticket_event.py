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
"""Ticket event model for the Discord outbox and status history."""

from __future__ import annotations

from typing import ClassVar

from django.db import models

from larpmanager.models.larpmanager import LarpManagerTicket
from larpmanager.models.member import Member


class TicketEvent(models.Model):
    """A single ticket lifecycle event.

    Events emitted with ``source="api"`` and no ``applied_at``/``acked_at`` are
    delivered to the Discord bot via the outbox cursor. History-only events and
    ``source="discord"`` events are emitted already acked so they only appear in
    ticket history, never in the outbox.
    """

    class EventType(models.TextChoices):
        """Supported ticket event types."""

        CREATED = "created", "Created"
        STATUS_CHANGED = "status_changed", "Status changed"
        PRIORITY_CHANGED = "priority_changed", "Priority changed"
        ASSIGNED = "assigned", "Assigned"
        CLOSED = "closed", "Closed"
        REOPENED = "reopened", "Reopened"
        DELETED = "deleted", "Deleted"
        CHANNEL_CREATE = "channel_create", "Channel create"
        CHANNEL_SYNCED = "channel_synced", "Channel synced"
        CHANNEL_UPDATE = "channel_update", "Channel update"
        CHANNEL_ARCHIVE = "channel_archive", "Channel archive"
        ACCESS_DENIED = "access_denied", "Access denied"
        REPLY = "reply", "Reply"

    class Source(models.TextChoices):
        """Origin of the event, derived from the authenticating key."""

        API = "api", "API"
        DISCORD = "discord", "Discord"

    id = models.BigAutoField(primary_key=True)

    ticket = models.ForeignKey(LarpManagerTicket, on_delete=models.CASCADE, related_name="events")

    event_type = models.CharField(max_length=32, choices=EventType.choices)

    source = models.CharField(max_length=16, choices=Source.choices)

    from_status = models.CharField(max_length=20, null=True, blank=True)

    to_status = models.CharField(max_length=20, null=True, blank=True)

    actor_discord_id = models.BigIntegerField(null=True, blank=True)

    actor_member = models.ForeignKey(Member, on_delete=models.SET_NULL, null=True, blank=True)

    payload = models.JSONField(default=dict, blank=True)

    created = models.DateTimeField(auto_now_add=True)

    acked_at = models.DateTimeField(null=True, blank=True)

    applied_at = models.DateTimeField(null=True, blank=True)

    attempts = models.IntegerField(default=0)

    last_error = models.TextField(null=True, blank=True)

    class Meta:
        ordering: ClassVar[list] = ["id"]
        indexes: ClassVar[list] = [
            models.Index(fields=["source", "applied_at", "acked_at"], name="ticket_event_outbox_idx"),
        ]

    def __str__(self) -> str:
        """Return a readable event summary."""
        return f"TicketEvent #{self.id} {self.event_type} ({self.source})"

    def actor_display(self) -> str | None:
        """Return a human-readable actor label (member name or discord id)."""
        if self.actor_member_id:
            return self.actor_member.display_member()
        if self.actor_discord_id is not None:
            return str(self.actor_discord_id)
        return None
