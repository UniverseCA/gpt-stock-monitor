from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from gpt_stock_monitor.config import AppConfig, MonitorConfig
from gpt_stock_monitor.monitor import _collect
from gpt_stock_monitor.sites.base import CategoryObservation, SuspiciousExtractionError


class _FailingAdapter:
    async def collect(self, config: MonitorConfig) -> Mapping[str, CategoryObservation]:
        raise SuspiciousExtractionError("product modal lacks one canonical item link")


def test_site_error_is_reported_on_stderr_with_monitor_and_step(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = AppConfig.model_validate(
        {
            "monitors": [
                {
                    "id": "aileader",
                    "name": "AI 源头渠道",
                    "url": "https://wzyp.cn/shop/Aileader",
                    "categories": ["G成品号"],
                }
            ]
        }
    )

    collected = asyncio.run(_collect(config, _FailingAdapter()))

    assert [result.failure_reason for result in collected.values()] == ["SuspiciousExtractionError"]
    assert capsys.readouterr().err == (
        "site error: aileader: SuspiciousExtractionError: "
        "product modal lacks one canonical item link\n"
    )
