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
from typing import Any
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.http import HttpRequest, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from larpmanager.models.base import PublisherApiKey
from larpmanager.models.member import Member, MemberConfig

logger = logging.getLogger(__name__)

# Discord OAuth configuration - these should be set in Django settings
DISCORD_CLIENT_ID = getattr(settings, 'DISCORD_CLIENT_ID', '')
DISCORD_CLIENT_SECRET = getattr(settings, 'DISCORD_CLIENT_SECRET', '')
DISCORD_REDIRECT_URI = getattr(settings, 'DISCORD_REDIRECT_URI', '')
DISCORD_BOT_API_KEY = getattr(settings, 'DISCORD_BOT_API_KEY', '')

# Discord API endpoints
DISCORD_API_BASE = 'https://discord.com/api/v10'
DISCORD_OAUTH_AUTHORIZE = 'https://discord.com/oauth2/authorize'
DISCORD_OAUTH_TOKEN = f'{DISCORD_API_BASE}/oauth2/token'
DISCORD_USER_ME = f'{DISCORD_API_BASE}/users/@me'

# Config key for storing Discord ID in MemberConfig
DISCORD_ID_CONFIG_KEY = 'discord_id'


def validate_bot_api_key(request: HttpRequest) -> tuple[bool, JsonResponse | None]:
    """Validate the bot API key from request headers.

    Args:
        request: HTTP request object

    Returns:
        Tuple of (is_valid, error_response). If valid, error_response is None.

    """
    api_key = request.headers.get('X-API-Key') or request.GET.get('api_key')

    if not api_key:
        return False, JsonResponse({'error': 'API key required'}, status=401)

    # Check against configured bot API key first
    if DISCORD_BOT_API_KEY and api_key == DISCORD_BOT_API_KEY:
        return True, None

    # Fallback to PublisherApiKey validation
    try:
        PublisherApiKey.objects.get(key=api_key, active=True)
        return True, None
    except PublisherApiKey.DoesNotExist:
        return False, JsonResponse({'error': 'Invalid API key'}, status=401)


