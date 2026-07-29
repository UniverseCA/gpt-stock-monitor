from __future__ import annotations

import traceback
from pathlib import Path

import pytest

import gpt_stock_monitor.config as config_module
from gpt_stock_monitor.config import (
    AppConfig,
    ConfigError,
    load_config,
    validate_final_url,
    validate_shop_url,
)
from gpt_stock_monitor.models import state_key

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

    with pytest.raises(ConfigError, match="value_error"):
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

    with pytest.raises(ConfigError):
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
    with pytest.raises(ConfigError, match="unexpected"):
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

    with pytest.raises(ConfigError) as exc_info:
        load_config(write_config(tmp_path, content))

    assert secret not in str(exc_info.value)


def test_parser_error_does_not_leak_credentials_through_exception_chain() -> None:
    secret = "sentinel-parser-secret"
    value = f"https://user:{secret}\uff20pay.ldxp.cn/shop/NIFGEAC5"

    with pytest.raises(ValueError) as exc_info:
        validate_shop_url(value)

    formatted = "".join(
        traceback.format_exception(
            type(exc_info.value), exc_info.value, exc_info.value.__traceback__
        )
    )
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert secret not in formatted


@pytest.mark.parametrize("control", ["\t", "\r", "\n"])
@pytest.mark.parametrize(
    "template",
    [
        "https://pay.ldxp.cn{control}/shop/NIFGEAC5",
        "https://pay.ldxp.cn/{control}shop/NIFGEAC5",
        "https://pay.ldxp.cn/shop/NIF{control}GEAC5",
    ],
    ids=["host", "path-prefix", "shop-id"],
)
def test_rejects_raw_url_control_characters(control: str, template: str) -> None:
    with pytest.raises(ValueError):
        validate_shop_url(template.format(control=control))


def test_rejects_monitor_values_that_would_collide_as_state_keys() -> None:
    first = {"id": "a\x1fb", "category": "c"}
    second = {"id": "a", "category": "b\x1fc"}
    assert state_key(first["id"], first["category"]) == state_key(second["id"], second["category"])

    with pytest.raises(ValueError, match="control"):
        AppConfig.model_validate(
            {
                "monitors": [
                    {
                        "id": first["id"],
                        "name": "First",
                        "url": "https://pay.ldxp.cn/shop/FIRST",
                        "categories": [first["category"]],
                    },
                    {
                        "id": second["id"],
                        "name": "Second",
                        "url": "https://pay.ldxp.cn/shop/SECOND",
                        "categories": [second["category"]],
                    },
                ]
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("id", "id\x00value"), ("name", "name\x85value"), ("categories", ["cat\x7fvalue"])],
)
def test_rejects_control_characters_in_monitor_strings(field: str, value: object) -> None:
    monitor: dict[str, object] = {
        "id": "monitor",
        "name": "Monitor",
        "url": "https://pay.ldxp.cn/shop/MONITOR",
        "categories": ["Category"],
    }
    monitor[field] = value

    with pytest.raises(ValueError, match="control"):
        AppConfig.model_validate({"monitors": [monitor]})


@pytest.mark.parametrize(
    "value",
    [
        "\x00https://pay.ldxp.cn/shop/NIFGEAC5",
        "\x1fhttps://pay.ldxp.cn/shop/NIFGEAC5",
        "\x7fhttps://pay.ldxp.cn/shop/NIFGEAC5",
        "\x85https://pay.ldxp.cn/shop/NIFGEAC5",
        "https://pay.ldxp.cn\x01/shop/NIFGEAC5",
        "https://pay.ldxp.cn/shop/NIF\x7fGEAC5",
        "https://pay.ldxp.cn/shop/NIF\x85GEAC5",
    ],
)
def test_rejects_all_raw_unicode_control_characters_before_url_parsing(value: str) -> None:
    with pytest.raises(ValueError, match="control characters") as exc_info:
        validate_shop_url(value)

    assert value not in str(exc_info.value)


def exception_surface(exc: BaseException) -> str:
    surface = [str(exc), repr(exc), repr(exc.args), repr(vars(exc))]
    errors = getattr(exc, "errors", None)
    if callable(errors):
        surface.append(repr(errors()))
    surface.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    current_traceback = exc.__traceback__
    while current_traceback is not None:
        if current_traceback.tb_frame.f_code.co_name == "load_config":
            surface.append(repr(current_traceback.tb_frame.f_locals))
        current_traceback = current_traceback.tb_next
    return "\n".join(surface)


@pytest.mark.parametrize(
    ("content", "sentinel", "expected_clues"),
    [
        (
            VALID_CONFIG.replace(
                "https://pay.ldxp.cn", "https://user:sentinel-url-password@pay.ldxp.cn"
            ),
            "sentinel-url-password",
            ("monitors.0.url", "value_error"),
        ),
        (
            VALID_CONFIG + "webhook: sentinel-webhook-value\n",
            "sentinel-webhook-value",
            ("webhook", "extra_forbidden"),
        ),
        (
            "webhook: [sentinel-broken-yaml\n",
            "sentinel-broken-yaml",
            ("yaml", "parse_error"),
        ),
    ],
    ids=["url-password", "unknown-webhook", "broken-yaml"],
)
def test_load_config_exposes_only_safe_error_details(
    tmp_path: Path, content: str, sentinel: str, expected_clues: tuple[str, str]
) -> None:
    with pytest.raises(ValueError) as exc_info:
        load_config(write_config(tmp_path, content))

    error = exc_info.value
    assert type(error) is getattr(config_module, "ConfigError", None)
    assert error.__cause__ is None
    assert error.__context__ is None
    surface = exception_surface(error)
    assert sentinel not in surface
    assert all(clue in str(error) for clue in expected_clues)


def test_rejects_empty_monitor_list(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, "monitors: []\n"))
