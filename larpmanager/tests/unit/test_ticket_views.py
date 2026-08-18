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
"""Tests for the web ticket dashboard, mutation views, and live-refresh endpoints."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from django.http import Http404
from django.test import RequestFactory

from larpmanager.models.larpmanager import LarpManagerTicket, TicketPriority, TicketStatus
from larpmanager.models.ticket_event import TicketEvent
from larpmanager.models.ticket_message import TicketMessage
from larpmanager.tests.unit.base import BaseTestCase
from larpmanager.views.exe import ticket as exe_ticket_views
from larpmanager.views.user import ticket as ticket_views


class _CapturedRender(dict[str, Any]):
    """Dict-like render stand-in that also exposes the response ``status_code``."""

    status_code: int = 200


def _capture_render(_request: Any, template_name: str, context: dict | None = None, **kwargs: Any) -> _CapturedRender:
    """Stand in for ``django.shortcuts.render`` to capture the template context."""
    rendered = _CapturedRender({"template": template_name, "context": context or {}})
    rendered.status_code = kwargs.get("status", 200)
    return rendered


class TestTicketViews(BaseTestCase):
    """Test cases for the web ticket views."""

    def _make_request(self, method: str = "get", path: str = "/", data: dict | None = None) -> Any:
        """Build an authenticated RequestFactory request."""
        factory = RequestFactory()
        request = factory.post(path, data or {}) if method == "post" else factory.get(path, data or {})
        request.user = self.get_user()
        request.session = {}
        request.resolver_match = None
        return request

    def _staff_context(self, association: Any, member: Any = None) -> dict[str, Any]:
        """Return a minimal staff context dict for patching ``get_context``."""
        return {
            "association_id": association.id,
            "member": member or self.create_member(),
            "is_admin": True,
            "features": {},
        }

    def test_soft_deleted_hidden_by_manager(self):
        """Soft-deleted tickets are hidden by the default SafeDelete manager."""
        ticket = self.create_larpmanager_ticket()
        ticket.delete()

        self.assertFalse(LarpManagerTicket.objects.filter(pk=ticket.pk).exists())
        self.assertTrue(LarpManagerTicket.all_objects.filter(pk=ticket.pk).exists())

    @patch("larpmanager.views.exe.ticket.render", side_effect=_capture_render)
    @patch("larpmanager.views.exe.ticket.check_association_context")
    def test_staff_list_includes_member_none_ticket(self, mock_check, mock_render):  # noqa: ARG002
        """The staff dashboard lists API-created tickets with member=None."""
        association = self.get_association()
        mock_check.return_value = {"association_id": association.id}

        member_ticket = self.create_larpmanager_ticket(association=association, member=self.create_member())
        api_ticket = self.create_larpmanager_ticket(
            association=association, member=None, discord_channel_id=999, status=TicketStatus.OPEN
        )

        request = self._make_request("get", "/manage/tickets/")
        result = exe_ticket_views.exe_tickets(request)

        uuids = {str(ticket.uuid) for ticket in result["context"]["tickets"]}
        self.assertIn(str(member_ticket.uuid), uuids)
        self.assertIn(str(api_ticket.uuid), uuids)

    @patch("larpmanager.views.user.ticket.get_context")
    def test_optimistic_lock_version(self, mock_get_context):
        """A web status update bumps the optimistic-lock version and emits an event."""
        association = self.get_association()
        mock_get_context.return_value = self._staff_context(association)
        ticket = self.create_larpmanager_ticket(association=association, status=TicketStatus.OPEN)
        self.assertEqual(ticket.version, 0)

        request = self._make_request(
            "post",
            f"/tickets/{ticket.uuid}/update/",
            {
                "status": "working",
                "priority": "high",
                "assigned_staff_discord_id": "4242",
                "analysis": "investigating",
                "base_version": "0",
            },
        )
        response = ticket_views.ticket_update(request, str(ticket.uuid))

        ticket.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ticket.version, 1)
        self.assertEqual(ticket.status, TicketStatus.WORKING)
        self.assertEqual(ticket.priority, TicketPriority.HIGH)
        self.assertEqual(ticket.assigned_staff_discord_id, 4242)
        self.assertEqual(ticket.analysis, "investigating")
        self.assertTrue(
            TicketEvent.objects.filter(ticket=ticket, event_type=TicketEvent.EventType.STATUS_CHANGED).exists()
        )

    @patch("larpmanager.views.user.ticket.get_context")
    def test_message_arrival_does_not_409(self, mock_get_context):
        """A message arriving between form-load and POST does not cause a 409."""
        association = self.get_association()
        mock_get_context.return_value = self._staff_context(association)
        ticket = self.create_larpmanager_ticket(association=association, status=TicketStatus.OPEN)

        # Background write: a transcript message does not bump the lock version.
        TicketMessage.objects.create(ticket=ticket, discord_message_id=111, author_name="Bot", content="hello")
        ticket.refresh_from_db()
        self.assertEqual(ticket.version, 0)

        request = self._make_request(
            "post",
            f"/tickets/{ticket.uuid}/update/",
            {
                "status": "working",
                "priority": "low",
                "assigned_staff_discord_id": "",
                "analysis": "",
                "base_version": "0",
            },
        )
        response = ticket_views.ticket_update(request, str(ticket.uuid))

        self.assertEqual(response.status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual(ticket.version, 1)

    @patch("larpmanager.views.user.ticket.render", side_effect=_capture_render)
    @patch("larpmanager.views.user.ticket.get_context")
    def test_concurrent_edit_409(self, mock_get_context, mock_render):  # noqa: ARG002
        """A stale base_version loses the conditional update and returns 409."""
        association = self.get_association()
        mock_get_context.return_value = self._staff_context(association)
        ticket = self.create_larpmanager_ticket(association=association, status=TicketStatus.OPEN)
        self.assertEqual(ticket.version, 0)

        # First staffer's edit wins and bumps the version.
        first = self._make_request(
            "post",
            f"/tickets/{ticket.uuid}/update/",
            {
                "status": "working",
                "priority": "low",
                "assigned_staff_discord_id": "",
                "analysis": "",
                "base_version": "0",
            },
        )
        first_response = ticket_views.ticket_update(first, str(ticket.uuid))
        self.assertEqual(first_response.status_code, 302)

        # Second staffer posts the same stale base_version: the conditional update
        # matches no row and returns 409, so the first edit is not lost.
        second = self._make_request(
            "post",
            f"/tickets/{ticket.uuid}/update/",
            {
                "status": "done",
                "priority": "low",
                "assigned_staff_discord_id": "",
                "analysis": "",
                "base_version": "0",
            },
        )
        second_response = ticket_views.ticket_update(second, str(ticket.uuid))
        self.assertEqual(second_response.status_code, 409)

        ticket.refresh_from_db()
        self.assertEqual(ticket.version, 1)
        self.assertEqual(ticket.status, TicketStatus.WORKING)

    @patch("larpmanager.views.user.ticket.get_context")
    def test_update_status_done_emits_closed(self, mock_get_context):
        """Setting status=done via the update form emits CLOSED and sets closed_at."""
        association = self.get_association()
        mock_get_context.return_value = self._staff_context(association)
        ticket = self.create_larpmanager_ticket(association=association, status=TicketStatus.WORKING)

        request = self._make_request(
            "post",
            f"/tickets/{ticket.uuid}/update/",
            {
                "status": "done",
                "priority": "low",
                "assigned_staff_discord_id": "",
                "analysis": "",
                "base_version": "0",
            },
        )
        response = ticket_views.ticket_update(request, str(ticket.uuid))

        self.assertEqual(response.status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, TicketStatus.DONE)
        self.assertIsNotNone(ticket.closed_at)
        self.assertTrue(
            TicketEvent.objects.filter(ticket=ticket, event_type=TicketEvent.EventType.CLOSED).exists()
        )
        self.assertFalse(
            TicketEvent.objects.filter(ticket=ticket, event_type=TicketEvent.EventType.STATUS_CHANGED).exists()
        )

    @patch("larpmanager.views.user.ticket.get_context")
    def test_reply_creates_message_and_event(self, mock_get_context):
        """A web reply persists a TicketMessage and emits a REPLY event."""
        association = self.get_association()
        member = self.create_member()
        mock_get_context.return_value = self._staff_context(association, member=member)
        ticket = self.create_larpmanager_ticket(association=association, member=member)

        request = self._make_request("post", f"/tickets/{ticket.uuid}/reply/", {"content": "Hello from the web"})
        response = ticket_views.ticket_reply(request, str(ticket.uuid))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(TicketMessage.objects.filter(ticket=ticket, content="Hello from the web").exists())
        self.assertTrue(TicketEvent.objects.filter(ticket=ticket, event_type=TicketEvent.EventType.REPLY).exists())

    @patch("larpmanager.views.user.ticket.has_association_permission", return_value=False)
    def test_memberless_user_cannot_access_member_none_ticket(self, _mock_perm):
        """A member-less actor must not open member=None tickets (None == None guard)."""
        association = self.get_association()
        request = self._make_request("get", "/")
        context = {"association_id": association.id, "member": None, "is_admin": False}
        ticket = self.create_larpmanager_ticket(association=association, member=None)

        with self.assertRaises(Http404):
            ticket_views._get_ticket_accessible(request, context, str(ticket.uuid))

    @patch("larpmanager.views.user.ticket.has_association_permission", return_value=True)
    @patch("larpmanager.views.user.ticket.get_context")
    def test_staff_with_exe_tickets_permission_can_mutate(self, mock_get_context, _mock_perm):
        """A non-admin staffer with exe_tickets can mutate tickets, not just admins."""
        association = self.get_association()
        mock_get_context.return_value = {
            "association_id": association.id,
            "member": self.create_member(),
            "is_admin": False,
        }
        ticket = self.create_larpmanager_ticket(association=association, member=None, status=TicketStatus.OPEN)

        request = self._make_request(
            "post",
            f"/tickets/{ticket.uuid}/update/",
            {
                "status": "working",
                "priority": "low",
                "assigned_staff_discord_id": "",
                "analysis": "",
                "base_version": "0",
            },
        )
        response = ticket_views.ticket_update(request, str(ticket.uuid))

        self.assertEqual(response.status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, TicketStatus.WORKING)

    @patch("larpmanager.views.user.ticket.has_association_permission", return_value=True)
    def test_detail_context_flags_staff_can_manage(self, _mock_perm):
        """The detail context exposes can_manage_tickets for non-admin staff."""
        association = self.get_association()
        request = self._make_request("get", "/")
        context = {"association_id": association.id, "member": self.create_member(), "is_admin": False}
        ticket = self.create_larpmanager_ticket(association=association, member=None)

        context = ticket_views._ticket_detail_context(request, context, ticket)

        self.assertTrue(context["can_manage_tickets"])

    @patch("larpmanager.views.user.ticket.render", side_effect=_capture_render)
    @patch("larpmanager.views.user.ticket.get_context")
    def test_member_list_counts_scoped_to_member(self, mock_get_context, mock_render):  # noqa: ARG002
        """Member-list tab counts reflect the member's own tickets, not the association."""
        association = self.get_association()
        member = self.create_member()
        other_member = self.create_member(user=self.create_user(username="other"))
        mock_get_context.return_value = {"association_id": association.id, "member": member, "is_admin": False}

        self.create_larpmanager_ticket(association=association, member=member, status=TicketStatus.OPEN)
        self.create_larpmanager_ticket(association=association, member=member, status=TicketStatus.DONE)
        self.create_larpmanager_ticket(association=association, member=other_member, status=TicketStatus.OPEN)
        self.create_larpmanager_ticket(association=association, member=None, status=TicketStatus.OPEN)

        request = self._make_request("get", "/tickets/")
        result = ticket_views.tickets(request)

        counts = result["context"]["counts"]
        self.assertEqual(counts["total"], 2)
        self.assertEqual(counts["active"], 1)
        self.assertEqual(counts["done"], 1)
        tickets = list(result["context"]["tickets"])
        self.assertEqual(len(tickets), 2)
        self.assertEqual({ticket.member_id for ticket in tickets}, {member.id})

    @patch("larpmanager.views.user.ticket.get_context")
    def test_state_endpoint(self, mock_get_context):
        """GET state/ returns the live snapshot fields used by the poller."""
        association = self.get_association()
        mock_get_context.return_value = self._staff_context(association)
        ticket = self.create_larpmanager_ticket(
            association=association,
            status=TicketStatus.WORKING,
            priority=TicketPriority.HIGH,
            assigned_staff_discord_id=777,
            version=3,
        )
        TicketMessage.objects.create(ticket=ticket, discord_message_id=10, author_name="A", content="one")
        TicketMessage.objects.create(ticket=ticket, discord_message_id=11, author_name="B", content="two")

        request = self._make_request("get", f"/tickets/{ticket.uuid}/state/")
        response = ticket_views.ticket_state(request, str(ticket.uuid))

        data = json.loads(response.content)
        self.assertEqual(data["status"], TicketStatus.WORKING)
        self.assertEqual(data["priority"], TicketPriority.HIGH)
        self.assertEqual(data["status_display"], "Working")
        self.assertEqual(data["priority_display"], "High")
        self.assertEqual(data["version"], 3)
        self.assertEqual(data["assigned_staff_discord_id"], 777)
        last_message = TicketMessage.objects.filter(ticket=ticket).order_by("id").last()
        self.assertEqual(data["last_message_id"], last_message.id)

    @patch("larpmanager.views.user.ticket.get_context")
    def test_messages_endpoint(self, mock_get_context):
        """GET messages/?after= returns only rows after the given id."""
        association = self.get_association()
        mock_get_context.return_value = self._staff_context(association)
        ticket = self.create_larpmanager_ticket(association=association)
        first = TicketMessage.objects.create(ticket=ticket, discord_message_id=1, author_name="A", content="one")
        TicketMessage.objects.create(ticket=ticket, discord_message_id=2, author_name="B", content="two")
        TicketMessage.objects.create(ticket=ticket, discord_message_id=3, author_name="C", content="three")

        request = self._make_request("get", f"/tickets/{ticket.uuid}/messages/", {"after": str(first.id)})
        response = ticket_views.ticket_messages(request, str(ticket.uuid))

        data = json.loads(response.content)
        self.assertEqual(len(data["messages"]), 2)
        self.assertEqual({message["author_name"] for message in data["messages"]}, {"B", "C"})
