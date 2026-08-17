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
"""Shareable, expiring transcript link for a support ticket."""

from __future__ import annotations

import secrets
from typing import ClassVar

from django.db import models
from django.utils import timezone

from larpmanager.models.association import Association
from larpmanager.models.base import BaseModel, UuidMixin
from larpmanager.models.larpmanager import LarpManagerTicket
from larpmanager.models.member import Member


def generate_transcript_token() -> str:
    """Return a high-entropy URL-safe token for a transcript link."""
    return secrets.token_urlsafe(32)


class TicketTranscriptLink(UuidMixin, BaseModel):
    """A time-limited, open-capped shareable link to a ticket transcript.

    The token is only valid within the ticket's association for a logged-in
    member, until it expires or exhausts ``max_opens``.
    """

    ticket = models.ForeignKey(LarpManagerTicket, on_delete=models.CASCADE, related_name="transcript_links")

    token = models.CharField(max_length=64, unique=True, db_index=True, editable=False, default=generate_transcript_token)

    created_by = models.ForeignKey(
        Member,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_transcript_links",
    )

    expires_at = models.DateTimeField()

    max_opens = models.PositiveSmallIntegerField(default=5)

    open_count = models.PositiveIntegerField(default=0)

    association = models.ForeignKey(Association, on_delete=models.CASCADE)

    class Meta:
        ordering: ClassVar[list] = ["-created"]

    def is_expired(self) -> bool:
        """Return True when the link has passed its expiry timestamp."""
        return self.expires_at <= timezone.now()

    def is_exhausted(self) -> bool:
        """Return True when the link has reached its open cap."""
        return self.open_count >= self.max_opens

    def __str__(self) -> str:
        """Return a readable link summary."""
        return f"TranscriptLink #{self.id} for ticket #{self.ticket_id}"
