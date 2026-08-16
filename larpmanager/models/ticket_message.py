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
"""Authoritative per-message transcript rows for a ticket."""

from __future__ import annotations

from typing import ClassVar

from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models

from larpmanager.models.base import BaseModel, UuidMixin
from larpmanager.models.larpmanager import LarpManagerTicket


class TicketMessage(UuidMixin, BaseModel):
    """A single Discord message persisted as part of a ticket transcript.

    ``discord_message_id`` is the natural idempotency key; the bot upserts on it
    so message delivery is at-least-once without producing duplicates.
    """

    ticket = models.ForeignKey(LarpManagerTicket, on_delete=models.CASCADE, related_name="messages")

    discord_message_id = models.BigIntegerField(unique=True)

    author_discord_id = models.BigIntegerField(null=True, blank=True)

    author_name = models.CharField(max_length=255)

    content = models.TextField()

    attachments = models.JSONField(default=list, blank=True)

    is_bot = models.BooleanField(default=False)

    sent_at = models.DateTimeField(null=True, blank=True)

    search_vector = SearchVectorField(null=True, editable=False)

    class Meta:
        indexes: ClassVar[list] = [
            GinIndex(fields=["search_vector"], name="ticket_msg_search_vector_idx"),
        ]

    def __str__(self) -> str:
        """Return a readable message summary."""
        return f"TicketMessage #{self.discord_message_id} by {self.author_name}"
