"""Load and validate monitor configuration."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Annotated, Any, Self
from urllib.parse import urlsplit

import yaml  # type: ignore[import-untyped]
from pydantic import (
    AfterValidator,
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)


def _contains_control_character(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _reject_control_characters(value: str) -> str:
    if _contains_control_character(value):
        raise ValueError("value must not contain control characters")
    return value


NonEmptyStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
    AfterValidator(_reject_control_characters),
]
SHOP_PATH = re.compile(r"^/shop/(?P<shop_id>[A-Za-z0-9_-]+)$")
URL_ADAPTER = TypeAdapter(AnyHttpUrl)


class ConfigError(ValueError):
    """A configuration error safe to expose without leaking input values."""


def _shop_id_from_url(value: str) -> str:
    if _contains_control_character(value):
        raise ValueError("shop URL must not contain control characters")

    parse_failed = False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        parse_failed = True
    if parse_failed:
        raise ValueError("invalid shop URL")

    if value != value.strip():
        raise ValueError("shop URL must not contain surrounding whitespace")
    if parsed.scheme != "https":
        raise ValueError("shop URL must use HTTPS")
    if parsed.hostname != "pay.ldxp.cn":
        raise ValueError("shop URL host must be pay.ldxp.cn")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("shop URL must not contain user credentials")
    if port not in (None, 443):
        raise ValueError("shop URL must use the default HTTPS port")
    if "?" in value:
        raise ValueError("shop URL must not contain a query")
    if "#" in value:
        raise ValueError("shop URL must not contain a fragment")

    match = SHOP_PATH.fullmatch(parsed.path)
    if match is None:
        raise ValueError("shop URL path must be /shop/<shop_id>")
    return match.group("shop_id")


def validate_shop_url(value: str) -> AnyHttpUrl:
    """Validate and return an approved ldxp shop URL."""
    _shop_id_from_url(value)
    return URL_ADAPTER.validate_python(value)


def validate_final_url(expected_shop_id: str, value: str) -> None:
    """Reject a redirect target that changes the approved shop identity."""
    actual_shop_id = _shop_id_from_url(str(validate_shop_url(value)))
    if actual_shop_id != expected_shop_id:
        raise ValueError("final URL shop id does not match configured shop id")


class MonitorConfig(BaseModel):
    """One shop and the product categories monitored within it."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: NonEmptyStr
    name: NonEmptyStr
    url: AnyHttpUrl
    categories: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @field_validator("url", mode="before")
    @classmethod
    def validate_url(cls, value: Any) -> AnyHttpUrl:
        if not isinstance(value, str):
            raise ValueError("shop URL must be a string")
        return validate_shop_url(value)

    @property
    def shop_id(self) -> str:
        """Return the validated shop identifier from the URL path."""
        return _shop_id_from_url(str(self.url))


class AppConfig(BaseModel):
    """Application monitor configuration."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    monitors: tuple[MonitorConfig, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def reject_duplicate_monitor_ids(self) -> Self:
        monitor_ids = [monitor.id for monitor in self.monitors]
        if len(monitor_ids) != len(set(monitor_ids)):
            raise ValueError("duplicate monitor id")
        return self


def load_config(path: Path) -> AppConfig:
    """Read a UTF-8 YAML configuration file and validate its structure."""
    yaml_error = False
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        yaml_error = True
    if yaml_error:
        raise ConfigError("yaml: parse_error")

    if not isinstance(document, dict):
        document = None
        raise ConfigError("document: mapping_type (configuration document must be a mapping)")

    config = None
    error_summary = None
    try:
        config = AppConfig.model_validate(document)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        error_summary = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['type']}"
            for error in errors
        )
    if error_summary is not None:
        document = None
        raise ConfigError(error_summary)

    assert config is not None
    return config