def get_member_by_discord_id(discord_id: int) -> Member | None:
    """Find a member by their linked Discord ID.

    Args:
        discord_id: The Discord user ID to search for

    Returns:
        Member object if found, None otherwise

    """
    try:
        config = MemberConfig.objects.select_related('member').get(
            name=DISCORD_ID_CONFIG_KEY,
            value=str(discord_id),
            deleted__isnull=True,
        )
        return config.member
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
    config, created = MemberConfig.objects.update_or_create(
        member=member,
        name=DISCORD_ID_CONFIG_KEY,
        defaults={'value': str(discord_id)},
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
    is_valid, error_response = validate_bot_api_key(request)
    if not is_valid:
        return error_response

    member = get_member_by_discord_id(discord_id)

    if member:
        return JsonResponse({
            'linked': True,
            'member': {
                'uuid': str(member.uuid),
                'name': member.display_member(),
                'email': member.email,
            }
        })

    return JsonResponse({'linked': False})


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
    is_valid, error_response = validate_bot_api_key(request)
    if not is_valid:
        return error_response

    if not DISCORD_CLIENT_ID:
        return JsonResponse(
            {'error': 'Discord OAuth not configured'},
            status=500
        )

    # Generate a state token that encodes the discord_id securely
    # Format: discord_id:random_nonce:signature
    nonce = secrets.token_urlsafe(16)
    message = f'{discord_id}:{nonce}'

    if DISCORD_CLIENT_SECRET:
        signature = hmac.new(
            DISCORD_CLIENT_SECRET.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()[:16]
        state = f'{message}:{signature}'
    else:
        state = message

    # Build the OAuth URL
    params = {
        'client_id': DISCORD_CLIENT_ID,
        'redirect_uri': DISCORD_REDIRECT_URI,
        'response_type': 'code',
        'scope': 'identify email',
        'state': state,
    }

    oauth_url = f'{DISCORD_OAUTH_AUTHORIZE}?{urlencode(params)}'

    return JsonResponse({'oauth_url': oauth_url})


@require_GET
def discord_oauth_callback(request: HttpRequest) -> HttpResponseRedirect:
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
    code = request.GET.get('code')
    state = request.GET.get('state')
    error = request.GET.get('error')

    if error:
        logger.warning(f'Discord OAuth error: {error}')
        return render(request, 'discord_link_error.html', {
            'error': 'Discord authorization was denied or failed.',
        })

    if not code or not state:
        return render(request, 'discord_link_error.html', {
            'error': 'Missing authorization code or state.',
        })

    # Parse and validate the state parameter
    try:
        parts = state.split(':')
        if len(parts) >= 2:
            discord_id_from_state = int(parts[0])

            # Verify signature if secret is configured
            if DISCORD_CLIENT_SECRET and len(parts) >= 3:
                nonce = parts[1]
                provided_signature = parts[2]
                message = f'{discord_id_from_state}:{nonce}'
                expected_signature = hmac.new(
                    DISCORD_CLIENT_SECRET.encode(),
                    message.encode(),
                    hashlib.sha256
                ).hexdigest()[:16]

                if not hmac.compare_digest(provided_signature, expected_signature):
                    raise ValueError('Invalid state signature')
        else:
            raise ValueError('Invalid state format')
    except (ValueError, IndexError) as e:
        logger.warning(f'Discord OAuth state validation failed: {e}')
        return render(request, 'discord_link_error.html', {
            'error': 'Invalid state parameter.',
        })

    # Exchange the code for an access token
    try:
        token_response = requests.post(
            DISCORD_OAUTH_TOKEN,
            data={
                'client_id': DISCORD_CLIENT_ID,
                'client_secret': DISCORD_CLIENT_SECRET,
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': DISCORD_REDIRECT_URI,
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=10,
        )
        token_response.raise_for_status()
        token_data = token_response.json()
    except requests.RequestException as e:
        logger.error(f'Discord token exchange failed: {e}')
        return render(request, 'discord_link_error.html', {
            'error': 'Failed to exchange authorization code.',
        })

    access_token = token_data.get('access_token')
    if not access_token:
        return render(request, 'discord_link_error.html', {
            'error': 'No access token received from Discord.',
        })

    # Fetch the Discord user's information
    try:
        user_response = requests.get(
            DISCORD_USER_ME,
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=10,
        )
        user_response.raise_for_status()
        discord_user = user_response.json()
    except requests.RequestException as e:
        logger.error(f'Discord user fetch failed: {e}')
        return render(request, 'discord_link_error.html', {
            'error': 'Failed to fetch Discord user information.',
        })

    discord_id = int(discord_user.get('id', 0))
    discord_username = discord_user.get('username', 'Unknown')

    # Verify the Discord ID matches what was in the state
    if discord_id != discord_id_from_state:
        logger.warning(
            f'Discord ID mismatch: state={discord_id_from_state}, actual={discord_id}'
        )
        return render(request, 'discord_link_error.html', {
            'error': 'Discord account mismatch. Please try again.',
        })

    # Check if the user is logged in to larpmanager
    if not request.user.is_authenticated:
        # Store Discord info in session and redirect to login
        request.session['pending_discord_link'] = {
            'discord_id': discord_id,
            'discord_username': discord_username,
        }
        return HttpResponseRedirect('/accounts/login/?next=/discord/link/complete/')

    # Link the Discord account to the logged-in member
    try:
        member = request.user.member
        link_discord_to_member(member, discord_id)
        logger.info(f'Linked Discord {discord_id} ({discord_username}) to member {member.uuid}')
    except Exception as e:
        logger.error(f'Failed to link Discord account: {e}')
        return render(request, 'discord_link_error.html', {
            'error': 'Failed to link Discord account to your profile.',
        })

    return render(request, 'discord_link_success.html', {
        'discord_username': discord_username,
        'member_name': member.display_member(),
    })


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
        return HttpResponseRedirect('/accounts/login/')

    pending_link = request.session.pop('pending_discord_link', None)
    if not pending_link:
        return render(request, 'discord_link_error.html', {
            'error': 'No pending Discord link found. Please try again.',
        })

    discord_id = pending_link['discord_id']
    discord_username = pending_link['discord_username']

    try:
        member = request.user.member
        link_discord_to_member(member, discord_id)
        logger.info(f'Linked Discord {discord_id} ({discord_username}) to member {member.uuid}')
    except Exception as e:
        logger.error(f'Failed to link Discord account: {e}')
        return render(request, 'discord_link_error.html', {
            'error': 'Failed to link Discord account to your profile.',
        })

    return render(request, 'discord_link_success.html', {
        'discord_username': discord_username,
        'member_name': member.display_member(),
    })


@require_GET
def discord_unlink(request: HttpRequest) -> JsonResponse:
    """Unlink a Discord account from a member (API endpoint).

    Args:
        request: HTTP request object with discord_id parameter

    Returns:
        JSON response indicating success or failure

    """
    is_valid, error_response = validate_bot_api_key(request)
    if not is_valid:
        return error_response

    discord_id = request.GET.get('discord_id')
    if not discord_id:
        return JsonResponse({'error': 'discord_id required'}, status=400)

    try:
        config = MemberConfig.objects.get(
            name=DISCORD_ID_CONFIG_KEY,
            value=str(discord_id),
            deleted__isnull=True,
        )
        config.delete()
        return JsonResponse({'success': True})
    except MemberConfig.DoesNotExist:
        return JsonResponse({'error': 'Discord account not linked'}, status=404)
