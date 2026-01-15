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
"""Ticket API endpoints for Discord bot integration.

This module provides REST API endpoints for:
- Creating, reading, updating tickets
- Listing tickets with filters
- Closing and reopening tickets
- Listing associations for ticket creation dropdown
"""
from __future__ import annotations

import json
import logging
from typing import Any

from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from larpmanager.models.association import Association
from larpmanager.models.larpmanager import LarpManagerTicket, TicketPriority, TicketStatus
from larpmanager.views.api_discord import get_member_by_discord_id, validate_bot_api_key

logger = logging.getLogger(__name__)


def ticket_to_dict(ticket: LarpManagerTicket) -> dict[str, Any]:
    """Convert a ticket model to a dictionary for JSON response.

    Args:
        ticket: The ticket model instance

    Returns:
        Dictionary representation of the ticket

    """
    return {
        'uuid': str(ticket.uuid),
        'subject': ticket.subject,
        'reason': ticket.reason,
        'content': ticket.content,
        'status': ticket.status,
        'priority': ticket.priority,
        'discord_channel_id': ticket.discord_channel_id,
        'discord_creator_id': ticket.discord_creator_id,
        'assigned_staff_discord_id': ticket.assigned_staff_discord_id,
        'association': {
            'uuid': str(ticket.association.uuid),
            'name': ticket.association.name,
            'slug': ticket.association.slug,
        } if ticket.association else None,
        'member': {
            'uuid': str(ticket.member.uuid),
            'name': ticket.member.display_member(),
        } if ticket.member else None,
        'email': ticket.email,
        'transcript': ticket.transcript,
        'created_at': ticket.created.isoformat() if ticket.created else None,
        'updated_at': ticket.updated.isoformat() if ticket.updated else None,
        'closed_at': ticket.closed_at.isoformat() if ticket.closed_at else None,
    }


@require_GET
def list_associations(request: HttpRequest) -> JsonResponse:
    """List all active associations for ticket creation dropdown.

    Args:
        request: HTTP request object

    Returns:
        JSON response with list of associations

    """
    is_valid, error_response = validate_bot_api_key(request)
    if not is_valid:
        return error_response

    associations = Association.objects.filter(
        deleted__isnull=True
    ).order_by('name')

    return JsonResponse({
        'associations': [
            {
                'uuid': str(assoc.uuid),
                'name': assoc.name,
                'slug': assoc.slug,
            }
            for assoc in associations
        ]
    })


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def tickets_list_create(request: HttpRequest) -> JsonResponse:
    """List tickets or create a new ticket.

    GET: List tickets with optional filters
        - status: Filter by status (open, working, done)
        - association_uuid: Filter by association
        - discord_creator_id: Filter by creator's Discord ID
        - limit: Max results (default 50)
        - offset: Pagination offset (default 0)

    POST: Create a new ticket
        Required: association_uuid, discord_creator_id, discord_channel_id
        Optional: subject, content, email, priority

    Args:
        request: HTTP request object

    Returns:
        JSON response with ticket(s)

    """
    is_valid, error_response = validate_bot_api_key(request)
    if not is_valid:
        return error_response

    if request.method == 'GET':
        # List tickets with filters
        queryset = LarpManagerTicket.objects.filter(
            deleted__isnull=True
        ).select_related('association', 'member').order_by('-created')

        # Apply filters
        status = request.GET.get('status')
        if status:
            queryset = queryset.filter(status=status)

        association_uuid = request.GET.get('association_uuid')
        if association_uuid:
            queryset = queryset.filter(association__uuid=association_uuid)

        discord_creator_id = request.GET.get('discord_creator_id')
        if discord_creator_id:
            queryset = queryset.filter(discord_creator_id=int(discord_creator_id))

        discord_channel_id = request.GET.get('discord_channel_id')
        if discord_channel_id:
            queryset = queryset.filter(discord_channel_id=int(discord_channel_id))

        # Only Discord-created tickets (have channel ID)
        discord_only = request.GET.get('discord_only', 'false').lower() == 'true'
        if discord_only:
            queryset = queryset.filter(discord_channel_id__isnull=False)

        # Pagination
        limit = min(int(request.GET.get('limit', 50)), 100)
        offset = int(request.GET.get('offset', 0))

        total = queryset.count()
        tickets = queryset[offset:offset + limit]

        return JsonResponse({
            'tickets': [ticket_to_dict(t) for t in tickets],
            'total': total,
            'limit': limit,
            'offset': offset,
        })

    elif request.method == 'POST':
        # Create a new ticket
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON body'}, status=400)

        # Required fields
        association_uuid = data.get('association_uuid')
        discord_creator_id = data.get('discord_creator_id')
        discord_channel_id = data.get('discord_channel_id')

        if not all([association_uuid, discord_creator_id, discord_channel_id]):
            return JsonResponse({
                'error': 'Missing required fields: association_uuid, discord_creator_id, discord_channel_id'
            }, status=400)

        # Get association
        try:
            association = Association.objects.get(uuid=association_uuid, deleted__isnull=True)
        except Association.DoesNotExist:
            return JsonResponse({'error': 'Association not found'}, status=404)

        # Get member if linked
        member = get_member_by_discord_id(int(discord_creator_id))

        # Create ticket
        ticket = LarpManagerTicket.objects.create(
            association=association,
            member=member,
            discord_channel_id=int(discord_channel_id),
            discord_creator_id=int(discord_creator_id),
            subject=data.get('subject', ''),
            reason=data.get('reason', 'Discord Support'),
            content=data.get('content', ''),
            email=data.get('email', member.email if member else None),
            priority=data.get('priority', TicketPriority.LOW),
            status=TicketStatus.OPEN,
        )

        logger.info(f'Created ticket {ticket.uuid} via Discord API')

        return JsonResponse({
            'ticket': ticket_to_dict(ticket)
        }, status=201)


