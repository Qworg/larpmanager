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
"""Discord OAuth and integration API endpoints.

This module provides API endpoints for:
- Checking if a Discord user is linked to a larpmanager member
- Generating OAuth URLs for linking Discord accounts
- Handling OAuth callbacks from Discord
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.http import HttpRequest, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from larpmanager.models.base import PublisherApiKey
from larpmanager.models.member import Member, MemberConfig
from larpmanager.utils.publication.api import log_api_access

logger = logging.getLogger(__name__)

# Discord OAuth configuration - these should be set in Django settings
DISCORD_CLIENT_ID = getattr(settings, "DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = getattr(settings, "DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI = getattr(settings, "DISCORD_REDIRECT_URI", "")

# Discord API endpoints
DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_OAUTH_AUTHORIZE = "https://discord.com/oauth2/authorize"
DISCORD_OAUTH_TOKEN = f"{DISCORD_API_BASE}/oauth2/token"
DISCORD_USER_ME = f"{DISCORD_API_BASE}/users/@me"

# Config key for storing Discord ID in MemberConfig
DISCORD_ID_CONFIG_KEY = "discord_id"


@dataclass
class TicketAuth:
    """Authentication result for a ticket/discord API request."""

    is_bot_key: bool
    api_key: PublisherApiKey | None
    scopes: list[str]
    association: Any
    used_fallback: bool
    key_id: str

    def has_scope(self, scope: str) -> bool:
        """Return whether this auth grants the given scope (wildcards respected)."""
        if "*" in self.scopes:
            return True
        if scope.startswith("tickets:") and "tickets:*" in self.scopes:
            return True
        return scope in self.scopes


def _scope_allows(scopes: list[str], required_scope: str | None) -> bool:
    """Return whether the given scopes grant the required scope.

    An empty scope list grants nothing. Only an explicit wildcard (``*`` for
    all scopes, or ``tickets:*`` for ticket scopes) grants the requirement.
    """
    if required_scope is None:
        return True
    if "*" in scopes:
        return True
    if required_scope.startswith("tickets:") and "tickets:*" in scopes:
        return True
    return required_scope in scopes


def validate_ticket_api_key(  # noqa: PLR0911
    request: HttpRequest, required_scope: str | None = None
) -> tuple[TicketAuth | None, JsonResponse | None]:
    """Validate a ticket/discord API key and record an audit log entry.

    Only the ``X-API-Key`` header is accepted. Bot keys (``DISCORD_BOT_API_KEYS``
    plus the legacy ``DISCORD_BOT_API_KEY``) are compared with ``hmac.compare_digest``
    and always pass. PublisherApiKey credentials are scope-checked (empty scopes
    grant nothing); during the dual-accept window
    (``DISCORD_TICKET_API_ALLOW_PUBLISHER_FALLBACK``) a PublisherApiKey that fails
    the scope check is still accepted but audit-flagged as a fallback.

    Args:
        request: HTTP request object
        required_scope: Scope required for this endpoint (None means any valid key)

    Returns:
        Tuple of (auth, error_response). On failure ``auth`` is None.

    """
    api_key = request.headers.get("X-API-Key")
    action = f"{request.method} {request.path}"

    if not api_key:
        log_api_access(None, request, 401, action=action)
        return None, JsonResponse({"error": "API key required"}, status=401)

    bot_keys = list(getattr(settings, "DISCORD_BOT_API_KEYS", []) or [])
    legacy_bot_key = getattr(settings, "DISCORD_BOT_API_KEY", "")
    if legacy_bot_key:
        bot_keys.append(legacy_bot_key)

    for configured_key in bot_keys:
        if configured_key and hmac.compare_digest(api_key, configured_key):
            auth = TicketAuth(
                is_bot_key=True,
                api_key=None,
                scopes=[],
                association=None,
                used_fallback=False,
                key_id="bot",
            )
            request._ticket_auth = auth  # noqa: SLF001
            log_api_access(None, request, 200, action=action)
            return auth, None

    try:
        publisher_key = PublisherApiKey.objects.get(key=api_key, active=True)
    except PublisherApiKey.DoesNotExist:
        log_api_access(None, request, 401, action=action)
        return None, JsonResponse({"error": "Invalid API key"}, status=401)

    scopes = list(publisher_key.scopes or [])
    if _scope_allows(scopes, required_scope):
        auth = TicketAuth(
            is_bot_key=False,
            api_key=publisher_key,
            scopes=scopes,
            association=publisher_key.association,
            used_fallback=False,
            key_id=str(publisher_key.id),
        )
        request._ticket_auth = auth  # noqa: SLF001
        log_api_access(publisher_key, request, 200, action=action)
        return auth, None

    # Scope check failed. During the dual-accept window, accept a PublisherApiKey
    # even without the required scope as an explicit, audit-flagged escape hatch.
    if getattr(settings, "DISCORD_TICKET_API_ALLOW_PUBLISHER_FALLBACK", False):
        auth = TicketAuth(
            is_bot_key=False,
            api_key=publisher_key,
            scopes=scopes,
            association=publisher_key.association,
            used_fallback=True,
            key_id=str(publisher_key.id),
        )
        request._ticket_auth = auth  # noqa: SLF001
        logger.warning("Deprecated PublisherApiKey fallback used for ticket API (key id=%s)", publisher_key.id)
        log_api_access(publisher_key, request, 200, action=action)
        return auth, None

    # Fail closed: a legacy key without any scopes is no longer a valid ticket key
    # (401); a scoped key that lacks the required scope is rejected (403).
    if not scopes:
        log_api_access(publisher_key, request, 401, action=action)
        return None, JsonResponse({"error": "Invalid API key"}, status=401)
    log_api_access(publisher_key, request, 403, action=action)
    return None, JsonResponse({"error": "Insufficient scope"}, status=403)


def validate_bot_api_key(request: HttpRequest) -> tuple[bool, JsonResponse | None]:
    """Validate the bot API key (backward-compatible wrapper).

    Args:
        request: HTTP request object

    Returns:
        Tuple of (is_valid, error_response). If valid, error_response is None.

    """
    _auth, error_response = validate_ticket_api_key(request)
    if error_response is not None:
        return False, error_response
    return True, None


def get_member_by_discord_id(discord_id: int) -> Member | None:
    """Find a member by their linked Discord ID.

    Args:
        discord_id: The Discord user ID to search for

    Returns:
        Member object if found, None otherwise

    """
    try:
        config = MemberConfig.objects.select_related("member").get(
            name=DISCORD_ID_CONFIG_KEY,
            value=str(discord_id),
            deleted__isnull=True,
        )
        return config.member  # noqa: TRY300
    except MemberConfig.DoesNotExist:
        return None


def link_discord_to_member(member: Member, discord_id: int) -> MemberConfig:
    """Link a Discord ID to a member account.

    Args:
        member: The member to link
        discord_id: The Discord user ID to link

    Returns:
        The created or updated MemberConfig object

    """
    config, _created = MemberConfig.objects.update_or_create(
        member=member,
        name=DISCORD_ID_CONFIG_KEY,
        defaults={"value": str(discord_id)},
    )
    return config


@require_GET
def discord_member_check(request: HttpRequest, discord_id: int) -> JsonResponse:
    """Check if a Discord user is linked to a larpmanager member.

    Args:
        request: HTTP request object
        discord_id: The Discord user ID to check

    Returns:
        JSON response with linked status and member info if linked

    """
    auth, error_response = validate_ticket_api_key(request)
    if error_response is not None:
        return error_response

    member = get_member_by_discord_id(discord_id)

    if member:
        member_data = {
            "uuid": str(member.uuid),
            "name": member.display_member(),
        }
        if auth.is_bot_key or auth.has_scope("members:read"):
            member_data["email"] = member.email
        return JsonResponse({"linked": True, "member": member_data})

    return JsonResponse({"linked": False})


@require_GET
def discord_oauth_url(request: HttpRequest, discord_id: int) -> JsonResponse:
    """Generate a Discord OAuth URL for linking an account.

    The state parameter contains the Discord ID so we can link it after OAuth.

    Args:
        request: HTTP request object
        discord_id: The Discord user ID requesting to link

    Returns:
        JSON response with the OAuth URL

    """
    _auth, error_response = validate_ticket_api_key(request)
    if error_response is not None:
        return error_response

    if not DISCORD_CLIENT_ID:
        return JsonResponse({"error": "Discord OAuth not configured"}, status=500)

    # Generate a state token that encodes the discord_id securely
    # Format: discord_id:random_nonce:signature
    nonce = secrets.token_urlsafe(16)
    message = f"{discord_id}:{nonce}"

    if DISCORD_CLIENT_SECRET:
        signature = hmac.new(DISCORD_CLIENT_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()[:16]
        state = f"{message}:{signature}"
    else:
        state = message

    # Build the OAuth URL
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify email",
        "state": state,
    }

    oauth_url = f"{DISCORD_OAUTH_AUTHORIZE}?{urlencode(params)}"

    return JsonResponse({"oauth_url": oauth_url})


@require_GET
def discord_oauth_callback(request: HttpRequest) -> HttpResponseRedirect:  # noqa: C901, PLR0911, PLR0912
    """Handle the OAuth callback from Discord.

    This endpoint:
    1. Exchanges the authorization code for an access token
    2. Fetches the Discord user's information
    3. Links the Discord account to the logged-in larpmanager user
    4. Redirects to a success page

    Args:
        request: HTTP request object with code and state parameters

    Returns:
        Redirect to success or error page

    """
    code = request.GET.get("code")
    state = request.GET.get("state")
    error = request.GET.get("error")

    if error:
        logger.warning("Discord OAuth error: %s", error)
        return render(
            request,
            "discord_link_error.html",
            {
                "error": "Discord authorization was denied or failed.",
            },
        )

    if not code or not state:
        return render(
            request,
            "discord_link_error.html",
            {
                "error": "Missing authorization code or state.",
            },
        )

    # Parse and validate the state parameter
    try:
        parts = state.split(":")
        if len(parts) >= 2:  # noqa: PLR2004
            discord_id_from_state = int(parts[0])

            # Verify signature if secret is configured
            if DISCORD_CLIENT_SECRET and len(parts) >= 3:  # noqa: PLR2004
                nonce = parts[1]
                provided_signature = parts[2]
                message = f"{discord_id_from_state}:{nonce}"
                expected_signature = hmac.new(
                    DISCORD_CLIENT_SECRET.encode(), message.encode(), hashlib.sha256
                ).hexdigest()[:16]

                if not hmac.compare_digest(provided_signature, expected_signature):
                    raise ValueError("Invalid state signature")  # noqa: EM101, TRY003, TRY301
        else:
            raise ValueError("Invalid state format")  # noqa: EM101, TRY003, TRY301
    except (ValueError, IndexError) as e:
        logger.warning("Discord OAuth state validation failed: %s", e)
        return render(
            request,
            "discord_link_error.html",
            {
                "error": "Invalid state parameter.",
            },
        )

    # Exchange the code for an access token
    try:
        token_response = requests.post(
            DISCORD_OAUTH_TOKEN,
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": DISCORD_REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        token_response.raise_for_status()
        token_data = token_response.json()
    except requests.RequestException:
        logger.exception("Discord token exchange failed")
        return render(
            request,
            "discord_link_error.html",
            {
                "error": "Failed to exchange authorization code.",
            },
        )

    access_token = token_data.get("access_token")
    if not access_token:
        return render(
            request,
            "discord_link_error.html",
            {
                "error": "No access token received from Discord.",
            },
        )

    # Fetch the Discord user's information
    try:
        user_response = requests.get(
            DISCORD_USER_ME,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        user_response.raise_for_status()
        discord_user = user_response.json()
    except requests.RequestException:
        logger.exception("Discord user fetch failed")
        return render(
            request,
            "discord_link_error.html",
            {
                "error": "Failed to fetch Discord user information.",
            },
        )

    discord_id = int(discord_user.get("id", 0))
    discord_username = discord_user.get("username", "Unknown")

    # Verify the Discord ID matches what was in the state
    if discord_id != discord_id_from_state:
        logger.warning("Discord ID mismatch: state=%s, actual=%s", discord_id_from_state, discord_id)
        return render(
            request,
            "discord_link_error.html",
            {
                "error": "Discord account mismatch. Please try again.",
            },
        )

    # Check if the user is logged in to larpmanager
    if not request.user.is_authenticated:
        # Store Discord info in session and redirect to login
        request.session["pending_discord_link"] = {
            "discord_id": discord_id,
            "discord_username": discord_username,
        }
        return HttpResponseRedirect("/accounts/login/?next=/discord/link/complete/")

    # Link the Discord account to the logged-in member
    try:
        member = request.user.member
        link_discord_to_member(member, discord_id)
        logger.info("Linked Discord %s (%s) to member %s", discord_id, discord_username, member.uuid)
    except Exception:
        logger.exception("Failed to link Discord account")
        return render(
            request,
            "discord_link_error.html",
            {
                "error": "Failed to link Discord account to your profile.",
            },
        )

    return render(
        request,
        "discord_link_success.html",
        {
            "discord_username": discord_username,
            "member_name": member.display_member(),
        },
    )


@require_GET
def discord_link_complete(request: HttpRequest) -> HttpResponseRedirect:
    """Complete Discord linking after login.

    Called when user was redirected to login during OAuth flow.

    Args:
        request: HTTP request object

    Returns:
        Redirect to success or error page

    """
    if not request.user.is_authenticated:
        return HttpResponseRedirect("/accounts/login/")

    pending_link = request.session.pop("pending_discord_link", None)
    if not pending_link:
        return render(
            request,
            "discord_link_error.html",
            {
                "error": "No pending Discord link found. Please try again.",
            },
        )

    discord_id = pending_link["discord_id"]
    discord_username = pending_link["discord_username"]

    try:
        member = request.user.member
        link_discord_to_member(member, discord_id)
        logger.info("Linked Discord %s (%s) to member %s", discord_id, discord_username, member.uuid)
    except Exception:
        logger.exception("Failed to link Discord account")
        return render(
            request,
            "discord_link_error.html",
            {
                "error": "Failed to link Discord account to your profile.",
            },
        )

    return render(
        request,
        "discord_link_success.html",
        {
            "discord_username": discord_username,
            "member_name": member.display_member(),
        },
    )


@require_GET
def discord_unlink(request: HttpRequest) -> JsonResponse:
    """Unlink a Discord account from a member (API endpoint).

    Args:
        request: HTTP request object with discord_id parameter

    Returns:
        JSON response indicating success or failure

    """
    _auth, error_response = validate_ticket_api_key(request, required_scope="tickets:write")
    if error_response is not None:
        return error_response

    discord_id = request.GET.get("discord_id")
    if not discord_id:
        return JsonResponse({"error": "discord_id required"}, status=400)

    try:
        config = MemberConfig.objects.get(
            name=DISCORD_ID_CONFIG_KEY,
            value=str(discord_id),
            deleted__isnull=True,
        )
        config.delete()
        return JsonResponse({"success": True})
    except MemberConfig.DoesNotExist:
        return JsonResponse({"error": "Discord account not linked"}, status=404)
