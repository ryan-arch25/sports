# cfb-edge

A command line tool that finds +EV DraftKings bets in college football by pricing
every DK line against a sharp book's de-vigged number.

It pulls NCAAF odds from [The Odds API](https://the-odds-api.com/), treats
Pinnacle as the fair-price reference (Circa, then a consensus of the other books,
as fallbacks), and ranks the DraftKings prices that beat that fair price.

```
GAME                              KICKOFF (ET)        MARKET     PICK                        DK                SHARP  FAIR%    DK%  EDGE  EV/$100   STAKE
--------------------------------  ------------------  ---------  ------------------------  ----  -------------------  -----  -----  ----  -------  ------
Ohio State Buckeyes @ Michigan …  Sun 09/20 12:00 PM  Spread     Michigan Wolverines +6.5  +110         -110 (circa)  50.0%  47.6%  2.4%    +5.00  $22.73
Texas Longhorns @ Oklahoma Soon…  Sun 09/20 8:00 PM   Total      Over 44.5                 +110  -109 (consensus(2))  49.8%  47.6%  2.2%    +4.55  $20.66
Georgia Bulldogs @ Alabama Crim…  Sun 09/20 7:30 PM   Moneyline  Georgia Bulldogs ML       +145      +130 (pinnacle)  42.4%  40.8%  1.5%    +3.76  $12.96
Georgia Bulldogs @ Alabama Crim…  Sun 09/20 7:30 PM   Spread     Georgia Bulldogs +3.5     +105      -105 (pinnacle)  50.0%  48.8%  1.2%    +2.50  $11.90

4 bet(s) at >= 1% edge | total stake $68.26 | expected profit $2.86

Different number — DK is off the sharp line, edge not computed (2):
GAME                              KICKOFF (ET)       MARKET  DK LINE       DK  SHARP LINE            SHARP
--------------------------------  -----------------  ------  ----------  ----  ----------  ---------------
Georgia Bulldogs @ Alabama Crim…  Sun 09/20 7:30 PM  Total   Over 51.5   -110  Over 53     -105 (pinnacle)
Georgia Bulldogs @ Alabama Crim…  Sun 09/20 7:30 PM  Total   Under 51.5  -110  Under 53    -105 (pinnacle)
```

## Setup

Python 3.11+ is required (the config loader uses the stdlib `tomllib`).

```bash
git clone <this repo> && cd sports
python3 -m venv .venv && source .venv/bin/activate
pip install -e .            # or: pip install -r requirements.txt
```

Put your API key in a `.env` file at the repo root:

```bash
cp .env.example .env
# then edit .env:
ODDS_API_KEY=your_key_here
```

Copy the config and set your bankroll:

```bash
cp config.example.toml config.toml
```

```toml
bankroll = 2000.0        # used for stake sizing
kelly_fraction = 0.25    # quarter Kelly
max_bet_pct = 2.0        # hard cap per bet, as a % of bankroll
min_edge = 1.0           # default --min-edge, in percentage points
markets = ["h2h", "spreads", "totals"]
```

Both files are gitignored. Without `config.toml` the built-in defaults apply
(a $1,000 bankroll, quarter Kelly, all three markets).

## Usage

```bash
cfb-edge                              # or: python -m cfb_edge
cfb-edge --min-edge 2                 # only show 2%+ edges
cfb-edge --market totals              # one market
cfb-edge --market h2h --market spreads
cfb-edge --market totals,spreads      # same thing, comma form
cfb-edge --bankroll 5000 --kelly 0.5  # override config for one run
cfb-edge --refresh                    # force a fresh API pull
cfb-edge --cache-only                 # score the last pull, spend no quota
cfb-edge --cache-file data/cache/americanfootball_ncaaf_1a2b3c4d5e_20260918T140500Z.json
```

| Flag | Default | What it does |
| --- | --- | --- |
| `--min-edge PCT` | `1.0` | Minimum edge, in percentage points |
| `--market MARKET` | all three | `h2h`, `spreads`, `totals`; repeatable or comma-separated |
| `--bankroll N` | config | Bankroll for stake sizing |
| `--kelly FRACTION` | `0.25` | Kelly fraction |
| `--max-bet-pct PCT` | `2.0` | Cap on a single stake, as a % of bankroll |
| `--refresh` | off | Ignore the cache and call the API |
| `--cache-only` | off | Never call the API; use the newest cached pull |
| `--cache-file PATH` | — | Score one specific snapshot |
| `--max-cache-age MINUTES` | `15` | How fresh a cached pull has to be to be reused |
| `--out-dir PATH` | `data/runs` | Where the CSV and JSON go |
| `--db PATH` | `data/cfb_edge.sqlite` | SQLite log location |
| `--no-db`, `--no-files` | off | Skip the SQLite log / the CSV and JSON |
| `--limit N` | — | Show only the top N bets |
| `--hide-different-number` | off | Omit the number-mismatch section |
| `--no-color` | off | Plain output |
| `--config PATH`, `--env-file PATH` | `config.toml`, `.env` | Alternate locations |

## How a bet is priced

**1. Pull the board.** One request to
`/v4/sports/americanfootball_ncaaf/odds` for `h2h`, `spreads` and `totals` in
American odds, filtered to DraftKings, Pinnacle, Circa, FanDuel, BetMGM and
Caesars. The books filter is what makes this work: Pinnacle sits in the API's
`eu` region and Circa in `us2`, so a plain `regions=us` request would not return
them. Quota cost scales with the regions your book list spans, and the
`x-requests-remaining` header is printed after each live pull.

**2. Cache it.** Every response is written to `data/cache/` in a timestamped
envelope (`<sport>_<request fingerprint>_<UTC timestamp>.json`) that holds the
fetch time, the request parameters, the quota headers and the raw payload — the
API key is never written to disk. Rerunning within `max_age_minutes` replays the
cached pull instead of spending another request. `--refresh` forces a new one.

**3. Find the fair price.** For each game and market, the sharp reference is
Pinnacle; if Pinnacle has not posted that market, Circa; if neither has it, a
consensus of FanDuel, BetMGM and Caesars. Both sides' American prices become
implied probabilities, and each is divided by their sum. That removes the vig
proportionally and leaves fair win probabilities summing to 1:

```
Pinnacle:  Over 53 (-105)   -> 0.5122
           Under 53 (-105)  -> 0.5122     sum 1.0244  (2.44% hold)
fair:      Over  0.5122 / 1.0244 = 0.5000
           Under 0.5122 / 1.0244 = 0.5000
```

For the consensus fallback, each book is de-vigged on its own and the fair
probabilities are averaged, but only across books sitting on the *same* number —
the modal line. A book hanging a different total drops out of the consensus
rather than smearing two different numbers together. Ties between equally
popular numbers go to the group with the lower average hold. DraftKings is never
part of its own benchmark.

**4. Compare DraftKings to it.** For every DK side whose number matches the
sharp number exactly:

- `edge = fair probability − DK implied probability` (DK's raw, vigged price)
- `EV per $100 = p × profit − (1 − p) × 100`
- `fair odds` = the American price that fair probability implies
- `stake` = quarter-Kelly, `bankroll × 0.25 × (p(b + 1) − 1) / b`, capped by `max_bet_pct`

When DK's number differs from the sharp number — DK Under 51.5 against
Pinnacle's 53 — the line is flagged **different number** and listed separately
with no edge computed. Comparing prices across different numbers would require
modeling how much a half point is worth, so the tool reports the mismatch
instead of guessing. Moneylines have no number, so they always compare.

Lines where no sharp or consensus price exists at all are counted in the run
summary and recorded in the output files with status `no_sharp_line`.

## Output

Each run writes two files to `data/runs/`, named with the run id
(`cfb-edge_20260918T140500Z-04f716.csv` / `.json`):

- **CSV** — one row per evaluated DraftKings line, ranked bets first, then the
  number mismatches, then everything else. Includes both teams, kickoff in UTC
  and ET, both prices and probabilities, the sharp source and its hold, edge,
  EV, stake, `status` and `above_min_edge`.
- **JSON** — the same rows plus run metadata: source (`api`/`cache`), snapshot
  path, markets, thresholds, bankroll settings, book configuration, status
  counts and the API quota headers.

## The run log (SQLite)

Every run is appended to `data/cfb_edge.sqlite`:

- `runs` — one row per run: timestamps, source, markets, thresholds, bankroll,
  counts, quota remaining, and the CSV/JSON paths.
- `observations` — one row per evaluated DK line per run, stamped with the
  snapshot's fetch time. This is the line-movement history.
- `closing_lines` (view) — each side's last observation before kickoff.
- `clv` (view) — every observation joined to that side's closing line, with
  `clv_prob_delta = closing implied probability − implied probability at the
  time`. Positive means the market moved toward the bet after it was logged.

Because rows accumulate on every run, the same bet observed several times shows
how the number and price moved:

```sql
-- How one side moved over the week
SELECT observed_at_utc, dk_point, dk_price, sharp_price, edge_pct
FROM observations
WHERE event_id = 'g2michigan' AND market = 'spreads' AND side = 'Michigan Wolverines'
ORDER BY observed_at_utc;

-- CLV on everything the tool actually flagged as a bet
SELECT matchup, market, side, dk_point, dk_price,
       closing_dk_price, ROUND(clv_prob_delta * 100, 2) AS clv_pct
FROM clv
WHERE status = 'priced' AND edge_pct >= 1
ORDER BY clv_pct DESC;
```

Note that `clv` joins on event/market/side, not on the number, so check
`dk_point` against `closing_dk_point` before reading a total's CLV — a closing
line on a different number is not a like-for-like comparison.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

186 tests, no network access required. The odds conversion and de-vig math are
covered against known values (`tests/test_oddsmath.py`), as are sharp-book
selection and consensus grouping (`tests/test_fair.py`), edge and number-mismatch
handling (`tests/test_edges.py`), caching and payload parsing
(`tests/test_cache.py`), and the CLI end to end against a sample board in
`tests/fixtures/sample_odds.json` (`tests/test_cli.py`).

## Limitations

- Edges are only as good as the sharp line. A stale Pinnacle price or a
  consensus built from two soft books on the same bad number will produce edges
  that are not real.
- No half-point conversion, so DK lines off the sharp number are reported but
  not priced.
- De-vigging is proportional (multiplicative). It is the standard approach and
  the right default, but it slightly overrates heavy favorites compared to
  power/Shin methods; on big moneyline dogs treat the fair price as approximate.
- The tool finds and sizes bets. It does not place them, and it does not know
  whether your account is limited.
