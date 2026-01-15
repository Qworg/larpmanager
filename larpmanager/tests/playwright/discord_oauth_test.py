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
"""Playwright E2E tests for Discord OAuth flow."""

import pytest

from larpmanager.tests.utils import expect_normalized, go_to

pytestmark = pytest.mark.e2e


def test_oauth_success_page_renders(pw_page):
    """Test that OAuth callback route exists and renders error template for invalid code."""
    page, live_server, context = pw_page
    # Visit callback with test params (will fail because we can't mock Discord)
    go_to(page, live_server, "/discord/callback/?code=test&state=123:nonce:sig")
    # Since we can't mock Discord OAuth, we expect an error page
    # This tests that the route exists and renders
    expect_normalized(page, page.locator("body"), "Discord")


def test_oauth_error_page_shows_message(pw_page):
    """Test that OAuth error page shows appropriate message when access denied."""
    page, live_server, context = pw_page
    go_to(page, live_server, "/discord/callback/?error=access_denied")
    # Should show error message about authorization being denied
    expect_normalized(page, page.locator("body"), "denied")


def test_oauth_callback_missing_code_shows_error(pw_page):
    """Test that callback shows error when authorization code is missing."""
    page, live_server, context = pw_page
    # Missing code parameter
    go_to(page, live_server, "/discord/callback/?state=123:nonce:sig")
    # Should show error about missing code
    expect_normalized(page, page.locator("body"), "Missing")


def test_oauth_callback_invalid_state_shows_error(pw_page):
    """Test that callback shows error when state is invalid."""
    page, live_server, context = pw_page
    # Invalid state format
    go_to(page, live_server, "/discord/callback/?code=test&state=invalid")
    # Should show error about invalid state
    expect_normalized(page, page.locator("body"), "Invalid")


def test_discord_link_complete_no_pending(pw_page):
    """Test that link complete shows error if no pending link in session."""
    page, live_server, context = pw_page
    # Try to complete linking without going through OAuth flow
    go_to(page, live_server, "/discord/link/complete/")
    # Should either redirect to login or show error about no pending link
    # (depends on auth state)
    body_text = page.locator("body").inner_text()
    assert "login" in body_text.lower() or "pending" in body_text.lower() or "error" in body_text.lower()
