from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_ROOT))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "allow_network: allow this test to open network sockets",
    )


@pytest.fixture(autouse=True)
def block_public_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make accidental public network access fail at the socket boundary."""
    if request.node.get_closest_marker("allow_network") is not None:
        return

    original_connect = socket.socket.connect

    def guarded_connect(sock: socket.socket, address: object) -> object:
        caller = sys._getframe(1)
        if caller.f_code is socket.socketpair.__code__:
            return original_connect(sock, address)  # type: ignore[arg-type]
        raise AssertionError("network access is disabled in tests")

    def blocked_connect_ex(sock: socket.socket, address: object) -> int:
        del sock, address
        raise AssertionError("network access is disabled in tests")

    def blocked_create_connection(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("network access is disabled in tests")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked_connect_ex)
    monkeypatch.setattr(socket, "create_connection", blocked_create_connection)
