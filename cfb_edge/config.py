"""Configuration loading: TOML config file plus .env for the API key."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATHS = ("config.toml", "cfb-edge.toml")

# Logical book name -> Odds API bookmaker keys to accept, in preference order.
# The API has renamed books before (Caesars was williamhill_us), so each
# logical book carries its known aliases and we use whichever one shows up.
DEFAULT_ALIASES: dict[str, list[str]] = {
    "draftkings": ["draftkings"],
    "pinnacle": ["pinnacle"],
    "circa": ["circasports", "circa"],
    "fanduel": ["fanduel"],
    "betmgm": ["betmgm"],
    "caesars": ["williamhill_us", "caesars"],
}


class ConfigError(RuntimeError):
    pass


@dataclass
class Config:
    bankroll: float = 1000.0
    kelly_fraction: float = 0.25
    max_bet_pct: float | None = 2.0
    min_edge: float = 1.0  # percentage points
    markets: tuple[str, ...] = ("h2h", "spreads", "totals")
    regions: str = "us"
    sport: str = "americanfootball_ncaaf"
    target_book: str = "draftkings"
    sharp_priority: tuple[str, ...] = ("pinnacle", "circa")
    consensus_books: tuple[str, ...] = ("fanduel", "betmgm", "caesars")
    min_consensus_books: int = 2
    cache_dir: Path = Path("data/cache")
    out_dir: Path = Path("data/runs")
    db_path: Path = Path("data/cfb_edge.sqlite")
    cache_max_age_minutes: float = 15.0
    aliases: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_ALIASES))
    api_key: str | None = None
    source_path: Path | None = None

    @property
    def all_books(self) -> tuple[str, ...]:
        """Every logical book we want prices for, target first."""
        ordered = [self.target_book, *self.sharp_priority, *self.consensus_books]
        seen: list[str] = []
        for book in ordered:
            if book not in seen:
                seen.append(book)
        return tuple(seen)

    def api_book_keys(self) -> list[str]:
        """Flattened alias list to send as the API's `bookmakers` filter."""
        keys: list[str] = []
        for book in self.all_books:
            for alias in self.aliases.get(book, [book]):
                if alias not in keys:
                    keys.append(alias)
        return keys


def load_dotenv(path: str | Path = ".env", override: bool = False) -> dict[str, str]:
    """Minimal .env reader (KEY=VALUE, # comments, optional quotes)."""
    env_path = Path(path)
    loaded: dict[str, str] = {}
    if not env_path.is_file():
        return loaded
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


def _as_path(value: Any, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else base / path


def load_config(path: str | Path | None = None, env_path: str | Path = ".env") -> Config:
    """Read config.toml (if present) and the API key from the environment."""
    load_dotenv(env_path)

    config_path: Path | None = None
    if path is not None:
        config_path = Path(path)
        if not config_path.is_file():
            raise ConfigError(f"config file not found: {config_path}")
    else:
        for candidate in DEFAULT_CONFIG_PATHS:
            if Path(candidate).is_file():
                config_path = Path(candidate)
                break

    data: dict[str, Any] = {}
    if config_path is not None:
        try:
            with open(config_path, "rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"could not read {config_path}: {exc}") from exc

    cfg = Config(source_path=config_path)
    base = config_path.parent if config_path is not None else Path(".")

    bankroll_section = data.get("bankroll")
    if isinstance(bankroll_section, dict):
        # Allow either `bankroll = 2000` or `[bankroll] amount = 2000`.
        cfg.bankroll = float(bankroll_section.get("amount", cfg.bankroll))
        cfg.kelly_fraction = float(bankroll_section.get("kelly_fraction", cfg.kelly_fraction))
        if "max_bet_pct" in bankroll_section:
            value = bankroll_section["max_bet_pct"]
            cfg.max_bet_pct = None if value is None else float(value)
    elif bankroll_section is not None:
        cfg.bankroll = float(bankroll_section)

    if "kelly_fraction" in data:
        cfg.kelly_fraction = float(data["kelly_fraction"])
    if "max_bet_pct" in data:
        value = data["max_bet_pct"]
        cfg.max_bet_pct = None if value is None else float(value)
    if "min_edge" in data:
        cfg.min_edge = float(data["min_edge"])
    if "markets" in data:
        cfg.markets = tuple(str(m) for m in data["markets"])
    if "regions" in data:
        cfg.regions = str(data["regions"])
    if "sport" in data:
        cfg.sport = str(data["sport"])

    books = data.get("books") or {}
    if "target" in books:
        cfg.target_book = str(books["target"])
    if "sharp_priority" in books:
        cfg.sharp_priority = tuple(str(b) for b in books["sharp_priority"])
    if "consensus" in books:
        cfg.consensus_books = tuple(str(b) for b in books["consensus"])
    if "min_consensus_books" in books:
        cfg.min_consensus_books = int(books["min_consensus_books"])
    aliases = books.get("aliases") or {}
    for book, values in aliases.items():
        cfg.aliases[str(book)] = [str(v) for v in values]

    paths = data.get("paths") or {}
    if "cache_dir" in paths:
        cfg.cache_dir = _as_path(paths["cache_dir"], base)
    if "out_dir" in paths:
        cfg.out_dir = _as_path(paths["out_dir"], base)
    if "db_path" in paths:
        cfg.db_path = _as_path(paths["db_path"], base)

    cache = data.get("cache") or {}
    if "max_age_minutes" in cache:
        cfg.cache_max_age_minutes = float(cache["max_age_minutes"])

    cfg.api_key = os.environ.get("ODDS_API_KEY") or None

    if cfg.kelly_fraction <= 0:
        raise ConfigError("kelly_fraction must be > 0")
    if cfg.bankroll < 0:
        raise ConfigError("bankroll must be >= 0")
    return cfg
