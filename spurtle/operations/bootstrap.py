"""Porringer API initialisation without Qt.

Extracts the service-creation logic from ``qt.py`` so that CLI commands
and tests can obtain a configured ``(Client, API, ResolvedConfig)``
tuple without importing PySide6 or qasync.
"""

from __future__ import annotations

import logging

from porringer import API

from spurtle.client import Client
from spurtle.resolution import resolve_config, resolve_update_config
from spurtle.schema import ResolvedConfig

logger = logging.getLogger(__name__)


def init_services() -> tuple[Client, API, ResolvedConfig]:
    """Create and configure core services without Qt.

    Returns:
        A ``(Client, porringer API, resolved config)`` tuple ready for
        use by CLI commands or headless operations.
    """
    config = resolve_config()
    client = Client()

    porringer = API()

    update_config = resolve_update_config(config)
    client.initialize_updater(update_config)

    logger.info(
        "Spurtle v%s started (channel: %s, source: %s, projects: %d)",
        client.version,
        update_config.channel.name,
        update_config.repo_url,
        len(config.projects),
    )

    return client, porringer, config
