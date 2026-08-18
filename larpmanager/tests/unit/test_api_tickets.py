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
"""Tests for the ticket REST API endpoints."""

import json
from datetime import UTC, datetime

import pytest
from django.core.cache import cache
from django.test import Client, override_settings

from larpmanager.models.association import Association
from larpmanager.models.base import PublisherApiKey
from larpmanager.models.larpmanager import LarpManagerTicket, TicketPriority, TicketStatus
from larpmanager.models.ticket_event import TicketEvent
from larpmanager.models.ticket_message import TicketMessage
from larpmanager.tests.unit.base import BaseTestCase
from larpmanager.utils.ticket_events import emit_ticket_event


class TestTicketAPI(BaseTestCase):
    """Test cases for the ticket REST API."""

    def setUp(self):
        """Set up test fixtures."""
        super().setUp()
        cache.clear()
        self.client = Client()
        self.api_key = PublisherApiKey.objects.create(
            name="Test Key",
            key="test-api-key-12345",
            active=True,
            scopes=["tickets:read", "tickets:write"],
        )
        self.headers = {"HTTP_X_API_KEY": "test-api-key-12345"}

    def test_list_tickets_requires_auth(self):
        """Test that GET /tickets/ returns 401 without API key."""
        response = self.client.get("/api/v1/tickets/")
        self.assertEqual(response.status_code, 401)

    def test_list_tickets_with_valid_api_key(self):
        """Test that GET /tickets/ returns ticket list with valid API key."""
        association = self.create_association(name="List Org", slug="list-org")
        ticket = self.create_larpmanager_ticket(association=association, discord_channel_id=123456789)

        response = self.client.get(f"/api/v1/tickets/?association_uuid={association.uuid}", **self.headers)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("tickets", data)
        self.assertEqual(len(data["tickets"]), 1)
        self.assertEqual(data["tickets"][0]["uuid"], str(ticket.uuid))

    def test_list_tickets_filter_by_status(self):
        """Test that ?status= filter works correctly."""
        association = self.create_association(name="Status Org", slug="status-org")
        self.create_larpmanager_ticket(association=association, status=TicketStatus.OPEN, discord_channel_id=111)
        self.create_larpmanager_ticket(association=association, status=TicketStatus.DONE, discord_channel_id=222)

        response = self.client.get(f"/api/v1/tickets/?association_uuid={association.uuid}&status=open", **self.headers)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["tickets"]), 1)
        self.assertEqual(data["tickets"][0]["status"], "open")

    def test_list_tickets_filter_by_association(self):
        """Test that ?association_uuid= filter works correctly."""
        assoc1 = self.create_association(name="Org 1", slug="org1")
        assoc2 = self.create_association(name="Org 2", slug="org2")

        self.create_larpmanager_ticket(association=assoc1, discord_channel_id=111)
        self.create_larpmanager_ticket(association=assoc2, discord_channel_id=222)

        response = self.client.get(
            f"/api/v1/tickets/?association_uuid={assoc1.uuid}",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["tickets"]), 1)
        self.assertEqual(data["tickets"][0]["association"]["uuid"], str(assoc1.uuid))

    def test_list_tickets_filter_by_discord_creator(self):
        """Test that ?discord_creator_id= filter works correctly."""
        self.create_larpmanager_ticket(discord_creator_id=12345, discord_channel_id=111)
        self.create_larpmanager_ticket(discord_creator_id=67890, discord_channel_id=222)

        response = self.client.get(
            "/api/v1/tickets/?discord_creator_id=12345",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["tickets"]), 1)
        self.assertEqual(data["tickets"][0]["discord_creator_id"], 12345)

    def test_list_tickets_filter_discord_only(self):
        """Test that ?discord_only=true filters correctly."""
        association = self.create_association(name="Discord Org", slug="discord-org")
        self.create_larpmanager_ticket(association=association, discord_channel_id=111)  # Has channel
        self.create_larpmanager_ticket(association=association, discord_channel_id=None)  # No channel

        response = self.client.get(
            f"/api/v1/tickets/?association_uuid={association.uuid}&discord_only=true",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["tickets"]), 1)
        self.assertIsNotNone(data["tickets"][0]["discord_channel_id"])

    def test_list_tickets_pagination(self):
        """Test that ?limit= and ?offset= work correctly."""
        association = self.create_association(name="Pagination Org", slug="pagination-org")
        # Create 5 tickets
        for i in range(5):
            self.create_larpmanager_ticket(
                association=association,
                subject=f"Ticket {i}",
                discord_channel_id=100 + i,
            )

        # Get first 2
        response = self.client.get(
            f"/api/v1/tickets/?association_uuid={association.uuid}&limit=2&offset=0",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["tickets"]), 2)
        self.assertEqual(data["total"], 5)
        self.assertEqual(data["limit"], 2)
        self.assertEqual(data["offset"], 0)

    def test_build_transcript_format(self):
        """build_transcript renders [timestamp] username content rows oldest-first."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=123987)
        TicketMessage.objects.create(
            ticket=ticket,
            discord_message_id=1001,
            author_name="Alice",
            content="first message",
            sent_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        )
        TicketMessage.objects.create(
            ticket=ticket,
            discord_message_id=1002,
            author_name="Bob",
            content="second message",
            sent_at=datetime(2026, 1, 1, 12, 1, 0, tzinfo=UTC),
        )

        transcript = ticket.build_transcript()

        self.assertEqual(
            transcript,
            "[2026-01-01 12:00:00] Alice first message\n[2026-01-01 12:01:00] Bob second message",
        )

    def test_title_search(self):
        """?q= matches a word in the ticket title via the ticket search vector."""
        ticket = self.create_larpmanager_ticket(subject="Zephyr Billing Question", discord_channel_id=321321)

        response = self.client.get("/api/v1/tickets/?q=zephyr", **self.headers)

        self.assertEqual(response.status_code, 200)
        uuids = [ticket_data["uuid"] for ticket_data in response.json()["tickets"]]
        self.assertEqual(uuids, [str(ticket.uuid)])

    def test_create_ticket_success(self):
        """Test that POST creates ticket with all fields."""
        association = self.get_association()

        response = self.client.post(
            "/api/v1/tickets/",
            data=json.dumps({
                "association_uuid": str(association.uuid),
                "discord_creator_id": 123456789,
                "discord_channel_id": 987654321,
                "subject": "Test Subject",
                "content": "Test content",
                "priority": "medium",
            }),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertIn("ticket", data)
        self.assertEqual(data["ticket"]["subject"], "Test Subject")
        self.assertEqual(data["ticket"]["discord_channel_id"], 987654321)
        self.assertEqual(data["ticket"]["priority"], "medium")

    def test_create_ticket_missing_required_fields(self):
        """Test that POST returns 400 for missing required fields."""
        response = self.client.post(
            "/api/v1/tickets/",
            data=json.dumps({"subject": "Test"}),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("error", data)

    def test_create_ticket_invalid_association(self):
        """Test that POST returns 404 for bad association UUID."""
        response = self.client.post(
            "/api/v1/tickets/",
            data=json.dumps({
                "association_uuid": "00000000-0000-0000-0000-000000000000",
                "discord_creator_id": 123456789,
                "discord_channel_id": 987654321,
            }),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 404)

    def test_create_ticket_links_member(self):
        """Test that ticket links member when Discord ID is linked."""
        association = self.get_association()
        member = self.create_discord_linked_member(discord_id=123456789)

        response = self.client.post(
            "/api/v1/tickets/",
            data=json.dumps({
                "association_uuid": str(association.uuid),
                "discord_creator_id": 123456789,
                "discord_channel_id": 987654321,
                "subject": "Test Subject",
            }),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertIsNotNone(data["ticket"]["member"])
        self.assertEqual(data["ticket"]["member"]["uuid"], str(member.uuid))

    def test_get_ticket_by_uuid(self):
        """Test that GET /tickets/{uuid}/ returns ticket."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=123)

        response = self.client.get(
            f"/api/v1/tickets/{ticket.uuid}/",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["ticket"]["uuid"], str(ticket.uuid))

    def test_get_ticket_not_found(self):
        """Test that GET returns 404 for unknown UUID."""
        response = self.client.get(
            "/api/v1/tickets/00000000-0000-0000-0000-000000000000/",
            **self.headers,
        )

        self.assertEqual(response.status_code, 404)

    def test_update_ticket_status(self):
        """Test that PATCH updates status."""
        ticket = self.create_larpmanager_ticket(
            status=TicketStatus.OPEN,
            discord_channel_id=123,
        )

        response = self.client.patch(
            f"/api/v1/tickets/{ticket.uuid}/",
            data=json.dumps({"version": 0, "status": "working"}),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["ticket"]["status"], "working")

        # Verify in database
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, TicketStatus.WORKING)

    def test_update_ticket_priority(self):
        """Test that PATCH updates priority."""
        ticket = self.create_larpmanager_ticket(
            priority=TicketPriority.LOW,
            discord_channel_id=123,
        )

        response = self.client.patch(
            f"/api/v1/tickets/{ticket.uuid}/",
            data=json.dumps({"version": 0, "priority": "high"}),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["ticket"]["priority"], "high")

    def test_update_ticket_assignment(self):
        """Test that PATCH updates assigned_staff_discord_id."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=123)

        response = self.client.patch(
            f"/api/v1/tickets/{ticket.uuid}/",
            data=json.dumps({"version": 0, "assigned_staff_discord_id": 999888777}),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["ticket"]["assigned_staff_discord_id"], 999888777)

    def test_patch_subject_emits_channel_update(self):
        """PATCHing subject emits a channel_update event with the new subject."""
        ticket = self.create_larpmanager_ticket(subject="old subject")

        response = self.client.patch(
            f"/api/v1/tickets/{ticket.uuid}/",
            data=json.dumps({"version": 0, "subject": "new subject"}),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        event = TicketEvent.objects.get(ticket=ticket, event_type=TicketEvent.EventType.CHANNEL_UPDATE)
        self.assertEqual(event.source, TicketEvent.Source.API)
        self.assertEqual(event.payload["subject"], "new subject")

    def test_patch_returns_version(self):
        """PATCH response includes the next version for optimistic locking."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=123)

        response = self.client.patch(
            f"/api/v1/tickets/{ticket.uuid}/",
            data=json.dumps({"version": 0, "status": "working"}),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ticket"]["version"], 1)

    def test_patch_version_required(self):
        """PATCH without a version returns 400."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=123)

        response = self.client.patch(
            f"/api/v1/tickets/{ticket.uuid}/",
            data=json.dumps({"status": "working"}),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "version required")

    def test_patch_version_409(self):
        """PATCH with a stale version returns 409 with the current version."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=123)
        ticket.version = 3
        ticket.save(update_fields=["version"])

        response = self.client.patch(
            f"/api/v1/tickets/{ticket.uuid}/",
            data=json.dumps({"version": 2, "status": "working"}),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["current_version"], 3)

    def test_update_ticket_invalid_status(self):
        """Test that PATCH returns 400 for invalid status."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=123)

        response = self.client.patch(
            f"/api/v1/tickets/{ticket.uuid}/",
            data=json.dumps({"version": 0, "status": "invalid_status"}),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400)

    def test_close_ticket_success(self):
        """Test that POST /close/ sets status=done and closed_at."""
        ticket = self.create_larpmanager_ticket(
            status=TicketStatus.OPEN,
            discord_channel_id=123,
        )

        response = self.client.post(
            f"/api/v1/tickets/{ticket.uuid}/close/",
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["ticket"]["status"], "done")
        self.assertIsNotNone(data["ticket"]["closed_at"])

    def test_close_ticket_with_transcript(self):
        """Test that POST /close/ saves transcript."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=123)
        transcript = "# Transcript\n[2024-01-01] User: Hello"

        response = self.client.post(
            f"/api/v1/tickets/{ticket.uuid}/close/",
            data=json.dumps({"transcript": transcript}),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["ticket"]["transcript"], transcript)

    def test_reopen_ticket_success(self):
        """Test that POST /reopen/ sets status=open."""
        ticket = self.create_larpmanager_ticket(
            status=TicketStatus.DONE,
            discord_channel_id=123,
        )

        response = self.client.post(
            f"/api/v1/tickets/{ticket.uuid}/reopen/",
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["ticket"]["status"], "open")

    def test_reopen_ticket_not_closed(self):
        """Test that POST /reopen/ returns 400 if ticket is not done."""
        ticket = self.create_larpmanager_ticket(
            status=TicketStatus.OPEN,
            discord_channel_id=123,
        )

        response = self.client.post(
            f"/api/v1/tickets/{ticket.uuid}/reopen/",
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400)

    def test_get_ticket_by_channel(self):
        """Test that GET /channel/{id}/ returns ticket."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=123456789)

        response = self.client.get(
            "/api/v1/tickets/channel/123456789/",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["ticket"]["uuid"], str(ticket.uuid))

    def test_get_ticket_by_channel_not_found(self):
        """Test that GET /channel/{id}/ returns 404 for unknown channel."""
        response = self.client.get(
            "/api/v1/tickets/channel/999999999/",
            **self.headers,
        )

        self.assertEqual(response.status_code, 404)

    def test_list_associations(self):
        """Test that GET /associations/ returns list."""
        # Ensure we have at least one association
        self.get_association()

        response = self.client.get(
            "/api/v1/associations/",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("associations", data)
        self.assertGreater(len(data["associations"]), 0)

    @override_settings(DISCORD_BOT_API_KEYS=["rate-limit-test-key"])
    def test_429_retry_after(self):
        """Test that exceeding the rate limit returns 429 with Retry-After."""
        association = self.get_association()
        headers = {"HTTP_X_API_KEY": "rate-limit-test-key"}
        for index in range(5):
            response = self.client.post(
                "/api/v1/tickets/",
                data=json.dumps({
                    "association_uuid": str(association.uuid),
                    "discord_creator_id": 424242,
                    "discord_channel_id": 7000 + index,
                }),
                content_type="application/json",
                **headers,
            )
            self.assertEqual(response.status_code, 201)

        response = self.client.post(
            "/api/v1/tickets/",
            data=json.dumps({
                "association_uuid": str(association.uuid),
                "discord_creator_id": 424242,
                "discord_channel_id": 7999,
            }),
            content_type="application/json",
            **headers,
        )

        self.assertEqual(response.status_code, 429)
        data = response.json()
        self.assertEqual(data["error"], "rate limited")
        self.assertGreaterEqual(data["retry_after"], 1)
        self.assertIn("Retry-After", response.headers)

    def test_401_before_429(self):
        """Test that unauthenticated requests return 401 even when rate limited."""
        for _attempt in range(6):
            response = self.client.post(
                "/api/v1/tickets/",
                data=json.dumps({}),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 401)

    def test_non_numeric_400(self):
        """Test that a non-numeric list filter returns 400."""
        self.create_larpmanager_ticket(discord_creator_id=12345, discord_channel_id=111)

        response = self.client.get(
            "/api/v1/tickets/?discord_creator_id=abc",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid discord_creator_id")

    def test_malformed_close_400_no_mutation(self):
        """Test that malformed JSON on close returns 400 without closing."""
        ticket = self.create_larpmanager_ticket(status=TicketStatus.OPEN, discord_channel_id=123)

        response = self.client.post(
            f"/api/v1/tickets/{ticket.uuid}/close/",
            data="{invalid json",
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, TicketStatus.OPEN)
        self.assertIsNone(ticket.closed_at)

    def test_transcript_in_patch_400(self):
        """Test that PATCH with transcript returns 400."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=123)

        response = self.client.patch(
            f"/api/v1/tickets/{ticket.uuid}/",
            data=json.dumps({"transcript": "should not be writable"}),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400)

    def test_cap_enforcement(self):
        """Test that creating a 6th open ticket for a creator returns 400."""
        association = self.get_association()
        for index in range(5):
            self.create_larpmanager_ticket(
                discord_creator_id=555000,
                discord_channel_id=2000 + index,
            )

        response = self.client.post(
            "/api/v1/tickets/",
            data=json.dumps({
                "association_uuid": str(association.uuid),
                "discord_creator_id": 555000,
                "discord_channel_id": 3000,
            }),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertEqual(data["error"], "ticket cap exceeded")
        self.assertEqual(data["max_open_tickets"], 5)

    @override_settings(DISCORD_BOT_API_KEYS=["pii-bot-key"])
    def test_pii_gating(self):
        """Test that ticket email is exposed only to the bot key or members:read scope."""
        ticket = self.create_larpmanager_ticket(
            discord_channel_id=111,
            email="requester@example.com",
        )

        bot_response = self.client.get(
            f"/api/v1/tickets/{ticket.uuid}/",
            HTTP_X_API_KEY="pii-bot-key",
        )
        self.assertEqual(bot_response.status_code, 200)
        self.assertEqual(bot_response.json()["ticket"]["email"], "requester@example.com")

        publisher_response = self.client.get(
            f"/api/v1/tickets/{ticket.uuid}/",
            **self.headers,
        )
        self.assertEqual(publisher_response.status_code, 200)
        self.assertNotIn("email", publisher_response.json()["ticket"])

        # Positive case: a publisher key with members:read scope does get the email.
        PublisherApiKey.objects.create(
            name="Members Read Key",
            key="members-read-key",
            active=True,
            scopes=["tickets:read", "members:read"],
        )
        members_read_response = self.client.get(
            f"/api/v1/tickets/{ticket.uuid}/",
            HTTP_X_API_KEY="members-read-key",
        )
        self.assertEqual(members_read_response.status_code, 200)
        self.assertEqual(members_read_response.json()["ticket"]["email"], "requester@example.com")

    def test_scope_enforcement(self):
        """Test that a read-only key can GET but not PATCH or DELETE."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=123)
        PublisherApiKey.objects.create(
            name="Read Only",
            key="read-only-key",
            active=True,
            scopes=["tickets:read"],
        )
        headers = {"HTTP_X_API_KEY": "read-only-key"}

        response = self.client.get(f"/api/v1/tickets/{ticket.uuid}/", **headers)
        self.assertEqual(response.status_code, 200)

        response = self.client.patch(
            f"/api/v1/tickets/{ticket.uuid}/",
            data=json.dumps({"version": 0, "status": "working"}),
            content_type="application/json",
            **headers,
        )
        self.assertEqual(response.status_code, 403)

        response = self.client.delete(f"/api/v1/tickets/{ticket.uuid}/", **headers)
        self.assertEqual(response.status_code, 403)

    @override_settings(DISCORD_TICKET_API_ALLOW_PUBLISHER_FALLBACK=False)
    def test_fallback_deprecation_flag(self):
        """Test that a legacy scope-less publisher key is rejected when the fallback is off."""
        PublisherApiKey.objects.create(
            name="Legacy Key",
            key="legacy-key",
            active=True,
        )

        response = self.client.get("/api/v1/tickets/", HTTP_X_API_KEY="legacy-key")
        self.assertEqual(response.status_code, 401)

    def test_association_scope_isolation(self):
        """Test that an association-scoped key cannot see another association's ticket."""
        assoc_a = self.get_association()
        assoc_b = self.create_association(name="Other Association", slug="other-association")
        ticket_a = self.create_larpmanager_ticket(association=assoc_a, discord_channel_id=111)
        ticket_b = self.create_larpmanager_ticket(association=assoc_b, discord_channel_id=222)

        PublisherApiKey.objects.create(
            name="Scoped Key",
            key="scoped-key",
            active=True,
            scopes=["tickets:read"],
            association=assoc_a,
        )
        headers = {"HTTP_X_API_KEY": "scoped-key"}

        response = self.client.get(f"/api/v1/tickets/{ticket_a.uuid}/", **headers)
        self.assertEqual(response.status_code, 200)

        response = self.client.get(f"/api/v1/tickets/{ticket_b.uuid}/", **headers)
        self.assertEqual(response.status_code, 404)

    def test_create_duplicate_channel_id(self):
        """Test that POST with a duplicate discord_channel_id returns the existing ticket."""
        association = self.get_association()
        existing = self.create_larpmanager_ticket(discord_channel_id=987654321, association=association)

        response = self.client.post(
            "/api/v1/tickets/",
            data=json.dumps({
                "association_uuid": str(association.uuid),
                "discord_creator_id": 123456789,
                "discord_channel_id": 987654321,
                "subject": "Duplicate",
            }),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ticket"]["uuid"], str(existing.uuid))

    def test_non_uuid_slug_404(self):
        """Test that a non-UUID slug returns 404 rather than 500."""
        response = self.client.get("/api/v1/tickets/not-a-uuid/", **self.headers)
        self.assertEqual(response.status_code, 404)

    def test_idempotency_key(self):
        """A replayed create with the same idempotency key returns the existing ticket."""
        association = self.get_association()
        payload = {
            "association_uuid": str(association.uuid),
            "discord_creator_id": 123456789,
            "discord_channel_id": 998877665,
            "subject": "Idempotent",
        }

        first = self.client.post(
            "/api/v1/tickets/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="key-abc-123",
            **self.headers,
        )
        self.assertEqual(first.status_code, 201)

        # Header replayed -> same ticket, no second row.
        second = self.client.post(
            "/api/v1/tickets/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="key-abc-123",
            **self.headers,
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["ticket"]["uuid"], first.json()["ticket"]["uuid"])

        # client_uuid body fallback dedupes too.
        third = self.client.post(
            "/api/v1/tickets/",
            data=json.dumps({**payload, "client_uuid": "key-abc-123"}),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(third.status_code, 200)
        self.assertEqual(third.json()["ticket"]["uuid"], first.json()["ticket"]["uuid"])

        self.assertEqual(LarpManagerTicket.objects.filter(discord_channel_id=998877665).count(), 1)

    def test_create_201_poll_contract(self):
        """Create without discord_channel_id returns 201, null channel, and a channel_create event."""
        association = self.get_association()

        response = self.client.post(
            "/api/v1/tickets/",
            data=json.dumps({
                "association_uuid": str(association.uuid),
                "discord_creator_id": 123456789,
                "subject": "No channel yet",
                "client_uuid": "poll-contract-key",
            }),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertIsNone(data["ticket"]["discord_channel_id"])

        ticket = LarpManagerTicket.objects.get(uuid=data["ticket"]["uuid"])
        self.assertIsNone(ticket.discord_channel_id)
        self.assertEqual(ticket.idempotency_key, "poll-contract-key")

        event = TicketEvent.objects.get(ticket=ticket, event_type=TicketEvent.EventType.CHANNEL_CREATE)
        self.assertEqual(event.source, TicketEvent.Source.API)
        self.assertEqual(event.payload["ticket_uuid"], str(ticket.uuid))
        self.assertEqual(event.payload["subject"], "No channel yet")
        self.assertEqual(event.payload["association_uuid"], str(association.uuid))
        self.assertEqual(event.payload["discord_creator_id"], 123456789)
        # Undelivered so the bot outbox serves it; the bot writes the channel back.
        self.assertIsNone(event.applied_at)
        self.assertIsNone(event.acked_at)

    def test_patch_archives(self):
        """DELETE soft-deletes the ticket and emits a channel_archive event for the bot."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=555666)

        response = self.client.delete(f"/api/v1/tickets/{ticket.uuid}/", **self.headers)
        self.assertEqual(response.status_code, 200)

        ticket.refresh_from_db()
        self.assertIsNotNone(ticket.deleted)

        event = TicketEvent.objects.get(ticket=ticket, event_type=TicketEvent.EventType.CHANNEL_ARCHIVE)
        self.assertEqual(event.source, TicketEvent.Source.API)
        self.assertEqual(event.payload["discord_channel_id"], 555666)

    def test_audit_log_presence(self):
        """Test that every ticket API call records an audit log entry."""
        from larpmanager.models.miscellanea import Log

        ticket = self.create_larpmanager_ticket(discord_channel_id=123)
        before = Log.objects.filter(cls="PublisherAPI").count()

        response = self.client.get(f"/api/v1/tickets/{ticket.uuid}/", **self.headers)
        self.assertEqual(response.status_code, 200)

        after = Log.objects.filter(cls="PublisherAPI").count()
        self.assertGreater(after, before)

    def test_unauthorized_web_get_logs_denial(self):
        """A non-admin member GETting another member's ticket 404s and logs access_denied."""
        from django.contrib.auth import get_user_model

        user_model = get_user_model()
        requester = user_model.objects.get(username="user@test.it")
        owner = user_model.objects.get(username="player@test.it")
        ticket = self.create_larpmanager_ticket(member=owner.member)

        self.client.force_login(requester)
        response = self.client.get(f"/tickets/{ticket.uuid}/")

        self.assertEqual(response.status_code, 404)
        event = TicketEvent.objects.get(ticket=ticket, event_type=TicketEvent.EventType.ACCESS_DENIED)
        self.assertEqual(event.source, TicketEvent.Source.API)
        self.assertEqual(event.actor_member, requester.member)
        self.assertEqual(event.payload["path"], f"/tickets/{ticket.uuid}/")
        self.assertIn("ip", event.payload)
        self.assertIsNotNone(event.applied_at)
        self.assertIsNotNone(event.acked_at)


@override_settings(DISCORD_BOT_API_KEYS=["test-bot-key"])
class TestTicketEventsAPI(BaseTestCase):
    """Tests for the outbox, ack, history, and outbound endpoints."""

    def setUp(self):
        """Set up test fixtures."""
        super().setUp()
        cache.clear()
        self.client = Client()
        self.api_key = PublisherApiKey.objects.create(
            name="Test Key",
            key="test-api-key-12345",
            active=True,
            scopes=["tickets:read", "tickets:write"],
        )
        self.headers = {"HTTP_X_API_KEY": "test-api-key-12345"}
        self.bot_headers = {"HTTP_X_API_KEY": "test-bot-key"}

    def test_outbox_cursor_pagination(self):
        """Outbox returns only unacked source=api events in id order; since does not filter."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=111)
        first = emit_ticket_event(
            ticket, TicketEvent.EventType.STATUS_CHANGED, source="api", from_status="open", to_status="working"
        )
        second = emit_ticket_event(
            ticket, TicketEvent.EventType.STATUS_CHANGED, source="api", from_status="working", to_status="done"
        )
        emit_ticket_event(
            ticket, TicketEvent.EventType.STATUS_CHANGED, source="discord", from_status="open", to_status="working"
        )

        response = self.client.get("/api/v1/tickets/events/?since=0&limit=10", **self.bot_headers)
        self.assertEqual(response.status_code, 200)
        ids = [event["id"] for event in response.json()["events"]]
        self.assertEqual(ids, [first.id, second.id])

        # ``since`` is accepted for backward-compat but does not filter; the NULL
        # predicates alone drive delivery, so the still-unacked first event is
        # re-delivered even with since=first.id (at-least-once).
        response = self.client.get(f"/api/v1/tickets/events/?since={first.id}&limit=1", **self.bot_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([event["id"] for event in response.json()["events"]], [first.id])

    def test_ack_idempotent(self):
        """Test that acking an event twice is harmless and idempotent."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=112)
        event = emit_ticket_event(
            ticket, TicketEvent.EventType.STATUS_CHANGED, source="api", from_status="open", to_status="working"
        )

        response = self.client.post(
            "/api/v1/tickets/events/ack/",
            data=json.dumps({"ids": [event.id]}),
            content_type="application/json",
            **self.bot_headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["acked"], 1)

        event.refresh_from_db()
        self.assertIsNotNone(event.acked_at)
        self.assertIsNotNone(event.applied_at)

        response = self.client.post(
            "/api/v1/tickets/events/ack/",
            data=json.dumps({"ids": [event.id]}),
            content_type="application/json",
            **self.bot_headers,
        )
        self.assertEqual(response.status_code, 200)
        event.refresh_from_db()
        self.assertIsNotNone(event.acked_at)

    def test_ack_batch(self):
        """Test that multiple event ids can be acked in a single request."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=113)
        events = [
            emit_ticket_event(
                ticket, TicketEvent.EventType.STATUS_CHANGED, source="api", from_status="open", to_status="working"
            )
            for _ in range(3)
        ]
        ids = [event.id for event in events]

        response = self.client.post(
            "/api/v1/tickets/events/ack/",
            data=json.dumps({"ids": ids}),
            content_type="application/json",
            **self.bot_headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["acked"], 3)

        for event in events:
            event.refresh_from_db()
            self.assertIsNotNone(event.applied_at)
            self.assertIsNotNone(event.acked_at)

    def test_outbound_upsert_atomic(self):
        """Test that a repeated outbound post upserts rather than duplicating."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=222)
        payload = {
            "discord_channel_id": 222,
            "discord_message_id": 123456789,
            "author_discord_id": 42,
            "author_name": "Alice",
            "content": "hello",
            "sent_at": "2026-01-01T00:00:00+00:00",
            "attachments": [],
            "is_bot": False,
        }

        response = self.client.post(
            "/api/v1/tickets/outbound/", data=json.dumps(payload), content_type="application/json", **self.bot_headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TicketMessage.objects.filter(discord_message_id=123456789).count(), 1)

        payload["content"] = "updated"
        response = self.client.post(
            "/api/v1/tickets/outbound/", data=json.dumps(payload), content_type="application/json", **self.bot_headers
        )
        self.assertEqual(response.status_code, 200)

        messages = TicketMessage.objects.filter(discord_message_id=123456789)
        self.assertEqual(messages.count(), 1)
        self.assertEqual(messages.first().content, "updated")
        self.assertIsNotNone(messages.first().search_vector)

        ticket.refresh_from_db()
        self.assertEqual(ticket.last_synced_message_id, 123456789)
        self.assertEqual(ticket.version, 0)

    def test_snapshot_append_atomic(self):
        """Two outbound messages append two rendered lines; an edit does not duplicate."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=555)
        base = {
            "discord_channel_id": 555,
            "discord_message_id": 1001,
            "author_name": "Alice",
            "content": "first message",
            "sent_at": "2026-01-01T00:00:00+00:00",
            "attachments": [],
            "is_bot": False,
        }
        response = self.client.post(
            "/api/v1/tickets/outbound/", data=json.dumps(base), content_type="application/json", **self.bot_headers
        )
        self.assertEqual(response.status_code, 200)

        second = dict(
            base,
            discord_message_id=1002,
            author_name="Bob",
            content="second message",
            sent_at="2026-01-01T00:01:00+00:00",
        )
        response = self.client.post(
            "/api/v1/tickets/outbound/", data=json.dumps(second), content_type="application/json", **self.bot_headers
        )
        self.assertEqual(response.status_code, 200)

        ticket.refresh_from_db()
        self.assertIn("[2026-01-01 00:00:00] Alice first message", ticket.transcript)
        self.assertIn("[2026-01-01 00:01:00] Bob second message", ticket.transcript)

        # Re-posting the first message as an edit must not append a duplicate line.
        edited = dict(base, content="first message edited")
        response = self.client.post(
            "/api/v1/tickets/outbound/", data=json.dumps(edited), content_type="application/json", **self.bot_headers
        )
        self.assertEqual(response.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.transcript.count("Alice first message"), 1)
        self.assertNotIn("first message edited", ticket.transcript)

    def test_snapshot_does_not_bump_version(self):
        """An outbound message append does not bump the optimistic-lock version."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=666)
        self.assertEqual(ticket.version, 0)

        response = self.client.post(
            "/api/v1/tickets/outbound/",
            data=json.dumps({
                "discord_channel_id": 666,
                "discord_message_id": 2001,
                "author_name": "Carol",
                "content": "hello",
                "sent_at": "2026-01-02T00:00:00+00:00",
                "attachments": [],
                "is_bot": False,
            }),
            content_type="application/json",
            **self.bot_headers,
        )
        self.assertEqual(response.status_code, 200)

        ticket.refresh_from_db()
        self.assertEqual(ticket.version, 0)
        self.assertIsNotNone(ticket.transcript)

    def test_message_search_open_ticket(self):
        """?q= finds a word from a message in an OPEN ticket via message search_vector."""
        ticket = self.create_larpmanager_ticket(
            subject="Unrelated subject", discord_channel_id=777, status=TicketStatus.OPEN
        )
        response = self.client.post(
            "/api/v1/tickets/outbound/",
            data=json.dumps({
                "discord_channel_id": 777,
                "discord_message_id": 3001,
                "author_name": "Carol",
                "content": "please check the xylophone inventory",
                "sent_at": "2026-01-02T00:00:00+00:00",
                "attachments": [],
                "is_bot": False,
            }),
            content_type="application/json",
            **self.bot_headers,
        )
        self.assertEqual(response.status_code, 200)

        response = self.client.get("/api/v1/tickets/?q=xylophone", **self.headers)
        self.assertEqual(response.status_code, 200)
        uuids = [ticket_data["uuid"] for ticket_data in response.json()["tickets"]]
        self.assertIn(str(ticket.uuid), uuids)

    def test_close_does_not_truncate_snapshot(self):
        """Close ignores a transcript body when the ticket already has message rows."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=888, status=TicketStatus.OPEN)
        self.client.post(
            "/api/v1/tickets/outbound/",
            data=json.dumps({
                "discord_channel_id": 888,
                "discord_message_id": 4001,
                "author_name": "Dave",
                "content": "keep me",
                "sent_at": "2026-01-03T00:00:00+00:00",
                "attachments": [],
                "is_bot": False,
            }),
            content_type="application/json",
            **self.bot_headers,
        )
        ticket.refresh_from_db()
        before = ticket.transcript
        self.assertIsNotNone(before)

        response = self.client.post(
            f"/api/v1/tickets/{ticket.uuid}/close/",
            data=json.dumps({"transcript": "# TRUNCATED"}),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 200)

        ticket.refresh_from_db()
        self.assertEqual(ticket.transcript, before)
        self.assertNotIn("TRUNCATED", ticket.transcript)

    def test_event_source_split(self):
        """Test that the outbox excludes source=discord but history includes all sources."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=333)
        api_event = emit_ticket_event(
            ticket, TicketEvent.EventType.STATUS_CHANGED, source="api", from_status="open", to_status="working"
        )
        discord_event = emit_ticket_event(
            ticket, TicketEvent.EventType.STATUS_CHANGED, source="discord", from_status="open", to_status="working"
        )

        response = self.client.get("/api/v1/tickets/events/?since=0&limit=10", **self.bot_headers)
        self.assertEqual(response.status_code, 200)
        outbox_ids = [event["id"] for event in response.json()["events"]]
        self.assertEqual(outbox_ids, [api_event.id])
        self.assertNotIn(discord_event.id, outbox_ids)

        response = self.client.get(f"/api/v1/tickets/{ticket.uuid}/events/", **self.headers)
        self.assertEqual(response.status_code, 200)
        history_ids = [event["id"] for event in response.json()["events"]]
        self.assertEqual(set(history_ids), {api_event.id, discord_event.id})

    def test_history_on_api_mutation(self):
        """A PATCH status change emits a history event with actor and from/to status."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=334, assigned_staff_discord_id=424242)

        response = self.client.patch(
            f"/api/v1/tickets/{ticket.uuid}/",
            data=json.dumps({"version": 0, "status": "working"}),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 200)

        response = self.client.get(f"/api/v1/tickets/{ticket.uuid}/events/", **self.headers)
        self.assertEqual(response.status_code, 200)
        status_events = [
            event
            for event in response.json()["events"]
            if event["event_type"] == TicketEvent.EventType.STATUS_CHANGED
        ]
        self.assertEqual(len(status_events), 1)
        event = status_events[0]
        self.assertEqual(event["from_status"], TicketStatus.OPEN)
        self.assertEqual(event["to_status"], TicketStatus.WORKING)
        self.assertEqual(event["actor_discord_id"], 424242)
        self.assertEqual(event["actor"], "424242")

    def test_version_bump_on_patch(self):
        """Test that a PATCH mutation bumps the optimistic-lock version."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=444)
        self.assertEqual(ticket.version, 0)

        response = self.client.patch(
            f"/api/v1/tickets/{ticket.uuid}/",
            data=json.dumps({"version": 0, "status": "working", "priority": "high"}),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 200)

        ticket.refresh_from_db()
        self.assertEqual(ticket.version, 1)

    def test_outbox_cursor_no_stranding(self):
        """Acked higher ids must not strand a lower un-applied event."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=114)
        events = [
            emit_ticket_event(
                ticket, TicketEvent.EventType.STATUS_CHANGED, source="api", from_status="open", to_status="working"
            )
            for _ in range(3)
        ]
        low, mid, high = events

        # Ack the two highest ids only, leaving the lowest un-applied.
        response = self.client.post(
            "/api/v1/tickets/events/ack/",
            data=json.dumps({"ids": [mid.id, high.id]}),
            content_type="application/json",
            **self.bot_headers,
        )
        self.assertEqual(response.status_code, 200)

        # A fresh poll with since=high.id must still return the stranded low event.
        response = self.client.get(f"/api/v1/tickets/events/?since={high.id}&limit=10", **self.bot_headers)
        self.assertEqual(response.status_code, 200)
        ids = [event["id"] for event in response.json()["events"]]
        self.assertIn(low.id, ids)
        self.assertNotIn(mid.id, ids)
        self.assertNotIn(high.id, ids)

    def test_history_endpoint_soft_deleted(self):
        """History is still returned after a ticket is soft-deleted."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=445)
        event = emit_ticket_event(
            ticket, TicketEvent.EventType.STATUS_CHANGED, source="api", from_status="open", to_status="working"
        )

        response = self.client.delete(f"/api/v1/tickets/{ticket.uuid}/", **self.headers)
        self.assertEqual(response.status_code, 200)

        ticket.refresh_from_db()
        self.assertIsNotNone(ticket.deleted)

        response = self.client.get(f"/api/v1/tickets/{ticket.uuid}/events/", **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn(event.id, [e["id"] for e in response.json()["events"]])

    def test_ack_ids_cap(self):
        """Acking more than 1000 ids is rejected with 400."""
        response = self.client.post(
            "/api/v1/tickets/events/ack/",
            data=json.dumps({"ids": list(range(1001))}),
            content_type="application/json",
            **self.bot_headers,
        )
        self.assertEqual(response.status_code, 400)

    def test_outbound_author_name_too_long(self):
        """Outbound author_name over 255 chars is rejected with 400."""
        self.create_larpmanager_ticket(discord_channel_id=446)
        payload = {
            "discord_channel_id": 446,
            "discord_message_id": 11223344,
            "author_name": "x" * 256,
            "content": "hello",
            "attachments": [],
            "is_bot": False,
        }
        response = self.client.post(
            "/api/v1/tickets/outbound/", data=json.dumps(payload), content_type="application/json", **self.bot_headers
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(TicketMessage.objects.filter(discord_message_id=11223344).count(), 0)

    def test_outbound_is_bot_string_false(self):
        """A string "false" for is_bot is parsed as False, not True."""
        self.create_larpmanager_ticket(discord_channel_id=447)
        payload = {
            "discord_channel_id": 447,
            "discord_message_id": 55667788,
            "author_name": "Bob",
            "content": "hello",
            "attachments": [],
            "is_bot": "false",
        }
        response = self.client.post(
            "/api/v1/tickets/outbound/", data=json.dumps(payload), content_type="application/json", **self.bot_headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(TicketMessage.objects.get(discord_message_id=55667788).is_bot)

    def test_channel_writeback_endpoint(self):
        """Bot key writes discord_channel_id without bumping version; scoped key is forbidden."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=666)
        self.assertEqual(ticket.version, 0)

        response = self.client.post(
            f"/api/v1/tickets/{ticket.uuid}/channel/",
            data=json.dumps({"discord_channel_id": 777}),
            content_type="application/json",
            **self.bot_headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ticket"]["discord_channel_id"], 777)

        ticket.refresh_from_db()
        self.assertEqual(ticket.discord_channel_id, 777)
        self.assertEqual(ticket.version, 0)

        response = self.client.post(
            f"/api/v1/tickets/{ticket.uuid}/channel/",
            data=json.dumps({"discord_channel_id": 888}),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 403)

    def test_delete_handler(self):
        """A deleted:true outbound soft-deletes the message and acks (200)."""
        self.create_larpmanager_ticket(discord_channel_id=910)
        self.client.post(
            "/api/v1/tickets/outbound/",
            data=json.dumps({
                "discord_channel_id": 910,
                "discord_message_id": 9001,
                "author_name": "Alice",
                "content": "to be deleted",
                "attachments": [],
                "is_bot": False,
            }),
            content_type="application/json",
            **self.bot_headers,
        )
        self.assertEqual(TicketMessage.objects.filter(discord_message_id=9001).count(), 1)

        response = self.client.post(
            "/api/v1/tickets/outbound/",
            data=json.dumps({"discord_channel_id": 910, "discord_message_id": 9001, "deleted": True}),
            content_type="application/json",
            **self.bot_headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["deleted"])

        # Soft-deleted: hidden from the default manager, present in all_objects.
        self.assertFalse(TicketMessage.objects.filter(discord_message_id=9001).exists())
        deleted_message = TicketMessage.all_objects.get(discord_message_id=9001)
        self.assertIsNotNone(deleted_message.deleted)

        # A non-bool deleted value is rejected before any lookup.
        response = self.client.post(
            "/api/v1/tickets/outbound/",
            data=json.dumps({"discord_channel_id": 910, "discord_message_id": 9002, "deleted": "true"}),
            content_type="application/json",
            **self.bot_headers,
        )
        self.assertEqual(response.status_code, 400)

        # An unknown channel still 404s on the delete branch.
        response = self.client.post(
            "/api/v1/tickets/outbound/",
            data=json.dumps({"discord_channel_id": 999999999, "discord_message_id": 9003, "deleted": True}),
            content_type="application/json",
            **self.bot_headers,
        )
        self.assertEqual(response.status_code, 404)

    def test_transcript_endpoint(self):
        """GET /tickets/<uuid>/transcript/ returns transcript and rows oldest-first."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=920)
        for message_id, author, content, sent_at, is_bot in [
            (9101, "Alice", "first", "2026-01-01T00:00:00+00:00", False),
            (9102, "Bob", "second", "2026-01-01T00:01:00+00:00", True),
        ]:
            response = self.client.post(
                "/api/v1/tickets/outbound/",
                data=json.dumps({
                    "discord_channel_id": 920,
                    "discord_message_id": message_id,
                    "author_name": author,
                    "content": content,
                    "sent_at": sent_at,
                    "attachments": [],
                    "is_bot": is_bot,
                }),
                content_type="application/json",
                **self.bot_headers,
            )
            self.assertEqual(response.status_code, 200)

        response = self.client.get(f"/api/v1/tickets/{ticket.uuid}/transcript/", **self.headers)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("[2026-01-01 00:00:00] Alice first", data["transcript"])
        self.assertIn("[2026-01-01 00:01:00] Bob second", data["transcript"])
        self.assertEqual([message["discord_message_id"] for message in data["messages"]], [9101, 9102])
        self.assertEqual(data["messages"][0]["author_name"], "Alice")
        self.assertFalse(data["messages"][0]["is_bot"])
        self.assertTrue(data["messages"][1]["is_bot"])

        # Bot keys are also allowed.
        bot_response = self.client.get(f"/api/v1/tickets/{ticket.uuid}/transcript/", **self.bot_headers)
        self.assertEqual(bot_response.status_code, 200)

    def test_no_fallback_after_delete(self):
        """A ticket that had messages but now has zero live rows returns '' not the snapshot."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=930, transcript="legacy snapshot")
        self.client.post(
            "/api/v1/tickets/outbound/",
            data=json.dumps({
                "discord_channel_id": 930,
                "discord_message_id": 9201,
                "author_name": "Alice",
                "content": "will be deleted",
                "attachments": [],
                "is_bot": False,
            }),
            content_type="application/json",
            **self.bot_headers,
        )

        # The watermark is now set; deleting the only message leaves zero live rows.
        self.client.post(
            "/api/v1/tickets/outbound/",
            data=json.dumps({"discord_channel_id": 930, "discord_message_id": 9201, "deleted": True}),
            content_type="application/json",
            **self.bot_headers,
        )

        ticket.refresh_from_db()
        self.assertIsNotNone(ticket.last_synced_message_id)
        self.assertEqual(ticket.build_transcript(), "")

    def test_legacy_transcript_fallback(self):
        """A legacy ticket with no watermark still falls back to the stored snapshot."""
        ticket = self.create_larpmanager_ticket(transcript="legacy snapshot")
        self.assertIsNone(ticket.last_synced_message_id)
        self.assertEqual(ticket.build_transcript(), "legacy snapshot")


@override_settings(DISCORD_BOT_API_KEYS=["test-bot-key"])
@pytest.mark.django_db(transaction=True)
class TestTicketOutboundConcurrency(BaseTestCase):
    """Outbound upsert dedupes to one row via the unique discord_message_id."""

    def test_outbound_concurrent_duplicate(self):
        """Two posts of one message id collapse to a single row (unique constraint)."""
        cache.clear()
        association = Association.objects.create(name="Concurrent Org", slug="concurrent-org", main_mail="c@example.com")
        LarpManagerTicket.objects.create(
            association=association,
            reason="Test",
            content="content",
            status=TicketStatus.OPEN,
            priority=TicketPriority.LOW,
            discord_channel_id=555,
        )

        payload = {
            "discord_channel_id": 555,
            "discord_message_id": 999888777,
            "author_discord_id": 1,
            "author_name": "User",
            "content": "concurrent",
            "sent_at": "2026-01-01T00:00:00+00:00",
            "attachments": [],
            "is_bot": False,
        }

        client = Client()
        first = client.post(
            "/api/v1/tickets/outbound/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_API_KEY="test-bot-key",
        )
        second = client.post(
            "/api/v1/tickets/outbound/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_API_KEY="test-bot-key",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(TicketMessage.objects.filter(discord_message_id=999888777).count(), 1)
