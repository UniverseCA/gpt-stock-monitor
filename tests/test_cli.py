from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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

    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.invalid/secret")
    monkeypatch.setattr("gpt_stock_monitor.cli.load_config", fake_load_config)
    monkeypatch.setattr("gpt_stock_monitor.cli.run_once", fake_run_once)

    assert main([], services_factory=services_factory) == 0
    captured = capsys.readouterr()
    assert observed == {"path": Path("config/monitors.yaml"), "dry_run": False}
    assert captured.out == '{"a":[2,1],"z":"中文"}\n'
    assert captured.err == ""


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

    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
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


@pytest.mark.parametrize("result_code", [0, 3])
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


def test_unknown_exception_traceback_is_redacted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "top-secret-token"
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


def test_main_does_not_catch_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gpt_stock_monitor.cli.load_config", lambda path: app_config())

    @asynccontextmanager
    async def interrupted_factory(webhook_url: str | None) -> AsyncIterator[RuntimeServices]:
        del webhook_url
        raise KeyboardInterrupt
        yield

    with pytest.raises(KeyboardInterrupt):
        main(["--dry-run"], services_factory=interrupted_factory)
