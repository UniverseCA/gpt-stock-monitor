from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from gpt_stock_monitor.config import (
    AppConfig,
    load_config,
    validate_final_url,
    validate_shop_url,
)

VALID_CONFIG = """\
monitors:
  - id: ldxp-demo
    name: \u5361\u5bc6 28
    url: https://pay.ldxp.cn/shop/NIFGEAC5
    categories:
      - GPT-plus\u6210\u54c1\u53f7
"""


def write_config(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "monitors.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_loads_valid_config_and_exposes_shop_id(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, VALID_CONFIG))

    assert isinstance(config, AppConfig)
    assert config.monitors[0].id == "ldxp-demo"
    assert config.monitors[0].name == "\u5361\u5bc6 28"
    assert config.monitors[0].categories == ("GPT-plus\u6210\u54c1\u53f7",)
    assert config.monitors[0].shop_id == "NIFGEAC5"


def test_rejects_duplicate_monitor_ids(tmp_path: Path) -> None:
    content = (
        VALID_CONFIG
        + """\
  - id: ldxp-demo
    name: Another
    url: https://pay.ldxp.cn/shop/OTHER_2
    categories: [Category]
"""
    )

    with pytest.raises(ValidationError, match="duplicate monitor id"):
        load_config(write_config(tmp_path, content))


@pytest.mark.parametrize(
    "categories",
    ["[]", "['']", "['   ']"],
)
def test_rejects_empty_categories_and_empty_category_names(tmp_path: Path, categories: str) -> None:
    content = VALID_CONFIG.replace(
        "      - GPT-plus\u6210\u54c1\u53f7", f"    categories: {categories}"
    )
    content = content.replace("    categories:\n    categories:", "    categories:")

    with pytest.raises(ValidationError):
        load_config(write_config(tmp_path, content))


@pytest.mark.parametrize(
    "content",
    [
        VALID_CONFIG + "unexpected: true\n",
        VALID_CONFIG.replace(
            "    name: \u5361\u5bc6 28", "    name: \u5361\u5bc6 28\n    unexpected: true"
        ),
    ],
)
def test_rejects_unknown_fields(tmp_path: Path, content: str) -> None:
    with pytest.raises(ValidationError, match="unexpected"):
        load_config(write_config(tmp_path, content))


@pytest.mark.parametrize(
    "value",
    [
        "http://pay.ldxp.cn/shop/NIFGEAC5",
        "https://pay.ldxp.cn:8443/shop/NIFGEAC5",
        "https://user@pay.ldxp.cn/shop/NIFGEAC5",
        "https://user:password@pay.ldxp.cn/shop/NIFGEAC5",
        "https://pay.ldxp.cn/shop/NIFGEAC5?",
        "https://pay.ldxp.cn/shop/NIFGEAC5?source=test",
        "https://pay.ldxp.cn/shop/NIFGEAC5#",
        "https://pay.ldxp.cn/shop/NIFGEAC5#products",
        "https://evil.example/shop/NIFGEAC5",
        "https://pay.ldxp.cn.evil.example/shop/NIFGEAC5",
        "https://evil-pay.ldxp.cn/shop/NIFGEAC5",
        "https://pay.ldxp.cn/shop/NIFGEAC5/extra",
        "https://pay.ldxp.cn/shop/NIFGEAC5/",
        "https://pay.ldxp.cn/shop/invalid.id",
        "https://pay.ldxp.cn/shop/%2Fetc",
        "https://pay.ldxp.cn/shop/\u5546\u5e97",
    ],
)
def test_validate_shop_url_rejects_unsafe_or_malformed_urls(value: str) -> None:
    with pytest.raises(ValueError):
        validate_shop_url(value)


def test_validate_shop_url_accepts_default_https_port() -> None:
    result = validate_shop_url("https://pay.ldxp.cn:443/shop/shop_01-test")

    assert result.host == "pay.ldxp.cn"


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example/shop/NIFGEAC5",
        "https://pay.ldxp.cn/shop/NIFGEAC5/",
        "https://pay.ldxp.cn/shop/NIFGEAC5?redirected=true",
        "https://pay.ldxp.cn/shop/OTHER",
    ],
)
def test_validate_final_url_revalidates_structure_and_shop_id(value: str) -> None:
    with pytest.raises(ValueError):
        validate_final_url("NIFGEAC5", value)


def test_validate_final_url_accepts_same_shop() -> None:
    validate_final_url("NIFGEAC5", "https://pay.ldxp.cn/shop/NIFGEAC5")


@pytest.mark.parametrize("content", ["", "- not-a-mapping\n"])
def test_load_config_rejects_empty_or_non_mapping_documents(tmp_path: Path, content: str) -> None:
    with pytest.raises(ValueError, match="mapping"):
        load_config(write_config(tmp_path, content))


def test_validation_error_does_not_echo_unknown_webhook_secret(tmp_path: Path) -> None:
    secret = "super-secret-webhook-value"
    content = VALID_CONFIG + f"webhook: {secret}\n"

    with pytest.raises(ValidationError) as exc_info:
        load_config(write_config(tmp_path, content))

    assert secret not in str(exc_info.value)
