"""Half-point values for college football spreads and totals.

The value of a half point is not a constant: moving a spread off 3 costs far
more than moving it off 8, because final margins pile up on 3, 7, 10 and 14.
That structure only exists relative to where the market set the line, so the
table is built by conditioning on the closing line:

    for each reference line, the empirical distribution of the final
    margin (or the final total) across historical games whose closing
    number was near it

With that distribution in hand, a fair probability quoted at the sharp book's
number can be moved to DraftKings' number: compute the model's cover
probability at both numbers and shift the sharp book's fair probability by the
difference. The shift cancels any bias in the model's location, so what the
table supplies is the *shape* around the number — which is exactly the part
the sharp price cannot tell us.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from cfb_edge.config import Config
from cfb_edge.models import SPREAD_LIKE, TOTAL_LIKE
from cfb_edge.scores import HistoricalGame

TABLE_VERSION = 1
SPREAD = "spreads"
TOTAL = "totals"

# Roles a side can play, which decide the direction of the inequality.
FAVORITE = "favorite"
UNDERDOG = "underdog"
OVER = "over"
UNDER = "under"


class HalfPointError(RuntimeError):
    pass


@dataclass
class Distribution:
    """The pmf of an integer outcome around one reference number."""

    reference: float
    counts: dict[int, int]
    sample: int

    @property
    def pmf(self) -> dict[int, float]:
        if self.sample <= 0:
            return {}
        return {k: v / self.sample for k, v in self.counts.items()}

    def prob_exactly(self, value: float) -> float:
        if self.sample <= 0 or value != int(value):
            return 0.0
        return self.counts.get(int(value), 0) / self.sample

    def prob_above(self, threshold: float) -> float:
        if self.sample <= 0:
            return 0.0
        hits = sum(count for key, count in self.counts.items() if key > threshold)
        return hits / self.sample

    def prob_below(self, threshold: float) -> float:
        if self.sample <= 0:
            return 0.0
        hits = sum(count for key, count in self.counts.items() if key < threshold)
        return hits / self.sample

    def cover_prob(self, threshold: float, above: bool) -> float:
        """Win probability conditional on not pushing, as a bet settles."""
        push = self.prob_exactly(threshold)
        if push >= 1.0:
            return 0.5
        win = self.prob_above(threshold) if above else self.prob_below(threshold)
        return win / (1.0 - push)


@dataclass
class HalfPointTable:
    spreads: dict[float, Distribution] = field(default_factory=dict)
    totals: dict[float, Distribution] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    # Guard rails. Beyond these the additive shift stops being trustworthy and
    # the caller is told to flag the line instead of pricing it.
    max_move: float = 3.0
    min_sample: int = 200
    prob_floor: float = 0.02
    max_sharp_prob: float = 0.95

    def is_empty(self) -> bool:
        return not self.spreads and not self.totals

    def _bucket(self, kind: str) -> dict[float, Distribution]:
        return self.spreads if kind == SPREAD else self.totals

    def distribution(self, kind: str, reference: float) -> Distribution | None:
        bucket = self._bucket(kind)
        if not bucket:
            return None
        nearest = min(bucket, key=lambda ref: (abs(ref - reference), ref))
        dist = bucket[nearest]
        if dist.sample < self.min_sample:
            return None
        return dist

    def translate(
        self,
        kind: str,
        role: str,
        sharp_prob: float,
        sharp_point: float,
        dk_point: float,
        reference: float | None = None,
    ) -> float | None:
        """Move a fair probability from the sharp number to DraftKings'.

        Returns None when the move is too big, the sample too thin, or the
        sharp price too lopsided for the shift to mean anything.
        """
        if abs(dk_point - sharp_point) > self.max_move:
            return None
        if not (1 - self.max_sharp_prob) <= sharp_prob <= self.max_sharp_prob:
            return None
        ref = reference if reference is not None else abs(sharp_point)
        dist = self.distribution(kind, ref)
        if dist is None:
            return None

        above = role in (FAVORITE, OVER)
        sharp_threshold = _threshold(kind, role, sharp_point)
        dk_threshold = _threshold(kind, role, dk_point)
        shift = dist.cover_prob(dk_threshold, above) - dist.cover_prob(sharp_threshold, above)
        moved = sharp_prob + shift
        if not self.prob_floor <= moved <= 1.0 - self.prob_floor:
            return None
        return moved

    def half_point_values(self, kind: str, max_number: float = 21.0) -> list[dict[str, Any]]:
        """What each half point is worth, for the `halfpoint show` table."""
        rows: list[dict[str, Any]] = []
        bucket = self._bucket(kind)
        for reference in sorted(bucket):
            if reference > max_number:
                continue
            dist = bucket[reference]
            if dist.sample < self.min_sample:
                continue
            if kind == SPREAD:
                # Favorite moving from laying n to laying n - 0.5.
                worse, better = _zero(-reference), _zero(0.5 - reference)
                role = FAVORITE
            else:
                # Over moving from n down to n - 0.5.
                worse, better = reference, reference - 0.5
                role = OVER
            above = True
            gain = dist.cover_prob(_threshold(kind, role, better), above) - dist.cover_prob(
                _threshold(kind, role, worse), above
            )
            rows.append({
                "reference": reference,
                "prob_gain": gain,
                "landed_exactly": dist.prob_exactly(reference),
                "sample": dist.sample,
            })
        return rows

    def to_dict(self) -> dict[str, Any]:
        def dump(bucket: dict[float, Distribution]) -> dict[str, Any]:
            return {
                str(ref): {
                    "sample": dist.sample,
                    "counts": {str(k): v for k, v in sorted(dist.counts.items())},
                }
                for ref, dist in sorted(bucket.items())
            }

        return {
            "version": TABLE_VERSION,
            "meta": self.meta,
            "guards": {
                "max_move": self.max_move,
                "min_sample": self.min_sample,
                "prob_floor": self.prob_floor,
                "max_sharp_prob": self.max_sharp_prob,
            },
            "spreads": dump(self.spreads),
            "totals": dump(self.totals),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HalfPointTable":
        def load(raw: dict[str, Any]) -> dict[float, Distribution]:
            bucket: dict[float, Distribution] = {}
            for ref, entry in (raw or {}).items():
                counts = {int(k): int(v) for k, v in (entry.get("counts") or {}).items()}
                bucket[float(ref)] = Distribution(
                    reference=float(ref),
                    counts=counts,
                    sample=int(entry.get("sample") or sum(counts.values())),
                )
            return bucket

        version = data.get("version")
        if version is not None and int(version) != TABLE_VERSION:
            raise HalfPointError(
                f"half-point table version {version} is not supported "
                f"(expected {TABLE_VERSION}); rebuild it with `cfb-edge halfpoint build`"
            )
        guards = data.get("guards") or {}
        return cls(
            spreads=load(data.get("spreads")),
            totals=load(data.get("totals")),
            meta=data.get("meta") or {},
            max_move=float(guards.get("max_move", 3.0)),
            min_sample=int(guards.get("min_sample", 200)),
            prob_floor=float(guards.get("prob_floor", 0.02)),
            max_sharp_prob=float(guards.get("max_sharp_prob", 0.95)),
        )


def _zero(value: float) -> float:
    """Collapse -0.0, which formats as '-0'."""
    return 0.0 if value == 0 else value


def _signed(value: float) -> str:
    return f"{_zero(value):+g}"


def _threshold(kind: str, role: str, point: float) -> float:
    """The number the outcome variable must beat for this side to cover.

    Spreads use the favorite's margin, so the underdog's inequality flips.
    Totals use the game's points.
    """
    if kind == SPREAD:
        return -point if role == FAVORITE else point
    return point


def line_diff(
    role: str, sharp_point: float | None, dk_point: float | None
) -> float | None:
    """Points in the bettor's favour: how much better DK's number is.

    Positive means DK's number helps this side, negative means it hurts. Both
    sides of a spread want a bigger number; an Over wants a lower total and an
    Under a higher one, which is the only case that flips.
    """
    if sharp_point is None or dk_point is None:
        return None
    if role == OVER:
        return float(sharp_point) - float(dk_point)
    return float(dk_point) - float(sharp_point)


def market_kind(market: str) -> str | None:
    if market in SPREAD_LIKE:
        return SPREAD
    if market in TOTAL_LIKE:
        return TOTAL
    return None


def side_role(kind: str, side: str, point: float | None) -> str | None:
    """Which side of the number this outcome is on."""
    if kind == TOTAL:
        lowered = side.lower()
        if lowered.endswith("over"):
            return OVER
        if lowered.endswith("under"):
            return UNDER
        return None
    if point is None:
        return None
    return FAVORITE if point <= 0 else UNDERDOG


def build_table(
    games: Sequence[HistoricalGame],
    spread_refs: Iterable[float] | None = None,
    total_refs: Iterable[float] | None = None,
    window: float = 1.5,
    min_sample: int = 200,
) -> HalfPointTable:
    """Bin historical results by their closing number."""
    spread_games = [
        (g.favorite_line, g.favorite_margin)
        for g in games
        if g.favorite_line is not None and g.favorite_margin is not None
    ]
    total_games = [
        (g.closing_total, g.total_points) for g in games if g.closing_total is not None
    ]

    spread_refs = list(spread_refs if spread_refs is not None else _frange(0.0, 28.0, 1.0))
    if total_games:
        lows = min(t for t, _ in total_games)
        highs = max(t for t, _ in total_games)
        default_totals = _frange(math.floor(lows), math.ceil(highs), 1.0)
    else:
        default_totals = []
    total_refs = list(total_refs if total_refs is not None else default_totals)

    spreads: dict[float, Distribution] = {}
    for ref in spread_refs:
        counts: dict[int, int] = {}
        sample = 0
        for line, margin in spread_games:
            if abs(line - ref) <= window:
                counts[margin] = counts.get(margin, 0) + 1
                sample += 1
        if sample:
            spreads[float(ref)] = Distribution(float(ref), counts, sample)

    totals: dict[float, Distribution] = {}
    for ref in total_refs:
        counts = {}
        sample = 0
        for line, points in total_games:
            if abs(line - ref) <= window:
                counts[points] = counts.get(points, 0) + 1
                sample += 1
        if sample:
            totals[float(ref)] = Distribution(float(ref), counts, sample)

    seasons = sorted({g.season for g in games})
    meta = {
        "built_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "games": len(games),
        "games_with_spread": len(spread_games),
        "games_with_total": len(total_games),
        "seasons": [seasons[0], seasons[-1]] if seasons else [],
        "window": window,
    }
    return HalfPointTable(spreads=spreads, totals=totals, meta=meta, min_sample=min_sample)


def _frange(start: float, stop: float, step: float) -> list[float]:
    values: list[float] = []
    current = float(start)
    while current <= stop + 1e-9:
        values.append(round(current, 2))
        current += step
    return values


def save_table(table: HalfPointTable, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(table.to_dict(), indent=1), encoding="utf-8")
    return path


def load_table(path: Path | str) -> HalfPointTable | None:
    """Load the table, or None when it has not been built yet."""
    table_path = Path(path)
    if not table_path.is_file():
        return None
    try:
        data = json.loads(table_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HalfPointError(f"could not read {table_path}: {exc}") from exc
    return HalfPointTable.from_dict(data)


def cmd_halfpoint_build(cfg: Config, args: argparse.Namespace) -> int:
    from cfb_edge.scores import connect, load_games

    conn = connect(cfg.scores_db_path)
    try:
        games = load_games(conn, min_season=args.min_season)
    finally:
        conn.close()

    if not games:
        print(
            f"no historical results in {cfg.scores_db_path}.\n"
            "run: cfb-edge scores fetch --seasons 2015-2024",
            file=sys.stderr,
        )
        return 1

    table = build_table(games, window=cfg.halfpoint_window, min_sample=cfg.halfpoint_min_sample)
    out = Path(args.out) if args.out else cfg.halfpoint_table_path
    save_table(table, out)
    usable_spreads = sum(1 for d in table.spreads.values() if d.sample >= table.min_sample)
    usable_totals = sum(1 for d in table.totals.values() if d.sample >= table.min_sample)
    print(f"built from {len(games)} games ({table.meta['games_with_spread']} with a closing spread, "
          f"{table.meta['games_with_total']} with a closing total)")
    print(f"reference lines with enough sample: {usable_spreads} spreads, {usable_totals} totals")
    print(f"wrote {out}")
    return 0


def cmd_halfpoint_show(cfg: Config, args: argparse.Namespace) -> int:
    from cfb_edge.report import Column, render_table

    path = Path(args.table) if args.table else cfg.halfpoint_table_path
    table = load_table(path)
    if table is None:
        print(f"no half-point table at {path}. run: cfb-edge halfpoint build", file=sys.stderr)
        return 1

    kind = SPREAD if args.market == "spreads" else TOTAL
    rows = table.half_point_values(kind, max_number=args.max_number)
    if not rows:
        print(f"no {args.market} reference lines with at least {table.min_sample} games")
        return 1

    meta = table.meta
    seasons = meta.get("seasons") or []
    span = f"{seasons[0]}-{seasons[1]}" if len(seasons) == 2 else "?"
    print()
    print(f"half-point values for {args.market} — {meta.get('games', '?')} games, {span}")
    print(f"window ±{meta.get('window', '?')} pts, minimum sample {table.min_sample}")
    print()

    if kind == SPREAD:
        headers = [
            Column("LINE", "right"), Column("MOVE"), Column("WIN PROB GAIN", "right"),
            Column("MARGIN LANDS HERE", "right"), Column("SAMPLE", "right"),
        ]
        body = [
            [
                f"{r['reference']:g}",
                f"{_signed(-r['reference'])} → {_signed(0.5 - r['reference'])}",
                f"{r['prob_gain'] * 100:+.2f}%",
                f"{r['landed_exactly'] * 100:.2f}%",
                f"{r['sample']:,}",
            ]
            for r in rows
        ]
    else:
        headers = [
            Column("TOTAL", "right"), Column("MOVE"), Column("OVER PROB GAIN", "right"),
            Column("TOTAL LANDS HERE", "right"), Column("SAMPLE", "right"),
        ]
        body = [
            [
                f"{r['reference']:g}",
                f"{r['reference']:g} → {r['reference'] - 0.5:g}",
                f"{r['prob_gain'] * 100:+.2f}%",
                f"{r['landed_exactly'] * 100:.2f}%",
                f"{r['sample']:,}",
            ]
            for r in rows
        ]
    print(render_table(headers, body))
    print()
    return 0