@csrf_exempt
@require_http_methods(['GET', 'PATCH', 'DELETE'])
def ticket_detail(request: HttpRequest, ticket_uuid: str) -> JsonResponse:
    """Get, update, or delete a specific ticket.

    GET: Get ticket details
    PATCH: Update ticket fields (status, priority, assignment, etc.)
    DELETE: Soft delete the ticket

    Args:
        request: HTTP request object
        ticket_uuid: UUID of the ticket

    Returns:
        JSON response with ticket details or success message

    """
    is_valid, error_response = validate_bot_api_key(request)
    if not is_valid:
        return error_response

    try:
        ticket = LarpManagerTicket.objects.select_related(
            'association', 'member'
        ).get(uuid=ticket_uuid, deleted__isnull=True)
    except LarpManagerTicket.DoesNotExist:
        return JsonResponse({'error': 'Ticket not found'}, status=404)

    if request.method == 'GET':
        return JsonResponse({'ticket': ticket_to_dict(ticket)})

    elif request.method == 'PATCH':
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON body'}, status=400)

        # Update allowed fields
        if 'status' in data:
            new_status = data['status']
            if new_status in [s.value for s in TicketStatus]:
                ticket.status = new_status
            else:
                return JsonResponse({'error': f'Invalid status: {new_status}'}, status=400)

        if 'priority' in data:
            new_priority = data['priority']
            if new_priority in [p.value for p in TicketPriority]:
                ticket.priority = new_priority
            else:
                return JsonResponse({'error': f'Invalid priority: {new_priority}'}, status=400)

        if 'assigned_staff_discord_id' in data:
            ticket.assigned_staff_discord_id = data['assigned_staff_discord_id']

        if 'subject' in data:
            ticket.subject = data['subject']

        if 'content' in data:
            ticket.content = data['content']

        if 'analysis' in data:
            ticket.analysis = data['analysis']

        if 'transcript' in data:
            ticket.transcript = data['transcript']

        ticket.save()
        logger.info(f'Updated ticket {ticket.uuid} via Discord API')

        return JsonResponse({'ticket': ticket_to_dict(ticket)})

    elif request.method == 'DELETE':
        ticket.deleted = timezone.now()
        ticket.save()
        logger.info(f'Deleted ticket {ticket.uuid} via Discord API')
        return JsonResponse({'success': True})


@csrf_exempt
@require_http_methods(['POST'])
def ticket_close(request: HttpRequest, ticket_uuid: str) -> JsonResponse:
    """Close a ticket with optional transcript.

    POST body:
        - transcript: Optional full transcript text

    Args:
        request: HTTP request object
        ticket_uuid: UUID of the ticket

    Returns:
        JSON response with updated ticket

    """
    is_valid, error_response = validate_bot_api_key(request)
    if not is_valid:
        return error_response

    try:
        ticket = LarpManagerTicket.objects.select_related(
            'association', 'member'
        ).get(uuid=ticket_uuid, deleted__isnull=True)
    except LarpManagerTicket.DoesNotExist:
        return JsonResponse({'error': 'Ticket not found'}, status=404)

    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        data = {}

    # Update transcript if provided
    if 'transcript' in data:
        ticket.transcript = data['transcript']

    # Close the ticket
    ticket.status = TicketStatus.DONE
    ticket.closed_at = timezone.now()
    ticket.save()

    logger.info(f'Closed ticket {ticket.uuid} via Discord API')

    return JsonResponse({'ticket': ticket_to_dict(ticket)})


@csrf_exempt
@require_http_methods(['POST'])
def ticket_reopen(request: HttpRequest, ticket_uuid: str) -> JsonResponse:
    """Reopen a closed ticket.

    Args:
        request: HTTP request object
        ticket_uuid: UUID of the ticket

    Returns:
        JSON response with updated ticket

    """
    is_valid, error_response = validate_bot_api_key(request)
    if not is_valid:
        return error_response

    try:
        ticket = LarpManagerTicket.objects.select_related(
            'association', 'member'
        ).get(uuid=ticket_uuid, deleted__isnull=True)
    except LarpManagerTicket.DoesNotExist:
        return JsonResponse({'error': 'Ticket not found'}, status=404)

    if ticket.status != TicketStatus.DONE:
        return JsonResponse({
            'error': 'Ticket is not closed, cannot reopen'
        }, status=400)

    # Reopen the ticket
    ticket.status = TicketStatus.OPEN
    ticket.closed_at = None
    ticket.save()

    logger.info(f'Reopened ticket {ticket.uuid} via Discord API')

    return JsonResponse({'ticket': ticket_to_dict(ticket)})


@require_GET
def ticket_by_channel(request: HttpRequest, channel_id: int) -> JsonResponse:
    """Get a ticket by its Discord channel ID.

    Args:
        request: HTTP request object
        channel_id: Discord channel ID

    Returns:
        JSON response with ticket details

    """
    is_valid, error_response = validate_bot_api_key(request)
    if not is_valid:
        return error_response

    try:
        ticket = LarpManagerTicket.objects.select_related(
            'association', 'member'
        ).get(discord_channel_id=channel_id, deleted__isnull=True)
    except LarpManagerTicket.DoesNotExist:
        return JsonResponse({'error': 'Ticket not found'}, status=404)

    return JsonResponse({'ticket': ticket_to_dict(ticket)})
