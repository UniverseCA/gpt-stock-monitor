from __future__ import annotations

import _socket
import socket
import sys
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_ROOT))


@pytest.fixture(autouse=True)
def block_public_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make accidental public network access fail at the socket boundary."""
    original_socket = socket.socket
    original_socket_type = _socket.socket
    original_socketpair = socket.socketpair
    socketpair_code = getattr(original_socketpair, "__code__", None)

    class GuardedSocket(original_socket):
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._allow_local_ipc = sys._getframe(1).f_code is socketpair_code
            original_socket_type.__init__(self, *args, **kwargs)  # type: ignore[arg-type]
            self._io_refs = 0
            self._closed = False

        def __enter__(self) -> GuardedSocket:
            return self

        def __exit__(self, *args: object) -> None:
            del args
            self.close()

        def connect(self, address: object) -> None:
            if self._allow_local_ipc and sys._getframe(1).f_code is socketpair_code:
                return super().connect(address)  # type: ignore[arg-type]
            raise AssertionError("network access is disabled in tests")

        def connect_ex(self, address: object) -> int:
            del address
            raise AssertionError("network access is disabled in tests")

        def send(self, data: object, flags: int = 0) -> int:
            if self._allow_local_ipc:
                return super().send(data, flags)  # type: ignore[arg-type]
            raise AssertionError("network access is disabled in tests")

        def sendall(self, data: object, flags: int = 0) -> None:
            if self._allow_local_ipc:
                return super().sendall(data, flags)  # type: ignore[arg-type]
            raise AssertionError("network access is disabled in tests")

        def sendto(self, data: object, *args: object) -> int:
            del data, args
            raise AssertionError("network access is disabled in tests")

        if hasattr(original_socket, "sendmsg"):

            def sendmsg(self, *args: object, **kwargs: object) -> int:
                del args, kwargs
                raise AssertionError("network access is disabled in tests")

        def accept(self) -> tuple[GuardedSocket, object]:
            descriptor, address = super()._accept()
            connection = GuardedSocket(
                self.family,
                self.type,
                self.proto,
                fileno=descriptor,
            )
            if self._allow_local_ipc:
                connection._allow_local_ipc = True
            return connection, address

    def guarded_socketpair(*args: object, **kwargs: object) -> tuple[GuardedSocket, GuardedSocket]:
        left, right = original_socketpair(*args, **kwargs)  # type: ignore[arg-type]
        left._allow_local_ipc = True
        right._allow_local_ipc = True
        return left, right

    def blocked(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("network access is disabled in tests")

    monkeypatch.setattr(socket, "socket", GuardedSocket)
    monkeypatch.setattr(socket, "SocketType", GuardedSocket)
    monkeypatch.setattr(_socket, "socket", GuardedSocket)
    monkeypatch.setattr(socket, "socketpair", guarded_socketpair)
    monkeypatch.setattr(socket, "create_connection", blocked)
    for name in (
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "gethostbyaddr",
        "getnameinfo",
        "getfqdn",
    ):
        monkeypatch.setattr(socket, name, blocked)
        if hasattr(_socket, name):
            monkeypatch.setattr(_socket, name, blocked)
