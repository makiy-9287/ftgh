"""Configuration loading: YAML strategy parameters + environment secrets.

Secrets never live in YAML; strategy parameters never live in code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True)
class Secrets:
    """Credentials sourced exclusively from the environment."""

    binance_key: str = ""
    binance_secret: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""
    telegram_admin_chat_id: str = ""

    @property
    def has_exchange_keys(self) -> bool:
        return bool(self.binance_key and self.binance_secret)

    def masked(self) -> dict[str, str]:
        """Safe-to-log representation. Never log the dataclass directly."""

        def mask(value: str) -> str:
            if not value:
                return "<unset>"
            return f"{value[:4]}…{value[-4:]}" if len(value) > 12 else "<set>"

        return {
            "binance_key": mask(self.binance_key),
            "binance_secret": mask(self.binance_secret),
            "telegram_token": mask(self.telegram_token),
            "telegram_chat_id": self.telegram_chat_id or "<unset>",
        }


@dataclass(frozen=True)
class Settings:
    """Fully resolved runtime settings."""

    env: str
    log_level: str
    data_dir: Path
    log_dir: Path
    strategy: dict[str, Any] = field(repr=False)
    secrets: Secrets = field(repr=False)

    # -- convenience accessors ------------------------------------------
    def section(self, name: str) -> dict[str, Any]:
        try:
            return self.strategy[name]
        except KeyError as exc:  # pragma: no cover - config typo guard
            raise ConfigError(f"missing config section: {name!r}") from exc

    def mode(self, name: str) -> dict[str, Any]:
        modes = self.section("modes")
        if name not in modes:
            raise ConfigError(f"unknown mode: {name!r}")
        return modes[name]

    @property
    def enabled_modes(self) -> list[str]:
        return [k for k, v in self.section("modes").items() if v.get("enabled")]


def _resolve_config_path() -> Path:
    raw = os.getenv("OFS_CONFIG_PATH")
    path = Path(raw) if raw else _REPO_ROOT / "config.yaml"
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    return path


def _resolve_dir(env_key: str, default: Path) -> Path:
    path = Path(os.getenv(env_key) or default)
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_settings(dotenv_path: str | os.PathLike[str] | None = None) -> Settings:
    """Load .env (if present), then config.yaml, into a Settings object."""
    load_dotenv(dotenv_path or os.getenv("OFS_ENV_FILE") or _REPO_ROOT / ".env",
                override=False)

    config_path = _resolve_config_path()
    with config_path.open("r", encoding="utf-8") as fh:
        strategy = yaml.safe_load(fh) or {}

    for required in ("exchange", "universe", "modes", "risk", "telegram"):
        if required not in strategy:
            raise ConfigError(f"config.yaml is missing the {required!r} section")

    return Settings(
        env=os.getenv("OFS_ENV", "development"),
        log_level=os.getenv("OFS_LOG_LEVEL", "INFO").upper(),
        data_dir=_resolve_dir("OFS_DATA_DIR", _REPO_ROOT / "data"),
        log_dir=_resolve_dir("OFS_LOG_DIR", _REPO_ROOT / "logs"),
        strategy=strategy,
        secrets=Secrets(
            binance_key=os.getenv("BINANCE_API_KEY", "").strip(),
            binance_secret=os.getenv("BINANCE_API_SECRET", "").strip(),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            telegram_admin_chat_id=os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip(),
        ),
    )
