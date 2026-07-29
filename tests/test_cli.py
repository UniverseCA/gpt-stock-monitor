from __future__ import annotations

import _socket
import json
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Never

import pytest

from gpt_stock_monitor.cli import RuntimeServices, main
from gpt_stock_monitor.config import AppConfig, ConfigError
from gpt_stock_monitor.monitor import RunResult
from gpt_stock_monitor.state import StateRepositoryError


def app_config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "monitors": [
                {
                    "id": "shop",
                    "name": "测试商店",
                    "url": "https://pay.ldxp.cn/shop/NIFGEAC5",
                    "categories": ["GPT 商品"],
                }
            ]
        }
    )


@asynccontextmanager
async def services_factory(webhook_url: str | None) -> AsyncIterator[RuntimeServices]:
    yield RuntimeServices(object(), object(), object())


@pytest.mark.allow_socketpair
def test_default_config_path_and_utf8_deterministic_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    observed: dict[str, object] = {}

    def fake_load_config(path: Path) -> AppConfig:
        observed["path"] = path
        return app_config()

    async def fake_run_once(*args: object, **kwargs: object) -> RunResult:
        observed["dry_run"] = kwargs["dry_run"]
        return RunResult(0, {"z": "中文", "a": [2, 1]})

    monkeypatch.setenv(
        "FEISHU_WEBHOOK_URL",
        "https://open.feishu.cn/open-apis/bot/v2/hook/unit-test-token-1234",
    )
    monkeypatch.setattr("gpt_stock_monitor.cli.load_config", fake_load_config)
    monkeypatch.setattr("gpt_stock_monitor.cli.run_once", fake_run_once)

    assert main([], services_factory=services_factory) == 0
    captured = capsys.readouterr()
    assert observed == {"path": Path("config/monitors.yaml"), "dry_run": False}
    assert captured.out == '{"a":[2,1],"z":"中文"}\n'
    assert captured.err == ""


@pytest.mark.allow_socketpair
def test_config_override_and_dry_run_do_not_read_or_pass_webhook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "custom.yaml"
    observed: dict[str, object] = {}

    def fake_load_config(path: Path) -> AppConfig:
        observed["path"] = path
        return app_config()

    @asynccontextmanager
    async def dry_factory(webhook_url: str | None) -> AsyncIterator[RuntimeServices]:
        observed["webhook"] = webhook_url
        yield RuntimeServices(object(), object(), object())

    async def fake_run_once(*args: object, **kwargs: object) -> RunResult:
        observed["dry_run"] = kwargs["dry_run"]
        return RunResult(0, {"changes": []})

    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "http://internal.invalid/hook/a")
    monkeypatch.setattr("gpt_stock_monitor.cli.load_config", fake_load_config)
    monkeypatch.setattr("gpt_stock_monitor.cli.run_once", fake_run_once)

    assert (
        main(
            ["--config", str(config_path), "--dry-run"],
            services_factory=dry_factory,
        )
        == 0
    )
    captured = capsys.readouterr()
    assert observed == {"path": config_path, "webhook": None, "dry_run": True}
    assert captured.out == '{"changes":[]}\n'
    assert captured.err == ""


def test_missing_webhook_is_safe_configuration_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    monkeypatch.setattr("gpt_stock_monitor.cli.load_config", lambda path: app_config())

    assert main([], services_factory=services_factory) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "configuration error: FEISHU_WEBHOOK_URL is required\n"


