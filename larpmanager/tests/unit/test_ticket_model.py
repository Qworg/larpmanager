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
"""Tests for LarpManagerTicket model with Discord fields."""

from django.db import IntegrityError
from django.utils import timezone

from larpmanager.models.larpmanager import LarpManagerTicket, TicketPriority, TicketStatus
from larpmanager.tests.unit.base import BaseTestCase


class TestTicketModel(BaseTestCase):
    """Test cases for the LarpManagerTicket model."""

    def test_ticket_creation_with_discord_fields(self):
        """Test creating a ticket with all Discord fields."""
        ticket = self.create_larpmanager_ticket(
            subject="Test Subject",
            discord_channel_id=123456789012345678,
            discord_creator_id=987654321098765432,
            assigned_staff_discord_id=111222333444555666,
            transcript="Test transcript content",
            closed_at=timezone.now(),
        )

        # Verify all fields were saved correctly
        self.assertEqual(ticket.subject, "Test Subject")
        self.assertEqual(ticket.discord_channel_id, 123456789012345678)
        self.assertEqual(ticket.discord_creator_id, 987654321098765432)
        self.assertEqual(ticket.assigned_staff_discord_id, 111222333444555666)
        self.assertEqual(ticket.transcript, "Test transcript content")
        self.assertIsNotNone(ticket.closed_at)
        self.assertIsNotNone(ticket.uuid)

    def test_ticket_str_with_subject(self):
        """Test __str__ returns subject when present."""
        ticket = self.create_larpmanager_ticket(subject="My Support Request")

        result = str(ticket)

        self.assertIn("My Support Request", result)
        self.assertIn(str(ticket.id), result)

    def test_ticket_str_without_subject_with_reason(self):
        """Test __str__ falls back to reason when subject is not present."""
        ticket = self.create_larpmanager_ticket(
            subject=None,
            reason="Technical Issue",
        )

        result = str(ticket)

        self.assertIn("Technical Issue", result)

    def test_ticket_str_without_subject_or_reason(self):
        """Test __str__ returns 'No reason' when neither subject nor reason."""
        ticket = self.create_larpmanager_ticket(
            subject=None,
            reason=None,
        )

        result = str(ticket)

        self.assertIn("No reason", result)

    def test_ticket_discord_channel_id_unique_constraint(self):
        """Test that discord_channel_id has unique constraint."""
        channel_id = 123456789012345678

        # Create first ticket with channel ID
        self.create_larpmanager_ticket(discord_channel_id=channel_id)

        # Attempt to create second ticket with same channel ID
        with self.assertRaises(IntegrityError):
            self.create_larpmanager_ticket(discord_channel_id=channel_id)

    def test_ticket_status_transitions(self):
        """Test that ticket status can transition through all states."""
        ticket = self.create_larpmanager_ticket(status=TicketStatus.OPEN)
        self.assertEqual(ticket.status, TicketStatus.OPEN)

        # Transition to working
        ticket.status = TicketStatus.WORKING
        ticket.save()
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, TicketStatus.WORKING)

        # Transition to done
        ticket.status = TicketStatus.DONE
        ticket.save()
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, TicketStatus.DONE)

    def test_ticket_priority_choices(self):
        """Test that all priority values work correctly."""
        # Test LOW priority
        ticket_low = self.create_larpmanager_ticket(priority=TicketPriority.LOW)
        self.assertEqual(ticket_low.priority, TicketPriority.LOW)

        # Test MEDIUM priority
        ticket_medium = self.create_larpmanager_ticket(priority=TicketPriority.MEDIUM)
        self.assertEqual(ticket_medium.priority, TicketPriority.MEDIUM)

        # Test HIGH priority
        ticket_high = self.create_larpmanager_ticket(priority=TicketPriority.HIGH)
        self.assertEqual(ticket_high.priority, TicketPriority.HIGH)

    def test_ticket_closed_at_set_on_close(self):
        """Test that closed_at is populated when status changes to done."""
        ticket = self.create_larpmanager_ticket(status=TicketStatus.OPEN)
        self.assertIsNone(ticket.closed_at)

        # Close the ticket
        ticket.status = TicketStatus.DONE
        ticket.closed_at = timezone.now()
        ticket.save()

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, TicketStatus.DONE)
        self.assertIsNotNone(ticket.closed_at)
