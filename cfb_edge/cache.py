"""On-disk cache of raw Odds API responses.

Every fetch is written to its own timestamped file so reruns can replay a
snapshot instead of burning API quota, and so old pulls stay around as a record
of what the board looked like.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FILE_TIME_FORMAT = "%Y%m%dT%H%M%SZ"


@dataclass
class CachedResponse:
    path: Path
    fetched_at: datetime
    params: dict[str, Any]
    data: list[dict[str, Any]]
    quota: dict[str, str]

    @property
    def age_minutes(self) -> float:
        delta = datetime.now(timezone.utc) - self.fetched_at
        return delta.total_seconds() / 60.0


def params_fingerprint(params: dict[str, Any]) -> str:
    """Stable short hash so different market/book requests cache separately."""
    blob = json.dumps(params, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:10]


def cache_filename(sport: str, params: dict[str, Any], fetched_at: datetime) -> str:
    stamp = fetched_at.astimezone(timezone.utc).strftime(FILE_TIME_FORMAT)
    return f"{sport}_{params_fingerprint(params)}_{stamp}.json"


def save_response(
    cache_dir: Path,
    sport: str,
    params: dict[str, Any],
    data: list[dict[str, Any]],
    quota: dict[str, str] | None = None,
    fetched_at: datetime | None = None,
) -> CachedResponse:
    fetched_at = fetched_at or datetime.now(timezone.utc)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / cache_filename(sport, params, fetched_at)
    envelope = {
        "fetched_at": fetched_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sport": sport,
        "params": params,
        "quota": quota or {},
        "data": data,
    }
    path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    return CachedResponse(
        path=path, fetched_at=fetched_at, params=params, data=data, quota=quota or {}
    )


def read_response(path: Path) -> CachedResponse:
    envelope = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(envelope, list):
        # A bare API response saved by hand: no metadata, treat it as ancient.
        return CachedResponse(
            path=Path(path),
            fetched_at=datetime.fromtimestamp(Path(path).stat().st_mtime, tz=timezone.utc),
            params={},
            data=envelope,
            quota={},
        )
    fetched_at_raw = str(envelope.get("fetched_at", "")).replace("Z", "+00:00")
    try:
        fetched_at = datetime.fromisoformat(fetched_at_raw)
    except ValueError:
        fetched_at = datetime.fromtimestamp(Path(path).stat().st_mtime, tz=timezone.utc)
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    return CachedResponse(
        path=Path(path),
        fetched_at=fetched_at.astimezone(timezone.utc),
        params=envelope.get("params") or {},
        data=envelope.get("data") or [],
        quota=envelope.get("quota") or {},
    )


def latest_response(
    cache_dir: Path, sport: str, params: dict[str, Any]
) -> CachedResponse | None:
    """Newest cached pull for this exact request, or None."""
    if not cache_dir.is_dir():
        return None
    pattern = f"{sport}_{params_fingerprint(params)}_*.json"
    matches = sorted(cache_dir.glob(pattern))
    for path in reversed(matches):
        try:
            return read_response(path)
        except (OSError, json.JSONDecodeError):
            continue
    return None
