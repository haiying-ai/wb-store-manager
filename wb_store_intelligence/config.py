from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


ENV_NAME = re.compile(r"^(?:WB_API_TOKEN|WB_INTEL_[A-Z0-9_]+_TOKEN)$")
ALLOWED_ROLES = {"owner_readonly", "benchmark_readonly"}
TOKEN_PLACEHOLDER_PREFIX = "REPLACE_WITH_"


def load_token_file(path: str | Path, *, override: bool = False) -> tuple[str, ...]:
    """Load a strict local KEY=VALUE file without printing or returning values."""
    source = Path(path)
    loaded: list[str] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(
        source.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Invalid token file line {line_number}: expected KEY=VALUE")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not ENV_NAME.fullmatch(name):
            raise ValueError(f"Invalid token variable on line {line_number}: {name!r}")
        if name in seen:
            raise ValueError(f"Duplicate token variable on line {line_number}: {name}")
        if not value or value.startswith(TOKEN_PLACEHOLDER_PREFIX):
            raise RuntimeError(f"Token has not been configured: {name}")
        seen.add(name)
        if override or name not in os.environ:
            os.environ[name] = value
        loaded.append(name)
    if not loaded:
        raise RuntimeError("Token file contains no configured WB_INTEL_*_TOKEN entries")
    return tuple(loaded)


@dataclass(frozen=True)
class StoreConfig:
    alias: str
    role: str
    token_env: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,31}", self.alias):
            raise ValueError(f"Invalid store alias: {self.alias!r}")
        if self.role not in ALLOWED_ROLES:
            raise ValueError(f"Invalid read-only store role: {self.role!r}")
        if not ENV_NAME.fullmatch(self.token_env):
            raise ValueError("token_env must be WB_API_TOKEN or WB_INTEL_*_TOKEN")

    def load_token(self) -> str:
        token = os.environ.get(self.token_env, "").strip()
        if not token:
            raise RuntimeError(f"Missing token environment variable: {self.token_env}")
        if token.startswith(TOKEN_PLACEHOLDER_PREFIX):
            raise RuntimeError(f"Token has not been configured: {self.token_env}")
        return token


@dataclass(frozen=True)
class StoreRegistry:
    stores: tuple[StoreConfig, ...]

    @classmethod
    def from_file(cls, path: str | Path) -> "StoreRegistry":
        source = Path(path)
        raw = json.loads(source.read_text(encoding="utf-8-sig"))
        if set(raw) != {"stores"} or not isinstance(raw["stores"], list):
            raise ValueError("Config must contain only a 'stores' list")

        stores: list[StoreConfig] = []
        aliases: set[str] = set()
        token_envs: set[str] = set()
        for item in raw["stores"]:
            if not isinstance(item, dict) or set(item) != {"alias", "role", "token_env"}:
                raise ValueError("Each store requires alias, role, and token_env only")
            store = StoreConfig(**item)
            if store.alias in aliases:
                raise ValueError(f"Duplicate store alias: {store.alias}")
            if store.token_env in token_envs:
                raise ValueError(f"Token environment variable reused: {store.token_env}")
            aliases.add(store.alias)
            token_envs.add(store.token_env)
            stores.append(store)

        if not stores:
            raise ValueError("At least one store is required")
        return cls(tuple(stores))

    def get(self, alias: str) -> StoreConfig:
        for store in self.stores:
            if store.alias == alias:
                return store
        raise KeyError(f"Unknown store alias: {alias}")
