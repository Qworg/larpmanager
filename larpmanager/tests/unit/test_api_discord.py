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
"""Tests for Discord OAuth and linking API endpoints."""

from unittest.mock import patch

from django.test import Client, override_settings

from larpmanager.models.base import PublisherApiKey
from larpmanager.models.member import MemberConfig
from larpmanager.tests.unit.base import BaseTestCase
from larpmanager.views.api_discord import get_member_by_discord_id, link_discord_to_member


class TestDiscordAPI(BaseTestCase):
    """Test cases for the Discord OAuth and linking API."""

    def setUp(self):
        """Set up test fixtures."""
        super().setUp()
        self.client = Client()
        self.api_key = PublisherApiKey.objects.create(
            name="Test Key",
            key="test-api-key-12345",
            active=True,
        )
        self.headers = {"HTTP_X_API_KEY": "test-api-key-12345"}

    def test_member_check_linked_returns_true(self):
        """Test that member check returns linked=true with member data."""
        member = self.create_discord_linked_member(discord_id=123456789)

        response = self.client.get(
            "/api/v1/discord/member/123456789/",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["linked"])
        self.assertIn("member", data)
        self.assertEqual(data["member"]["uuid"], str(member.uuid))

    def test_member_check_not_linked_returns_false(self):
        """Test that member check returns linked=false when not linked."""
        response = self.client.get(
            "/api/v1/discord/member/999999999/",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["linked"])
        self.assertNotIn("member", data)

    def test_member_check_requires_auth(self):
        """Test that member check returns 401 without API key."""
        response = self.client.get("/api/v1/discord/member/123456789/")

        self.assertEqual(response.status_code, 401)

    @override_settings(
        DISCORD_CLIENT_ID="test-client-id",
        DISCORD_CLIENT_SECRET="test-secret",
        DISCORD_REDIRECT_URI="http://localhost/callback",
    )
    def test_get_oauth_url_success(self):
        """Test that OAuth URL endpoint returns valid URL with state."""
        response = self.client.get(
            "/api/v1/discord/link/123456789/",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("oauth_url", data)
        self.assertIn("discord.com", data["oauth_url"])
        self.assertIn("client_id=test-client-id", data["oauth_url"])
        self.assertIn("state=", data["oauth_url"])

    @override_settings(DISCORD_CLIENT_ID="")
    def test_get_oauth_url_missing_config(self):
        """Test that OAuth URL returns 500 if CLIENT_ID not set."""
        response = self.client.get(
            "/api/v1/discord/link/123456789/",
            **self.headers,
        )

        self.assertEqual(response.status_code, 500)
        data = response.json()
        self.assertIn("error", data)

    def test_link_discord_to_member_creates_config(self):
        """Test that linking creates MemberConfig entry."""
        member = self.create_member()
        discord_id = 111222333

        config = link_discord_to_member(member, discord_id)

        self.assertEqual(config.member, member)
        self.assertEqual(config.name, "discord_id")
        self.assertEqual(config.value, str(discord_id))

        # Verify it's in the database
        found_config = MemberConfig.objects.get(member=member, name="discord_id")
        self.assertEqual(found_config.value, str(discord_id))

    def test_link_discord_to_member_updates_existing(self):
        """Test that linking updates existing config."""
        member = self.create_member()
        old_discord_id = 111222333
        new_discord_id = 444555666

        # Create initial link
        link_discord_to_member(member, old_discord_id)

        # Update link
        config = link_discord_to_member(member, new_discord_id)

        self.assertEqual(config.value, str(new_discord_id))

        # Verify only one config exists
        configs = MemberConfig.objects.filter(member=member, name="discord_id")
        self.assertEqual(configs.count(), 1)
        self.assertEqual(configs.first().value, str(new_discord_id))

    def test_get_member_by_discord_id_found(self):
        """Test that get_member_by_discord_id returns member when linked."""
        member = self.create_discord_linked_member(discord_id=123456789)

        result = get_member_by_discord_id(123456789)

        self.assertIsNotNone(result)
        self.assertEqual(result.uuid, member.uuid)

    def test_get_member_by_discord_id_not_found(self):
        """Test that get_member_by_discord_id returns None when not linked."""
        result = get_member_by_discord_id(999999999)

        self.assertIsNone(result)

    def test_unlink_discord_success(self):
        """Test that unlink deletes MemberConfig entry."""
        self.create_discord_linked_member(discord_id=123456789)

        response = self.client.get(
            "/api/v1/discord/unlink/?discord_id=123456789",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])

        # Verify config is deleted
        result = get_member_by_discord_id(123456789)
        self.assertIsNone(result)

    def test_unlink_discord_not_found(self):
        """Test that unlink returns 404 if not linked."""
        response = self.client.get(
            "/api/v1/discord/unlink/?discord_id=999999999",
            **self.headers,
        )

        self.assertEqual(response.status_code, 404)

    def test_validate_bot_api_key_with_header(self):
        """Test that X-API-Key header validates correctly."""
        response = self.client.get(
            "/api/v1/discord/member/123456789/",
            HTTP_X_API_KEY="test-api-key-12345",
        )

        self.assertEqual(response.status_code, 200)

    def test_validate_bot_api_key_with_param(self):
        """Test that ?api_key= param validates correctly."""
        response = self.client.get(
            "/api/v1/discord/member/123456789/?api_key=test-api-key-12345",
        )

        self.assertEqual(response.status_code, 200)

    def test_validate_bot_api_key_invalid(self):
        """Test that invalid key returns 401."""
        response = self.client.get(
            "/api/v1/discord/member/123456789/",
            HTTP_X_API_KEY="invalid-key",
        )

        self.assertEqual(response.status_code, 401)
