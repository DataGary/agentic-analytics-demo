import os
import socket

import pytest


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def pytest_collection_modifyitems(config, items):
    """Integration tests need the compose stack; skip them cleanly when it isn't up."""
    if _port_open("localhost", int(os.environ.get("PG_PORT", 5432))):
        return
    skip = pytest.mark.skip(reason="docker compose stack not running (postgres not reachable)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
