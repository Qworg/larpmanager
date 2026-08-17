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
"""Tests for shareable transcript links (W8 / Req 4d)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

from django.http import Http404
from django.test import RequestFactory
from django.utils import timezone
from freezegun import freeze_time

from larpmanager.models.ticket_message import TicketMessage
from larpmanager.models.ticket_transcript_link import TicketTranscriptLink
from larpmanager.tests.unit.base import BaseTestCase
from larpmanager.views.user import ticket as ticket_views


class _CapturedRender(dict[str, Any]):
    """Dict-like render stand-in that also exposes the response ``status_code``."""

    status_code: int = 200


def _capture_render(_request: Any, template_name: str, context: dict | None = None, **kwargs: Any) -> _CapturedRender:
    """Stand in for ``django.shortcuts.render`` to capture the template context."""
    rendered = _CapturedRender({"template": template_name, "context": context or {}})
    rendered.status_code = kwargs.get("status", 200)
    return rendered


class TestTranscriptLink(BaseTestCase):
    """Test cases for shareable transcript links."""

    def _make_request(self, method: str = "get", path: str = "/", data: dict | None = None) -> Any:
        """Build an authenticated RequestFactory request."""
        factory = RequestFactory()
        request = factory.post(path, data or {}) if method == "post" else factory.get(path, data or {})
        request.user = self.get_user()
        request.session = {}
        request.resolver_match = None
        return request

    def _context(self, association: Any, member: Any = None) -> dict[str, Any]:
        """Return a minimal context dict for patching ``get_context``."""
        return {
            "association_id": association.id,
            "member": member,
            "is_admin": False,
            "features": {},
        }

    def _create_link(self, ticket: Any, association: Any, **kwargs: Any) -> Any:
        """Create a transcript link with sensible defaults."""
        defaults = {
            "ticket": ticket,
            "association": association,
            "created_by": self.get_member(),
            "expires_at": timezone.now() + timedelta(hours=24),
            "max_opens": 5,
        }
        defaults.update(kwargs)
        return TicketTranscriptLink.objects.create(**defaults)

    @patch("larpmanager.views.user.ticket.get_context")
    def test_link_association_scope(self, mock_get_context):
        """A link 404s outside its association, and for a mismatched ticket association."""
        association = self.get_association()
        other_association = self.create_association(name="Other", slug="other-association")
        ticket = self.create_larpmanager_ticket(association=association)
        link = self._create_link(ticket, association)

        # Member of another association: the link's association does not match.
        mock_get_context.return_value = self._context(other_association, member=None)
        request = self._make_request("get", f"/transcript/{link.token}/")
        with self.assertRaises(Http404):
            ticket_views.transcript_share(request, link.token)
        link.refresh_from_db()
        self.assertEqual(link.open_count, 0)

        # Link whose ticket belongs to a different association than the link
        # itself must also 404, even in a matching link-association context.
        mismatched = self._create_link(ticket, other_association)
        mock_get_context.return_value = self._context(other_association, member=None)
        request = self._make_request("get", f"/transcript/{mismatched.token}/")
        with self.assertRaises(Http404):
            ticket_views.transcript_share(request, mismatched.token)
        mismatched.refresh_from_db()
        self.assertEqual(mismatched.open_count, 0)

    @patch("larpmanager.views.user.ticket.render", side_effect=_capture_render)
    @patch("larpmanager.views.user.ticket.get_context")
    def test_link_max_opens(self, mock_get_context, _mock_render):
        """A link 404s once its open cap is exhausted."""
        association = self.get_association()
        mock_get_context.return_value = self._context(association, member=self.get_member())
        ticket = self.create_larpmanager_ticket(association=association)
        link = self._create_link(ticket, association, max_opens=3)
        request = self._make_request("get", f"/transcript/{link.token}/")

        for _unused in range(3):
            response = ticket_views.transcript_share(request, link.token)
            self.assertEqual(response.status_code, 200)

        link.refresh_from_db()
        self.assertEqual(link.open_count, 3)
        with self.assertRaises(Http404):
            ticket_views.transcript_share(request, link.token)

    @patch("larpmanager.views.user.ticket.get_context")
    def test_link_expiry(self, mock_get_context):
        """An expired link 404s and is not counted as an open."""
        association = self.get_association()
        mock_get_context.return_value = self._context(association, member=self.get_member())
        ticket = self.create_larpmanager_ticket(association=association)
        link = self._create_link(ticket, association)
        request = self._make_request("get", f"/transcript/{link.token}/")

        with freeze_time(timezone.now() + timedelta(hours=25)):
            with self.assertRaises(Http404):
                ticket_views.transcript_share(request, link.token)

        link.refresh_from_db()
        self.assertEqual(link.open_count, 0)

    @patch("larpmanager.views.user.ticket.render", side_effect=_capture_render)
    @patch("larpmanager.views.user.ticket.get_context")
    def test_link_access_logged(self, mock_get_context, _mock_render):
        """A successful open increments the count and emits an access log."""
        association = self.get_association()
        member = self.get_member()
        mock_get_context.return_value = self._context(association, member=member)
        ticket = self.create_larpmanager_ticket(association=association)
        TicketMessage.objects.create(ticket=ticket, discord_message_id=1, author_name="Staff", content="hello there")
        link = self._create_link(ticket, association)
        request = self._make_request("get", f"/transcript/{link.token}/")

        with self.assertLogs("larpmanager.views.user.ticket", level="INFO") as captured:
            response = ticket_views.transcript_share(request, link.token)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["context"]["ticket"], ticket)
        self.assertEqual(len(response["context"]["ticket_messages"]), 1)
        self.assertIn("hello there", response["context"]["transcript"])
        link.refresh_from_db()
        self.assertEqual(link.open_count, 1)
        self.assertTrue(any("transcript link opened" in message for message in captured.output))

    @patch("larpmanager.views.user.ticket.render", side_effect=_capture_render)
    @patch("larpmanager.views.user.ticket.get_context")
    def test_create_and_revoke_link(self, mock_get_context, _mock_render):
        """Staff create a link (24h TTL, max 5) and can revoke it."""
        association = self.get_association()
        mock_get_context.return_value = self._context(association, member=self.get_member())
        ticket = self.create_larpmanager_ticket(association=association)

        request = self._make_request("post", f"/tickets/{ticket.uuid}/transcript-link/")
        response = ticket_views.ticket_transcript_link_create(request, str(ticket.uuid))
        self.assertEqual(response.status_code, 302)

        link = TicketTranscriptLink.objects.get(ticket=ticket)
        self.assertEqual(link.max_opens, 5)
        self.assertGreater(link.expires_at, timezone.now())
        self.assertEqual(link.association_id, association.id)

        revoke = self._make_request("post", f"/tickets/{ticket.uuid}/transcript-link/revoke/", {"token": link.token})
        response = ticket_views.ticket_transcript_link_revoke(revoke, str(ticket.uuid))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(TicketTranscriptLink.objects.filter(pk=link.pk).exists())
        self.assertTrue(TicketTranscriptLink.all_objects.filter(pk=link.pk).exists())