@pytest.mark.allow_socketpair
@pytest.mark.parametrize(
    "webhook",
    [
        "http://open.feishu.cn/open-apis/bot/v2/hook/valid-token-123456",
        "https://internal.example/open-apis/bot/v2/hook/valid-token-123456",
        "https://user@open.feishu.cn/open-apis/bot/v2/hook/valid-token-123456",
        "https://open.feishu.cn:8443/open-apis/bot/v2/hook/valid-token-123456",
        "https://open.feishu.cn/open-apis/bot/v2/other/valid-token-123456",
        "https://open.feishu.cn/open-apis/bot/v2/hook/a",
        "https://open.feishu.cn/open-apis/bot/v2/hook/valid-token-123456?query=1",
        "https://open.feishu.cn/open-apis/bot/v2/hook/valid-token-123456#fragment",
        "https://open.feishu.cn/open-apis/bot/v2/hook/valid-token-123456?",
        "https://open.feishu.cn/open-apis/bot/v2/hook/valid-token-123456#",
        "https://open.feishu.cn:/open-apis/bot/v2/hook/valid-token-123456",
    ],
    ids=[
        "http",
        "foreign-host",
        "userinfo",
        "non-default-port",
        "wrong-path",
        "short-token",
        "query",
        "fragment",
        "empty-query-delimiter",
        "empty-fragment-delimiter",
        "empty-port-delimiter",
    ],
)
def test_invalid_webhook_is_rejected_before_factory(
    webhook: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory_called = False
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", webhook)
    monkeypatch.setattr("gpt_stock_monitor.cli.load_config", lambda path: app_config())

    @asynccontextmanager
    async def observing_factory(value: str | None) -> AsyncIterator[RuntimeServices]:
        nonlocal factory_called
        factory_called = True
        yield RuntimeServices(object(), object(), object())

    assert main([], services_factory=observing_factory) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "configuration error: invalid FEISHU_WEBHOOK_URL\n"
    assert webhook not in captured.err
    assert "Traceback" not in captured.err
    assert not factory_called


@pytest.mark.allow_socketpair
def test_valid_canonical_webhook_reaches_factory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/valid-token-123456"
    observed: list[str | None] = []
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", webhook)
    monkeypatch.setattr("gpt_stock_monitor.cli.load_config", lambda path: app_config())

    @asynccontextmanager
    async def observing_factory(value: str | None) -> AsyncIterator[RuntimeServices]:
        observed.append(value)
        yield RuntimeServices(object(), object(), object())

    async def fake_run_once(*args: object, **kwargs: object) -> RunResult:
        del args, kwargs
        return RunResult(0, {"ok": True})

    monkeypatch.setattr("gpt_stock_monitor.cli.run_once", fake_run_once)

    assert main([], services_factory=observing_factory) == 0
    assert observed == [webhook]
    assert capsys.readouterr().out == '{"ok":true}\n'


def test_config_error_returns_two_without_exposing_config_content(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "config-secret-value"

    def fail(path: Path) -> Never:
        del path
        raise ConfigError(f"monitors.0.url: value_error {secret}")

    monkeypatch.setattr("gpt_stock_monitor.cli.load_config", fail)

    assert main(["--dry-run"], services_factory=services_factory) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "configuration error:" in captured.err
    assert secret not in captured.err


def test_missing_config_returns_safe_configuration_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret_path = tmp_path / "secret-config-name.yaml"

    assert (
        main(
            ["--config", str(secret_path), "--dry-run"],
            services_factory=services_factory,
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "configuration error: configuration file could not be read\n"
    assert str(secret_path) not in captured.err
    assert "Traceback" not in captured.err


def test_unreadable_config_returns_safe_configuration_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "permission-secret-path"

    def fail(path: Path) -> Never:
        del path
        raise PermissionError(secret)

    monkeypatch.setattr("gpt_stock_monitor.cli.load_config", fail)

    assert main(["--dry-run"], services_factory=services_factory) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "configuration error: configuration file could not be read\n"
    assert secret not in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("result_code", [0, 3])
@pytest.mark.allow_socketpair
def test_returns_run_result_exit_code_and_prints_output(
    result_code: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("gpt_stock_monitor.cli.load_config", lambda path: app_config())

    async def fake_run_once(*args: object, **kwargs: object) -> RunResult:
        return RunResult(result_code, {"exit": result_code})

    monkeypatch.setattr("gpt_stock_monitor.cli.run_once", fake_run_once)

    assert main(["--dry-run"], services_factory=services_factory) == result_code
    captured = capsys.readouterr()
    assert captured.out == json.dumps(
        {"exit": result_code},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    assert captured.err == ""


@pytest.mark.allow_socketpair
def test_non_json_output_returns_three_with_redacted_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "non-json-secret-1234"
    webhook = f"https://open.feishu.cn/open-apis/bot/v2/hook/{secret}"
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", webhook)
    monkeypatch.setattr("gpt_stock_monitor.cli.load_config", lambda path: app_config())

    async def fake_run_once(*args: object, **kwargs: object) -> RunResult:
        del args, kwargs
        return RunResult(0, {"bad": object(), "webhook": webhook})

    monkeypatch.setattr("gpt_stock_monitor.cli.run_once", fake_run_once)

    assert main([], services_factory=services_factory) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback (most recent call last):" in captured.err
    assert "TypeError" in captured.err
    assert secret not in captured.err
    assert webhook not in captured.err


@pytest.mark.allow_socketpair
def test_stdout_json_redacts_webhook(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "stdout-secret-1234"
    webhook = f"https://open.feishu.cn/open-apis/bot/v2/hook/{secret}"
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", webhook)
    monkeypatch.setattr("gpt_stock_monitor.cli.load_config", lambda path: app_config())

    async def fake_run_once(*args: object, **kwargs: object) -> RunResult:
        del args, kwargs
        return RunResult(0, {"diagnostic": webhook})

    monkeypatch.setattr("gpt_stock_monitor.cli.run_once", fake_run_once)

    assert main([], services_factory=services_factory) == 0
    captured = capsys.readouterr()
    assert captured.out == '{"diagnostic":"[REDACTED]"}\n'
    assert secret not in captured.out
    assert webhook not in captured.out
    assert captured.err == ""


@pytest.mark.allow_socketpair
def test_stdout_json_is_written_as_explicit_utf8_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class GbkTextStream:
        def __init__(self) -> None:
            self.buffer = BytesIO()

        def write(self, value: str) -> int:
            self.buffer.write(value.encode("gbk"))
            return len(value)

    output = GbkTextStream()
    monkeypatch.setattr("gpt_stock_monitor.cli.load_config", lambda path: app_config())
    monkeypatch.setattr("gpt_stock_monitor.cli.sys.stdout", output)

    async def fake_run_once(*args: object, **kwargs: object) -> RunResult:
        del args, kwargs
        return RunResult(0, {"name": "中文"})

    monkeypatch.setattr("gpt_stock_monitor.cli.run_once", fake_run_once)

    assert main(["--dry-run"], services_factory=services_factory) == 0
    assert output.buffer.getvalue() == '{"name":"中文"}\n'.encode()
    assert output.buffer.getvalue() != '{"name":"中文"}\n'.encode("gbk")


@pytest.mark.allow_socketpair
def test_stdout_write_failure_is_an_unknown_redacted_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "stdout-write-secret-1234"
    webhook = f"https://open.feishu.cn/open-apis/bot/v2/hook/{secret}"

    class FailingBuffer:
        def write(self, value: bytes) -> Never:
            del value
            raise OSError(webhook)

    class FailingStdout:
        buffer = FailingBuffer()

    monkeypatch.setenv("FEISHU_WEBHOOK_URL", webhook)
    monkeypatch.setattr("gpt_stock_monitor.cli.load_config", lambda path: app_config())
    monkeypatch.setattr("gpt_stock_monitor.cli.sys.stdout", FailingStdout())

    async def fake_run_once(*args: object, **kwargs: object) -> RunResult:
        del args, kwargs
        return RunResult(0, {"ok": True})

    monkeypatch.setattr("gpt_stock_monitor.cli.run_once", fake_run_once)

    assert main([], services_factory=services_factory) == 3
    captured = capsys.readouterr()
    assert "Traceback (most recent call last):" in captured.err
    assert "OSError" in captured.err
    assert webhook not in captured.err
    assert secret not in captured.err
    assert "[REDACTED]" in captured.err


@pytest.mark.allow_socketpair
def test_defined_application_exception_returns_three(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("gpt_stock_monitor.cli.load_config", lambda path: app_config())

    @asynccontextmanager
    async def failing_factory(webhook_url: str | None) -> AsyncIterator[RuntimeServices]:
        del webhook_url
        raise StateRepositoryError("repository details")
        yield

    assert main(["--dry-run"], services_factory=failing_factory) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "monitor run failed\n"


@pytest.mark.allow_socketpair
def test_unknown_exception_traceback_is_redacted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "top-secret-token-1234"
    webhook = f"https://open.feishu.cn/open-apis/bot/v2/hook/{secret}"
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", webhook)
    monkeypatch.setattr("gpt_stock_monitor.cli.load_config", lambda path: app_config())

    @asynccontextmanager
    async def failing_factory(webhook_url: str | None) -> AsyncIterator[RuntimeServices]:
        raise RuntimeError(f"unexpected {webhook_url}")
        yield

    assert main([], services_factory=failing_factory) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback (most recent call last):" in captured.err
    assert "RuntimeError" in captured.err
    assert secret not in captured.err
    assert webhook not in captured.err
    assert "[REDACTED]" in captured.err


@pytest.mark.allow_socketpair
def test_token_only_unknown_exception_traceback_is_redacted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    token = "token-only-secret-1234"
    webhook = f"https://open.feishu.cn/open-apis/bot/v2/hook/{token}"
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", webhook)
    monkeypatch.setattr("gpt_stock_monitor.cli.load_config", lambda path: app_config())

    @asynccontextmanager
    async def failing_factory(webhook_url: str | None) -> AsyncIterator[RuntimeServices]:
        assert webhook_url is not None
        raise RuntimeError(f"unexpected token {webhook_url.rsplit('/', 1)[-1]}")
        yield

    assert main([], services_factory=failing_factory) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback (most recent call last):" in captured.err
    assert "RuntimeError" in captured.err
    assert webhook not in captured.err
    assert token not in captured.err
    assert "[REDACTED]" in captured.err


@pytest.mark.allow_socketpair
def test_main_does_not_catch_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gpt_stock_monitor.cli.load_config", lambda path: app_config())

    @asynccontextmanager
    async def interrupted_factory(webhook_url: str | None) -> AsyncIterator[RuntimeServices]:
        del webhook_url
        raise KeyboardInterrupt
        yield

    with pytest.raises(KeyboardInterrupt):
        main(["--dry-run"], services_factory=interrupted_factory)


def test_unmarked_connect_ex_is_blocked() -> None:
    with socket.socket() as connection:
        with pytest.raises(AssertionError, match="network access is disabled in tests"):
            connection.connect_ex(("127.0.0.1", 9))


def test_unmarked_socketpair_is_blocked() -> None:
    with pytest.raises(AssertionError, match="network access is disabled in tests"):
        socket.socketpair()


@pytest.mark.allow_socketpair
def test_marked_socketpair_only_permits_local_ipc() -> None:
    left, right = socket.socketpair()
    left.close()
    right.close()

    with socket.socket() as connection:
        with pytest.raises(AssertionError, match="network access is disabled in tests"):
            connection.connect(("127.0.0.1", 9))
        with pytest.raises(AssertionError, match="network access is disabled in tests"):
            connection.connect_ex(("127.0.0.1", 9))
    with pytest.raises(AssertionError, match="network access is disabled in tests"):
        socket.create_connection(("127.0.0.1", 9))


def test_udp_sendto_is_blocked() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        with pytest.raises(AssertionError, match="network access is disabled in tests"):
            connection.sendto(b"blocked", ("127.0.0.1", 9))


@pytest.mark.skipif(not hasattr(socket.socket, "sendmsg"), reason="sendmsg unavailable")
def test_udp_sendmsg_is_blocked() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        with pytest.raises(AssertionError, match="network access is disabled in tests"):
            connection.sendmsg([b"blocked"], [], 0, ("127.0.0.1", 9))


@pytest.mark.parametrize(
    "lookup",
    [
        lambda: socket.getaddrinfo("localhost", 80),
        lambda: socket.gethostbyname("localhost"),
        lambda: socket.gethostbyname_ex("localhost"),
        lambda: socket.gethostbyaddr("127.0.0.1"),
        lambda: socket.getnameinfo(("127.0.0.1", 80), 0),
        lambda: socket.getfqdn("localhost"),
    ],
    ids=[
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname-ex",
        "gethostbyaddr",
        "getnameinfo",
        "getfqdn",
    ],
)
def test_dns_name_helpers_are_blocked(lookup: Callable[[], object]) -> None:
    with pytest.raises(AssertionError, match="network access is disabled in tests"):
        lookup()


@pytest.mark.parametrize(
    "socket_type_name",
    ["_socket.socket", "socket.SocketType"],
    ids=["_socket-socket", "socket-type"],
)
def test_direct_socket_aliases_cannot_connect_or_send(
    socket_type_name: str,
) -> None:
    socket_type: Callable[..., socket.socket]
    if socket_type_name == "_socket.socket":
        socket_type = _socket.socket
    else:
        socket_type = socket.SocketType
    connection = socket_type(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(AssertionError, match="network access is disabled in tests"):
            connection.connect(("127.0.0.1", 9))
        with pytest.raises(AssertionError, match="network access is disabled in tests"):
            connection.connect_ex(("127.0.0.1", 9))
        with pytest.raises(AssertionError, match="network access is disabled in tests"):
            connection.send(b"blocked")
        with pytest.raises(AssertionError, match="network access is disabled in tests"):
            connection.sendto(b"blocked", ("127.0.0.1", 9))
    finally:
        connection.close()
