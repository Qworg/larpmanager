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

from django.core.cache import cache
from django.test import Client, override_settings

from larpmanager.models.base import PublisherApiKey
from larpmanager.models.larpmanager import LarpManagerTicket, TicketPriority, TicketStatus
from larpmanager.tests.unit.base import BaseTestCase


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
        ticket = self.create_larpmanager_ticket(discord_channel_id=123456789)

        response = self.client.get("/api/v1/tickets/", **self.headers)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("tickets", data)
        self.assertEqual(len(data["tickets"]), 1)
        self.assertEqual(data["tickets"][0]["uuid"], str(ticket.uuid))

    def test_list_tickets_filter_by_status(self):
        """Test that ?status= filter works correctly."""
        self.create_larpmanager_ticket(status=TicketStatus.OPEN, discord_channel_id=111)
        self.create_larpmanager_ticket(status=TicketStatus.DONE, discord_channel_id=222)

        response = self.client.get("/api/v1/tickets/?status=open", **self.headers)

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
        self.create_larpmanager_ticket(discord_channel_id=111)  # Has channel
        self.create_larpmanager_ticket(discord_channel_id=None)  # No channel

        response = self.client.get(
            "/api/v1/tickets/?discord_only=true",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["tickets"]), 1)
        self.assertIsNotNone(data["tickets"][0]["discord_channel_id"])

    def test_list_tickets_pagination(self):
        """Test that ?limit= and ?offset= work correctly."""
        # Create 5 tickets
        for i in range(5):
            self.create_larpmanager_ticket(
                subject=f"Ticket {i}",
                discord_channel_id=100 + i,
            )

        # Get first 2
        response = self.client.get(
            "/api/v1/tickets/?limit=2&offset=0",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["tickets"]), 2)
        self.assertEqual(data["total"], 5)
        self.assertEqual(data["limit"], 2)
        self.assertEqual(data["offset"], 0)

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
            data=json.dumps({"status": "working"}),
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
            data=json.dumps({"priority": "high"}),
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
            data=json.dumps({"assigned_staff_discord_id": 999888777}),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["ticket"]["assigned_staff_discord_id"], 999888777)

    def test_update_ticket_invalid_status(self):
        """Test that PATCH returns 400 for invalid status."""
        ticket = self.create_larpmanager_ticket(discord_channel_id=123)

        response = self.client.patch(
            f"/api/v1/tickets/{ticket.uuid}/",
            data=json.dumps({"status": "invalid_status"}),
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
            data=json.dumps({"status": "working"}),
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

    def test_audit_log_presence(self):
        """Test that every ticket API call records an audit log entry."""
        from larpmanager.models.miscellanea import Log

        ticket = self.create_larpmanager_ticket(discord_channel_id=123)
        before = Log.objects.filter(cls="PublisherAPI").count()

        response = self.client.get(f"/api/v1/tickets/{ticket.uuid}/", **self.headers)
        self.assertEqual(response.status_code, 200)

        after = Log.objects.filter(cls="PublisherAPI").count()
        self.assertGreater(after, before)
