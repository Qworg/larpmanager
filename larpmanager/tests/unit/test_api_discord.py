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
from urllib.parse import parse_qs, urlsplit

from django.core.cache import cache
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext

from larpmanager.models.base import PublisherApiKey
from larpmanager.models.member import MemberConfig, Membership
from larpmanager.tests.unit.base import BaseTestCase
from larpmanager.views.api_discord import _make_signed_state, get_member_by_discord_id, link_discord_to_member


class TestDiscordAPI(BaseTestCase):
    """Test cases for the Discord OAuth and linking API."""

    def setUp(self):
        """Set up test fixtures."""
        super().setUp()
        self.client = Client()
        # Scoped to the default test association: discord_unlink now requires a
        # non-bot key's target member to belong to the key's own association.
        self.api_key = PublisherApiKey.objects.create(
            name="Test Key",
            key="test-api-key-12345",
            active=True,
            scopes=["tickets:write"],
            association=self.get_association(),
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

    @patch("larpmanager.views.api_discord.DISCORD_REDIRECT_URI", "http://localhost/callback")
    @patch("larpmanager.views.api_discord.DISCORD_CLIENT_SECRET", "test-secret")
    @patch("larpmanager.views.api_discord.DISCORD_CLIENT_ID", "test-client-id")
    def test_get_oauth_url_success(self):
        """Test that OAuth URL endpoint returns our own start URL with state.

        The endpoint no longer returns Discord's raw authorize URL: it points
        at our LM-hosted discord_oauth_start view instead, so the state gets
        bound to the requesting browser's session before ever reaching Discord
        (see test_api_discord.py CSRF regression tests below).
        """
        response = self.client.get(
            "/api/v1/discord/link/123456789/",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("oauth_url", data)
        self.assertIn("/discord/link/start/", data["oauth_url"])
        self.assertIn("state=", data["oauth_url"])
        self.assertNotIn("discord.com", data["oauth_url"])

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
        """Test that ?api_key= query-string auth is ignored (header-only)."""
        response = self.client.get(
            "/api/v1/discord/member/123456789/?api_key=test-api-key-12345",
        )

        self.assertEqual(response.status_code, 401)

    def test_validate_bot_api_key_invalid(self):
        """Test that invalid key returns 401."""
        response = self.client.get(
            "/api/v1/discord/member/123456789/",
            HTTP_X_API_KEY="invalid-key",
        )

        self.assertEqual(response.status_code, 401)

    def test_unlink_requires_write_scope(self):
        """Test that a read-only key cannot unlink a Discord account."""
        self.create_discord_linked_member(discord_id=123456789)
        PublisherApiKey.objects.create(
            name="Read Only Key",
            key="read-only-discord-key",
            active=True,
            scopes=["tickets:read"],
        )

        response = self.client.get(
            "/api/v1/discord/unlink/?discord_id=123456789",
            HTTP_X_API_KEY="read-only-discord-key",
        )

        self.assertEqual(response.status_code, 403)

    # -- MINE-1: OAuth account-link CSRF (state must be bound to the session) --

    @patch("larpmanager.views.api_discord.DISCORD_CLIENT_SECRET", "test-secret")
    def test_oauth_callback_rejects_state_not_in_session(self):
        """Test that a callback with a state never stored via discord_oauth_start does not link."""
        member = self.create_member()
        self.client.force_login(member.user)

        # A validly-signed state that was simply never written into this
        # session by discord_oauth_start (the session check must catch this
        # independently of signature validity).
        signed_state = _make_signed_state(123456789)
        response = self.client.get(f"/discord/callback/?code=somecode&state={signed_state}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"session expired", response.content.lower())
        self.assertIsNone(get_member_by_discord_id(123456789))

    @patch("larpmanager.views.api_discord.DISCORD_REDIRECT_URI", "http://localhost/callback")
    @patch("larpmanager.views.api_discord.DISCORD_CLIENT_ID", "test-client-id")
    @patch("larpmanager.views.api_discord.DISCORD_CLIENT_SECRET", "test-secret")
    def test_oauth_start_mints_fresh_state_different_from_caller_supplied_token(self):
        """discord_oauth_start must never echo the caller-supplied token as the OAuth state.

        Round 1 stored the raw query-string ``state`` verbatim, which meant the
        value was not secret to the browser that fetched it - anyone who knew
        it (e.g. the bot-issued link) could get it written into an unrelated
        session too. It must instead mint a brand-new, unpredictable value.
        """
        link_token = _make_signed_state(123456789)
        response = self.client.get(f"/discord/link/start/?state={link_token}")

        self.assertEqual(response.status_code, 302)
        self.assertIn("discord.com", response.url)
        session_state = self.client.session.get("discord_oauth_state")
        self.assertIsNotNone(session_state)
        self.assertNotEqual(session_state, link_token)
        self.assertTrue(session_state.startswith("123456789:"))
        redirect_state = parse_qs(urlsplit(response.url).query)["state"][0]
        self.assertEqual(redirect_state, session_state)

    @patch("larpmanager.views.api_discord.DISCORD_CLIENT_ID", "test-client-id")
    @patch("larpmanager.views.api_discord.DISCORD_CLIENT_SECRET", "test-secret")
    def test_oauth_start_rejects_invalid_link_token_signature(self):
        """Test that discord_oauth_start rejects a token whose signature doesn't verify."""
        response = self.client.get("/discord/link/start/?state=42:somenonce:badsignature")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"invalid state", response.content.lower())
        self.assertIsNone(self.client.session.get("discord_oauth_state"))

    @patch("larpmanager.views.api_discord.DISCORD_REDIRECT_URI", "http://localhost/callback")
    @patch("larpmanager.views.api_discord.DISCORD_CLIENT_SECRET", "test-secret")
    @patch("larpmanager.views.api_discord.DISCORD_CLIENT_ID", "test-client-id")
    def test_oauth_two_get_session_fixation_attack_is_blocked(self):
        """Reproduce the round-1 two-GET attack end to end and assert no link is created.

        1. Attacker gets a real bot-issued link token for their OWN discord_id
           and legitimately visits discord_oauth_start with it, capturing the
           real state Discord would echo back (without ever completing the
           callback).
        2. Attacker lures the logged-in victim to visit the SAME bot-issued
           link (seeding the victim's session), then to the callback with the
           attacker's own captured code+state.
        The victim's session must hold a freshly minted, different state, so
        the callback's session check must reject the replay and no Discord
        account may be linked to the victim.
        """
        attacker_discord_id = 555000111
        url_response = self.client.get(
            f"/api/v1/discord/link/{attacker_discord_id}/",
            **self.headers,
        )
        link_token = parse_qs(urlsplit(url_response.json()["oauth_url"]).query)["state"][0]

        # Step 1: attacker legitimately starts their own flow and captures the
        # real state Discord would be sent (without completing the callback).
        attacker_client = Client()
        start_response = attacker_client.get(f"/discord/link/start/?state={link_token}")
        self.assertEqual(start_response.status_code, 302)
        captured_state = parse_qs(urlsplit(start_response.url).query)["state"][0]

        # Step 2: attacker lures the logged-in victim to the SAME bot-issued
        # link (this seeds the victim's session with a state of its own)...
        victim = self.create_member()
        victim_client = Client()
        victim_client.force_login(victim.user)
        victim_client.get(f"/discord/link/start/?state={link_token}")

        # ...then to the callback with the attacker's own captured code+state.
        callback_response = victim_client.get(
            f"/discord/callback/?code=attacker-code&state={captured_state}",
        )

        self.assertEqual(callback_response.status_code, 200)
        self.assertIn(b"session expired", callback_response.content.lower())
        self.assertIsNone(get_member_by_discord_id(attacker_discord_id))
        self.assertFalse(
            MemberConfig.objects.filter(member=victim, name="discord_id", deleted__isnull=True).exists(),
        )

    @patch("larpmanager.views.api_discord.DISCORD_CLIENT_SECRET", "test-secret")
    def test_oauth_callback_rejects_two_part_state_when_secret_configured(self):
        """Test that an unsigned 2-part state is rejected once a signing secret is configured."""
        response = self.client.get("/discord/callback/?code=somecode&state=123456789:somenonce")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"invalid state", response.content.lower())
        self.assertIsNone(get_member_by_discord_id(123456789))

    @patch("larpmanager.views.api_discord.DISCORD_CLIENT_ID", "test-client-id")
    def test_oauth_start_rejects_unsigned_state_when_secret_not_configured(self):
        """Test that an unsigned state is rejected outright when no signing secret is configured.

        Round 3 found _parse_signed_state fell back to accepting an unsigned
        ``discord_id:nonce`` token whenever DISCORD_CLIENT_SECRET was falsy -
        which is the DEFAULT posture, since the secret is only ever set in
        dev.py (base/test/prod_example settings never reference it). That let
        anyone mint a session state for an arbitrary discord_id with no
        signature and no credentials at all. Verification must now fail
        closed instead: DISCORD_CLIENT_SECRET is deliberately left unpatched
        here (falsy, matching the real default test/prod posture).
        """
        response = self.client.get("/discord/link/start/?state=123456789:somenonce")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"invalid state", response.content.lower())
        self.assertIsNone(self.client.session.get("discord_oauth_state"))

    def test_oauth_callback_rejects_unsigned_state_when_secret_not_configured(self):
        """Test that discord_oauth_callback also fails closed on an unsigned state with no secret configured."""
        member = self.create_member()
        self.client.force_login(member.user)

        response = self.client.get("/discord/callback/?code=somecode&state=123456789:somenonce")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"invalid state", response.content.lower())
        self.assertIsNone(get_member_by_discord_id(123456789))

    # -- MINE-2: a discord_id must map to at most one member --

    def test_get_member_by_discord_id_multiple_does_not_500(self):
        """Test that two members sharing a linked discord_id does not raise MultipleObjectsReturned."""
        member1 = self.create_member(user=self.create_user(username="dup1", email="dup1@example.com"))
        member2 = self.create_member(user=self.create_user(username="dup2", email="dup2@example.com"))
        MemberConfig.objects.create(member=member1, name="discord_id", value="555555555")
        MemberConfig.objects.create(member=member2, name="discord_id", value="555555555")

        result = get_member_by_discord_id(555555555)

        self.assertIsNotNone(result)
        self.assertIn(result.uuid, {member1.uuid, member2.uuid})

    def test_link_discord_to_member_clears_other_members_link(self):
        """Test that linking a discord_id to a member clears its config row on any other member."""
        member1 = self.create_member(user=self.create_user(username="owner1", email="owner1@example.com"))
        member2 = self.create_member(user=self.create_user(username="owner2", email="owner2@example.com"))

        link_discord_to_member(member1, 777777777)
        link_discord_to_member(member2, 777777777)

        self.assertFalse(MemberConfig.objects.filter(member=member1, name="discord_id", deleted__isnull=True).exists())
        result = get_member_by_discord_id(777777777)
        self.assertEqual(result.uuid, member2.uuid)

    def test_link_discord_to_member_uses_advisory_lock(self):
        """Test that link_discord_to_member takes a Postgres advisory lock keyed on discord_id.

        This is what serializes two concurrent links of the same discord_id so
        they cannot both pass the dedupe-delete step and insert duplicates.
        """
        member = self.create_member()

        with CaptureQueriesContext(connection) as ctx:
            link_discord_to_member(member, 123123123)

        self.assertTrue(any("pg_advisory_xact_lock" in query["sql"] for query in ctx.captured_queries))

    def test_link_discord_to_member_rolls_back_atomically_on_failure(self):
        """Test that a failure partway through link_discord_to_member rolls back the whole operation.

        Proves the dedupe-delete and the write happen in one transaction: if
        the write half fails, the other member's row (that would have been
        deleted) must still be there, and no half-applied state is left.
        """
        member1 = self.create_member(user=self.create_user(username="atomic1", email="atomic1@example.com"))
        member2 = self.create_member(user=self.create_user(username="atomic2", email="atomic2@example.com"))
        link_discord_to_member(member1, 909090909)

        with (
            patch(
                "larpmanager.models.member.MemberConfig.objects.update_or_create",
                side_effect=RuntimeError("boom"),
            ),
            self.assertRaises(RuntimeError),
        ):
            link_discord_to_member(member2, 909090909)

        result = get_member_by_discord_id(909090909)
        self.assertIsNotNone(result)
        self.assertEqual(result.uuid, member1.uuid)

    def test_unlink_multiple_configs_bot_key_deletes_all(self):
        """Test that a bot key unlinking a discord_id with duplicate rows deletes all of them."""
        member1 = self.create_member(user=self.create_user(username="dupu1", email="dupu1@example.com"))
        member2 = self.create_member(user=self.create_user(username="dupu2", email="dupu2@example.com"))
        MemberConfig.objects.create(member=member1, name="discord_id", value="888777666")
        MemberConfig.objects.create(member=member2, name="discord_id", value="888777666")

        with override_settings(DISCORD_BOT_API_KEYS=["bot-unlink-key"]):
            response = self.client.get(
                "/api/v1/discord/unlink/?discord_id=888777666",
                HTTP_X_API_KEY="bot-unlink-key",
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertFalse(
            MemberConfig.objects.filter(name="discord_id", value="888777666", deleted__isnull=True).exists(),
        )

    def test_unlink_multiple_configs_scoped_key_deletes_only_in_scope(self):
        """Test that a scoped key with duplicate discord_id rows only unlinks the in-association row.

        Round 1 fixed the MultipleObjectsReturned 500 only in get_member_by_discord_id;
        the identical .get() in discord_unlink still raised. This also checks the
        association-scoping keeps working once the fix stops using .get().
        """
        member1 = self.create_member(user=self.create_user(username="scop1", email="scop1@example.com"))
        other_association = self.create_association(slug="other-assoc-scope", name="Other Assoc Scope")
        member2 = self.create_member(user=self.create_user(username="scop2", email="scop2@example.com"))
        Membership.objects.filter(member=member2, association=self.get_association()).delete()
        Membership.objects.create(member=member2, association=other_association)
        MemberConfig.objects.create(member=member1, name="discord_id", value="333222111")
        MemberConfig.objects.create(member=member2, name="discord_id", value="333222111")

        response = self.client.get(
            "/api/v1/discord/unlink/?discord_id=333222111",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertFalse(
            MemberConfig.objects.filter(member=member1, name="discord_id", deleted__isnull=True).exists(),
        )
        self.assertTrue(
            MemberConfig.objects.filter(member=member2, name="discord_id", deleted__isnull=True).exists(),
        )

    # -- MINE-3: discord_unlink must be association-scoped for non-bot keys --

    def test_unlink_rejects_cross_association_key(self):
        """Test that a tickets:write key scoped to one association cannot unlink another's member."""
        self.create_discord_linked_member(discord_id=222333444)
        other_association = self.create_association(slug="other-assoc", name="Other Assoc")
        PublisherApiKey.objects.create(
            name="Other Assoc Key",
            key="other-assoc-key",
            active=True,
            scopes=["tickets:write"],
            association=other_association,
        )

        response = self.client.get(
            "/api/v1/discord/unlink/?discord_id=222333444",
            HTTP_X_API_KEY="other-assoc-key",
        )

        self.assertEqual(response.status_code, 404)
        self.assertIsNotNone(get_member_by_discord_id(222333444))

    # -- FIX-OAUTH-3: request.limited must be checked before API-key auth --

    def test_unlink_rate_limit_checked_before_invalid_key(self):
        """Test that discord_unlink returns 429, not 401, once an invalid key is over the limit.

        Round 1 checked request.limited AFTER validate_ticket_api_key, so an
        invalid/missing key always short-circuited to 401 before the limiter
        was ever consulted - meaning API-key brute-forcing was never throttled.
        """
        cache.clear()
        self.addCleanup(cache.clear)
        invalid_headers = {"HTTP_X_API_KEY": "brute-force-unlink-key"}

        last_status = None
        for _attempt in range(31):
            response = self.client.get(
                "/api/v1/discord/unlink/?discord_id=1",
                **invalid_headers,
            )
            last_status = response.status_code

        self.assertEqual(last_status, 429)

    def test_oauth_url_rate_limit_checked_before_invalid_key(self):
        """Test that discord_oauth_url returns 429, not 401, once an invalid key is over the limit."""
        cache.clear()
        self.addCleanup(cache.clear)
        invalid_headers = {"HTTP_X_API_KEY": "brute-force-link-key"}

        last_status = None
        for _attempt in range(31):
            response = self.client.get(
                "/api/v1/discord/link/123456789/",
                **invalid_headers,
            )
            last_status = response.status_code

        self.assertEqual(last_status, 429)

    def test_member_check_rate_limit_checked_before_invalid_key(self):
        """Test that discord_member_check returns 429, not 401, once an invalid key is over the limit."""
        cache.clear()
        self.addCleanup(cache.clear)
        invalid_headers = {"HTTP_X_API_KEY": "brute-force-member-check-key"}

        last_status = None
        for _attempt in range(121):
            response = self.client.get(
                "/api/v1/discord/member/123456789/",
                **invalid_headers,
            )
            last_status = response.status_code

        self.assertEqual(last_status, 429)
