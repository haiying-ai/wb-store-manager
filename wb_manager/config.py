from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    token: str
    verify_tls: bool = True


def load_settings() -> Settings:
    env_values = _load_local_env()
    token = os.getenv("WB_API_TOKEN", "").strip() or env_values.get("WB_API_TOKEN", "").strip()
    token_file = os.getenv("WB_API_TOKEN_FILE", "").strip() or env_values.get("WB_API_TOKEN_FILE", "").strip()
    if not token and token_file:
        token = Path(token_file).expanduser().read_text(encoding="utf-8-sig").strip()
    if not token:
        raise ConfigError("WB_API_TOKEN is not set.")

    verify_raw = (os.getenv("WB_VERIFY_TLS", "").strip() or env_values.get("WB_VERIFY_TLS", "true").strip()).lower()
    verify_tls = verify_raw not in {"0", "false", "no", "off"}
    return Settings(token=token, verify_tls=verify_tls)


def _load_local_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for name in (".env.local", ".env"):
        path = Path.cwd() / name
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values
