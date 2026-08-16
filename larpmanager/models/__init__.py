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
"""LarpManager models package.

Django imports this module during app-loading; model modules are re-exported here
so the app registry discovers them (see apps.py, which also imports
``larpmanager.models.signals``).
"""

from larpmanager.models.ticket_event import TicketEvent
from larpmanager.models.ticket_message import TicketMessage

__all__ = ["TicketEvent", "TicketMessage"]
