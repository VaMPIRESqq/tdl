"""Local configuration and credential persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TypeVar

from .models import Settings, Token

T = TypeVar("T")


def config_dir() -> Path:
    return Path.home() / ".tdl"


def settings_path() -> Path:
    return config_dir() / "settings.json"


def token_path() -> Path:
    return config_dir() / "token.json"


def load_settings() -> Settings:
    path = settings_path()
    if not path.exists():
        return Settings()
    return Settings.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_settings(settings: Settings) -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    settings_path().write_text(
        json.dumps(settings.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_token() -> Token:
    path = token_path()
    if not path.exists():
        return Token()
    return Token.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_token(token: Token) -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    path = token_path()
    path.write_text(
        json.dumps(token.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def delete_token() -> None:
    try:
        token_path().unlink()
    except FileNotFoundError:
        pass
