"""Command-line entry point for one monitor cycle."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import traceback
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from playwright.async_api import async_playwright

from gpt_stock_monitor.config import AppConfig, ConfigError, load_config
from gpt_stock_monitor.monitor import Notifier, run_once
from gpt_stock_monitor.notifiers.feishu import FeishuError, FeishuNotifier
from gpt_stock_monitor.sites.base import (
    CategoryNotFoundError,
    InteractiveChallengeError,
    SiteAdapter,
    SiteNavigationError,
    SuspiciousExtractionError,
)
from gpt_stock_monitor.sites.ldxp import LdxpAdapter
from gpt_stock_monitor.state import StateRepository, StateRepositoryError
from gpt_stock_monitor.state_git import GitStateRepository

_DEFAULT_CONFIG = Path("config/monitors.yaml")
_FEISHU_WEBHOOK = re.compile(
    r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[^\s\"'<>]+"
)
_FEISHU_WEBHOOK_PATH = re.compile(
    r"^/open-apis/bot/v2/hook/(?P<token>[A-Za-z0-9_-]{16,128})$"
)
_RUN_ERRORS = (
    StateRepositoryError,
    FeishuError,
    SiteNavigationError,
    InteractiveChallengeError,
    CategoryNotFoundError,
    SuspiciousExtractionError,
)


@dataclass(frozen=True)
class RuntimeServices:
    """External boundaries used by one monitor run."""

    state_repo: StateRepository
    adapter: SiteAdapter
    notifier: Notifier


class _NoopNotifier:
    async def send_parts(self, parts: Sequence[dict[str, object]]) -> None:
        del parts


@asynccontextmanager
async def build_production_services(
    webhook_url: str | None,
) -> AsyncIterator[RuntimeServices]:
    """Create and reliably close ordinary production service boundaries."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            repository = GitStateRepository(
                Path.cwd(),
                Path(tempfile.gettempdir()) / "gpt-stock-monitor",
            )
            if webhook_url is None:
                yield RuntimeServices(repository, LdxpAdapter(page), _NoopNotifier())
            else:
                async with httpx.AsyncClient() as client:
                    yield RuntimeServices(
                        repository,
                        LdxpAdapter(page),
                        FeishuNotifier(webhook_url, client=client),
                    )
        finally:
            await browser.close()


ServicesFactory = Callable[
    [str | None],
    AbstractAsyncContextManager[RuntimeServices],
]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gpt-stock-monitor")
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _validate_webhook_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        value != value.strip()
        or parsed.scheme != "https"
        or parsed.hostname != "open.feishu.cn"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        return None
    match = _FEISHU_WEBHOOK_PATH.fullmatch(parsed.path)
    return match.group("token") if match is not None else None


def _redact(text: str, webhook_url: str | None, webhook_token: str | None) -> str:
    if webhook_url:
        text = text.replace(webhook_url, "[REDACTED]")
    if webhook_token:
        text = text.replace(webhook_token, "[REDACTED]")
    return _FEISHU_WEBHOOK.sub("[REDACTED]", text)


def _write_stdout_utf8(text: str) -> None:
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(text.encode("utf-8"))
        return
    sys.stdout.write(text)


async def _run(
    config: AppConfig,
    *,
    dry_run: bool,
    webhook_url: str | None,
    services_factory: ServicesFactory,
) -> tuple[int, dict[str, object]]:
    async with services_factory(webhook_url) as services:
        result = await run_once(
            config,
            services.state_repo,
            services.adapter,
            services.notifier,
            dry_run=dry_run,
            checked_at=datetime.now(UTC),
        )
    return result.exit_code, result.output


def main(
    argv: Sequence[str] | None = None,
    services_factory: ServicesFactory = build_production_services,
) -> int:
    """Run one cycle and return its process exit code."""
    arguments = _parser().parse_args(argv)
    webhook_url = None if arguments.dry_run else os.environ.get("FEISHU_WEBHOOK_URL")
    webhook_token = None
    if not arguments.dry_run and not webhook_url:
        sys.stderr.write("configuration error: FEISHU_WEBHOOK_URL is required\n")
        return 2
    if webhook_url is not None:
        webhook_token = _validate_webhook_url(webhook_url)
        if webhook_token is None:
            sys.stderr.write("configuration error: invalid FEISHU_WEBHOOK_URL\n")
            return 2

    try:
        try:
            config = load_config(arguments.config)
        except ConfigError:
            sys.stderr.write("configuration error: invalid configuration\n")
            return 2
        except OSError:
            sys.stderr.write("configuration error: configuration file could not be read\n")
            return 2

        exit_code, output = asyncio.run(
            _run(
                config,
                dry_run=arguments.dry_run,
                webhook_url=webhook_url,
                services_factory=services_factory,
            )
        )
        serialized = json.dumps(
            output,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        _write_stdout_utf8(f"{_redact(serialized, webhook_url, webhook_token)}\n")
    except _RUN_ERRORS:
        sys.stderr.write("monitor run failed\n")
        return 3
    except Exception:
        diagnostic = _redact(traceback.format_exc(), webhook_url, webhook_token)
        sys.stderr.write(diagnostic)
        return 3

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
