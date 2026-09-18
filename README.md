# cfb-edge

A command line tool that finds +EV DraftKings bets in college football by pricing
every DK line against a sharp book's de-vigged number.

It pulls NCAAF odds from [The Odds API](https://the-odds-api.com/), treats
Pinnacle as the fair-price reference (Circa, then a consensus of the other books,
as fallbacks), and ranks the DraftKings prices that beat that fair price. It can
also watch the board on a loop, price lines that sit off the sharp number using a
half-point table built from historical results, scan player props and alternate
lines, log the bets you actually placed, score them against the close, and serve
the whole thing as a web dashboard your friends can open.

```
GAME                              KICKOFF (ET)        MARKET     PICK                        DK                SHARP  FAIR%    DK%  EDGE  EV/$100   STAKE
--------------------------------  ------------------  ---------  ------------------------  ----  -------------------  -----  -----  ----  -------  ------
Georgia Bulldogs @ Alabama Crim…  Sun 09/20 7:30 PM   Total      Over 51.5                 -110  -105 @53 (pinnacle)  56.2%  52.4%  3.8%    +7.29  $40.00
Ohio State Buckeyes @ Michigan …  Sun 09/20 12:00 PM  Spread     Michigan Wolverines +6.5  +110         -110 (circa)  50.0%  47.6%  2.4%    +5.00  $22.73
Texas Longhorns @ Oklahoma Soon…  Sun 09/20 8:00 PM   Total      Over 44.5                 +110  -109 (consensus(2))  49.8%  47.6%  2.2%    +4.55  $20.66
Georgia Bulldogs @ Alabama Crim…  Sun 09/20 7:30 PM   Moneyline  Georgia Bulldogs ML       +145      +130 (pinnacle)  42.4%  40.8%  1.5%    +3.76  $12.96
Georgia Bulldogs @ Alabama Crim…  Sun 09/20 7:30 PM   Spread     Georgia Bulldogs +3.5     +105      -105 (pinnacle)  50.0%  48.8%  1.2%    +2.50  $11.90

5 bet(s) at >= 1% edge | total stake $108.26 | expected profit $5.78
```

The first row is DraftKings on a different number from Pinnacle (51.5 against 53).
`@53` marks a price that came through the half-point table.

## Setup

Python 3.11+ is required (the config loader uses the stdlib `tomllib`).

```bash
git clone <this repo> && cd sports
python3 -m venv .venv && source .venv/bin/activate
pip install -e .            # or: pip install -r requirements.txt
```

Keys go in `.env`, settings in `config.toml`. Both are gitignored.

```bash
cp .env.example .env            # ODDS_API_KEY, and CFBD_API_KEY for the half-point table
cp config.example.toml config.toml
```

```toml
bankroll = 2000.0        # used for stake sizing
kelly_fraction = 0.25    # quarter Kelly
max_bet_pct = 2.0        # hard cap per bet, as a % of bankroll
min_edge = 1.0           # default --min-edge, in percentage points
```

Without `config.toml` the built-in defaults apply (a $1,000 bankroll, quarter
Kelly, all three game markets).

## Commands

| Command | What it does |
| --- | --- |
| `cfb-edge scan` | Score the current board. This is the default: bare flags still work (`cfb-edge --min-edge 2`). |
| `cfb-edge watch` | Rescan on an interval, printing only new or changed lines. |
| `cfb-edge scores fetch` | Download historical results and closing lines from CollegeFootballData. |
| `cfb-edge halfpoint build` / `show` | Build and inspect the half-point value table. |
| `cfb-edge bets add` / `list` | Log a bet you placed, and review what you have logged. |
| `cfb-edge clv` | Compare your prices to the closing line. |
| `cfb-edge serve` | Run the web dashboard (FastAPI). |

```bash
cfb-edge                              # scan, or: python -m cfb_edge
cfb-edge --min-edge 2                 # only show 2%+ edges
cfb-edge --market totals              # one market
cfb-edge --market totals,spreads      # comma form, or repeat --market
cfb-edge --bankroll 5000 --kelly 0.5  # override config for one run
cfb-edge --refresh                    # force a fresh API pull
cfb-edge --cache-only                 # score the last pull, spend no quota
cfb-edge --props --alts               # add player props and alternate lines
cfb-edge watch --interval 10          # rescan every ten minutes
cfb-edge watch --notify               # and announce 2%+ edges to Discord
```

### Scan flags

| Flag | Default | What it does |
| --- | --- | --- |
| `--min-edge PCT` | `1.0` | Minimum edge, in percentage points |
| `--market MARKET` | all three | `h2h`, `spreads`, `totals`; repeatable or comma-separated |
| `--bankroll N` / `--kelly F` / `--max-bet-pct P` | config | Stake sizing overrides |
| `--refresh` / `--cache-only` / `--cache-file PATH` | off | Where the odds come from |
| `--max-cache-age MINUTES` | `15` | How fresh a cached pull has to be to be reused |
| `--props` / `--alts` | off | Add player props / alternate lines (one request per game) |
| `--prop-market M` | config list | Limit which props are requested |
| `--props-window HOURS` | `24` | Only request props for games kicking off this soon |
| `--props-max-events N` | `25` | Hard cap on per-game requests |
| `--yes` | off | Skip the quota confirmation prompt |
| `--notify` / `--notify-dry-run` | off | Send qualifying bets to Discord / show what would be sent |
| `--out-dir PATH` / `--db PATH` | `data/runs`, `data/cfb_edge.sqlite` | Where output goes |
| `--no-db` / `--no-files` | off | Skip the SQLite log / the CSV and JSON |
| `--limit N` | — | Show only the top N bets |
| `--hide-different-number` | off | Omit the number-mismatch section |
| `--no-color` | off | Plain output |
| `--config PATH` / `--env-file PATH` | `config.toml`, `.env` | Alternate locations |

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

Markets are priced one *group* at a time. A game market is a single group; a
player prop market is one group per player, each with its own two sides, so
each player's line is de-vigged on its own.

**4. Compare DraftKings to it.** For every DK side whose number matches the
sharp number exactly:

- `edge = fair probability − DK implied probability` (DK's raw, vigged price)
- `EV per $100 = p × profit − (1 − p) × 100`
- `fair odds` = the American price that fair probability implies
- `stake` = quarter-Kelly, `bankroll × 0.25 × (p(b + 1) − 1) / b`, capped by `max_bet_pct`

When DK's number differs from the sharp number, the half-point table below
prices it if it can, and the line is flagged **different number** if it cannot.
Moneylines have no number, so they always compare.

## Half-point values

DraftKings is often a half point or a point off the sharp number: DK Under 51.5
against Pinnacle's 53. Comparing those prices directly is wrong, and guessing
what the difference is worth is worse. So the tool measures it.

**Nothing ships prebuilt** — the table is built from results you download:

```bash
cfb-edge scores fetch --seasons 2015-2024   # needs CFBD_API_KEY
cfb-edge halfpoint build
cfb-edge halfpoint show --market spreads
```

`scores fetch` pulls final scores *and* closing lines from
[CollegeFootballData](https://collegefootballdata.com) (a free key) into
`data/scores.sqlite`. Closing lines matter as much as the scores: the value of a
half point depends on where the market set the number, which is the whole reason
3 is expensive and 8 is cheap.

`halfpoint build` bins those games by their closing number and records, for each
reference line, the distribution of the final margin (or the final total) across
games whose number was near it:

```
LINE  MOVE       WIN PROB GAIN  MARGIN LANDS HERE  SAMPLE
----  ---------  -------------  -----------------  ------
   3  -3 → -2.5         +3.48%              6.78%   1,771
   4  -4 → -3.5         +2.92%              5.24%   1,660
   5  -5 → -4.5         +1.53%              2.65%   1,548
   6  -6 → -5.5         +0.95%              1.65%   1,451
   7  -7 → -6.5         +3.22%              5.56%   1,348
```

(Numbers from a synthetic test set, not real results — yours will differ.)

With the table in place, a fair probability quoted at the sharp book's number is
moved to DraftKings' number: the model's cover probability is computed at both
numbers and the sharp book's fair probability is shifted by the difference.
Pushes are handled the way a bet settles — laying 3.5 instead of 3 turns every
push on 3 into a loss, which is exactly what the table prices. Those rows show up
in the scan with the sharp's own number in the SHARP column (`-105 @53`) and are
counted separately in the run summary.

**What it will not do.** The translation is refused, and the line goes back to
being flagged, when:

- the move is bigger than 3 points (`max_move`), where an additive shift stops meaning much
- the reference line has fewer than `min_sample` games behind it
- the sharp price is more lopsided than 95/5, where the shift approximation breaks down
- the result would land outside (2%, 98%)
- the market is a moneyline, a player prop or a team total — the table describes
  game margins and game totals, and nothing else

**Assumptions worth knowing.** Games are binned by closing number within a
±1.5 point window, so a line of 3 borrows a little from 1.5 and 4.5; the shift is
additive in probability space, which is the standard half-point-chart
approximation and is most accurate near even money; and the sample is whatever
you downloaded, so thin reference lines are honestly thin. Turn the whole thing
off with `enabled = false` under `[halfpoint]`.

## Watch mode

```bash
cfb-edge watch                      # every 15 minutes
cfb-edge watch --interval 5 --min-edge 2
cfb-edge watch --show-dropped       # also report lines that fell off
```

The first pass prints every qualifying line, because on the first pass they are
all new. After that the loop is quiet unless something moves:

```
[Sat 09/20 10:15 AM] 2 change(s) across 5 qualifying line(s) — odds pulled Sat 09/20 10:15 AM ET
CHANGE              GAME                          KICKOFF (ET)        MARKET  PICK                        DK                SHARP  FAIR%  EDGE  EV/$100   STAKE
------------------  ----------------------------  ------------------  ------  ------------------------  ----  -------------------  -----  ----  -------  ------
new                 Texas Longhorns @ Oklahoma …  Sat 09/20 8:00 PM   Total   Over 44.5                 +110  -109 (consensus(2))  49.8%  2.2%    +4.55  $20.66
price +110→+125     Ohio State Buckeyes @ Michi…  Sat 09/20 12:00 PM  Spread  Michigan Wolverines +6.5  +125         -110 (circa)  50.0%  4.4%    +9.00  $40.00

[Sat 09/20 10:30 AM] 5 qualifying line(s), nothing new (48 games)
```

A line is tracked by game, market and side, so a number move is reported as one
change rather than a disappearance and a new arrival. Price and number moves are
always reported; an edge move has to clear `--edge-delta` (0.25 points by
default). Every pass is still written to the CSV, JSON and SQLite log, and a scan
that fails does not end the loop.

## Player props and alternate lines

Props and alternate spreads/totals come from a different endpoint, one request
per game, billed per market. A 60-game Saturday at six prop markets is 360 quota
units against the 1 the main scan costs. So they are opt-in and fenced:

```bash
cfb-edge --props                          # props for games kicking off within 24h
cfb-edge --alts                           # alternate spreads and totals
cfb-edge --props --props-window 6 --yes   # next six hours, no prompt
cfb-edge --props --prop-market player_rush_yds
```

Games are taken soonest-first, capped at `max_events` (25), and anything above
`confirm_threshold` (10 quota units) asks before spending. In a non-interactive
shell it refuses instead of hanging, and tells you to pass `--yes`. Props are
priced exactly like game markets — Pinnacle first, then Circa, then consensus —
except each player is their own two-sided market. Pinnacle's college prop
coverage is thin, so expect a lot of consensus pricing and `no sharp line`.

## Logging bets and measuring CLV

```bash
cfb-edge bets add --event "Michigan" --market spreads --side michigan \
                  --point 6.5 --price 110 --stake 25
cfb-edge bets list
cfb-edge clv
```

`bets add` resolves the game out of the run log by event id or team name, accepts
partial sides (`michigan`, `over`), and attaches what the tool thought the line
was worth at the time:

```
logged bet #1: Michigan Wolverines +6.5 +110 for $25.00 at draftkings
  Ohio State Buckeyes @ Michigan Wolverines
  your implied probability: 47.6%
  fair at the time of the last scan: 50.0% (edge 2.38%)
```

`cfb-edge clv` compares each bet to the closing line — the last observation of
that side before kickoff:

```
#  GAME                          PICK                      YOURS       CLOSE     CLV  ADJ CLV   STAKE  NOTE
-  ----------------------------  ------------------------  -----  ----------  ------  -------  ------  ---------------------------
2  Georgia Bulldogs @ Alabama C  Over 51.5                  -110  -110 @52.5  +0.00%   +3.48%  $40.00  closed on 52.5, bet was 51.5
1  Ohio State Buckeyes @ Michig  Michigan Wolverines +6.5   +110        -120  +6.93%        -  $25.00

2 scored bet(s) | beat the close 2/2 (100%) | average CLV +5.21 points of probability
```

CLV is the closing implied probability minus yours, in points of probability;
positive means the market moved your way. When the number moved too, the raw CLV
is not a like-for-like comparison — so if the half-point table is built, **ADJ
CLV** re-prices the closing line onto the number you actually bet. Bets at
another book are still scored against DK's close, and the note says so. Games
that have not kicked off are excluded unless you pass `--all`.

## Output

Each scan writes two files to `data/runs/`, named with the run id
(`cfb-edge_20260918T140500Z-04f716.csv` / `.json`):

- **CSV** — one row per evaluated DraftKings line, ranked bets first, then
  half-point-translated lines, then number mismatches, then everything else.
  Includes both teams, kickoff in UTC and ET, both prices and probabilities, the
  sharp source and its hold, edge, EV, stake, `status`, `translated_from` and
  `above_min_edge`.
- **JSON** — the same rows plus run metadata: source (`api`/`cache`), snapshot
  path, markets, thresholds, bankroll settings, book configuration, status
  counts, per-game request count and the API quota headers.

## The run log (SQLite)

Everything lands in `data/cfb_edge.sqlite`:

- `runs` — one row per run: timestamps, source, markets, thresholds, bankroll,
  counts, quota remaining, and the CSV/JSON paths.
- `observations` — one row per evaluated DK line per run, stamped with the
  snapshot's fetch time. This is the line-movement history.
- `bets` — what you logged with `bets add`.
- `notifications` — what has been announced to Discord, so nothing repeats.
- `closing_lines` (view) — each side's last observation before kickoff.
- `clv` (view) — every observation joined to that side's closing line, with
  `clv_prob_delta = closing implied probability − implied probability at the
  time`.

```sql
-- How one side moved over the week
SELECT observed_at_utc, dk_point, dk_price, sharp_price, edge_pct
FROM observations
WHERE event_id = 'g2michigan' AND market = 'spreads' AND side = 'Michigan Wolverines'
ORDER BY observed_at_utc;

-- CLV on everything the tool flagged, whether or not you bet it
SELECT matchup, market, side, dk_point, dk_price,
       closing_dk_price, ROUND(clv_prob_delta * 100, 2) AS clv_pct
FROM clv
WHERE status IN ('priced', 'translated') AND edge_pct >= 1
ORDER BY clv_pct DESC;
```

The `clv` view joins on event/market/side, not on the number, so check
`dk_point` against `closing_dk_point` before reading a total's CLV. The
`cfb-edge clv` command does that adjustment for your own bets; this view does
not.

The schema migrates itself forward on open, so an older database keeps working.

## Discord alerts

```bash
cfb-edge --notify-dry-run    # print the payload, send nothing
cfb-edge --notify            # send it
cfb-edge watch --notify      # announce new prices as they appear
```

Set `DISCORD_WEBHOOK_URL` in `.env` (preferred) or `webhook_url` under
`[discord]` in `config.toml`. The threshold is `[discord] min_edge`, 2% by
default, and is independent of `--min-edge`, so you can watch a 1% board and only
be pinged for the 2%+ ones. Each line is announced once per price: if DK moves
from +110 to +125 you get told again, but a rescan that finds the same price does
not repeat itself. A webhook that is down or misconfigured prints a warning and
leaves the scan alone, and nothing is recorded as sent unless Discord accepted it.

## Web dashboard

A single page, no build step: FastAPI serves one HTML file, the page fetches
JSON and renders the ranked table in the browser.

```bash
pip install -r requirements-web.txt
export DASHBOARD_PASSWORD='something your friends can remember'
cfb-edge serve                      # http://127.0.0.1:8000
cfb-edge serve --host 0.0.0.0 --port 8080
```

The dashboard **never scans on request**. A background task runs the scan every
30 minutes (`[web] refresh_minutes`, or `DASHBOARD_REFRESH_MINUTES`) and every
page load reads the last finished scan, so opening the page ten times costs
nothing. The page polls for new results once a minute, and the header carries
the stamp:

```
Updated Sat 09/20 10:15 AM ET (2 min ago) · 48 games · live pull from Sat 09/20 10:15 AM ET
```

It shows the same ranked table as the terminal — game, kickoff in ET, market,
pick, DK price, sharp price, fair %, DK %, edge, EV per $100 and stake — with a
`½pt` badge on any line priced through the half-point table. Filters for market
and minimum edge are client-side, so they are instant and cost no requests; they
are remembered per browser. On a phone each bet becomes a labelled card rather
than a table you have to scroll sideways.

A **Refresh** button forces a scan, rate-limited to once every 30 seconds. It is
usually free: the odds cache means a refresh inside `max_age_minutes` replays
the last pull instead of spending quota.

If a refresh fails — a dead key, an API outage — the previous results stay on
screen under a banner saying what went wrong and how old they are. It does not
blank the page, and the schedule keeps trying.

### The password

One shared password, set as `DASHBOARD_PASSWORD`. Signing in exchanges it for an
HMAC-signed cookie (HttpOnly, SameSite=Lax, Secure behind HTTPS) that lasts 30
days, so the password is not re-sent on every request. Wrong guesses are
throttled per IP: eight failures in five minutes and that address is locked out
for the rest of the window.

**If `DASHBOARD_PASSWORD` is unset the dashboard refuses to serve anything** and
the background scan does not start — it fails closed rather than publishing your
board to anyone with the URL. `/healthz` stays up either way so a platform health
check still passes, and it reports whether the password is configured.

Set `SECRET_KEY` too if you want sessions to survive a password change.
Otherwise the signing key is derived from the password, which means changing the
password signs everyone out — usually what you want.

This is one password shared by a group. It keeps a public URL from being
world-readable; it is not a user system, there are no accounts, and anyone with
the password has the whole board.

### Deploying to Railway

`Dockerfile` and `railway.toml` are in the repo, so Railway needs no build
configuration:

1. Create a project from this repo. `railway.toml` selects the Dockerfile
   builder and points the health check at `/healthz`.
2. Under **Variables**, set:

   | Variable | Required | What it is |
   | --- | --- | --- |
   | `ODDS_API_KEY` | yes | The Odds API key |
   | `DASHBOARD_PASSWORD` | yes | The shared password |
   | `SECRET_KEY` | no | Cookie signing key; defaults to one derived from the password |
   | `BANKROLL` | no | Stake sizing, default 1000 |
   | `KELLY_FRACTION` | no | Default 0.25 |
   | `MIN_EDGE` | no | Where the page's edge filter starts, default 1.0 |
   | `MARKETS` | no | e.g. `spreads,totals` |
   | `DASHBOARD_REFRESH_MINUTES` | no | Default 30 |
   | `DASHBOARD_TITLE` | no | Page title |
   | `DATA_DIR` | no | Where the cache and run log are written, default `/app/data`. Set it to your volume's mount path |
   | `APP_USER` | no | User the entrypoint drops to, default `cfbedge` |

   `config.toml` is gitignored, so it is not in the image — on Railway these
   variables are how you configure it. Railway sets `PORT` itself.
3. Generate a domain and open it.

### Volumes, and why the container starts as root

Railway's filesystem is ephemeral, so the odds cache and the SQLite run log are
lost on redeploy unless you attach a volume. Add one, then set `DATA_DIR` to the
same path you mounted it at (`/data` if you mounted at `/data`).

A mounted volume arrives owned by `root`, which an unprivileged process cannot
write to — that is a `PermissionError: [Errno 13] Permission denied: '/data'` on
the first scan. So the image has no `USER` line. It starts as root, and
`ENTRYPOINT` runs `python -m cfb_edge.entrypoint`, which:

1. creates `DATA_DIR` and its `cache/` and `runs/` subdirectories,
2. hands them to `$APP_USER` (`cfbedge`, uid 10001), skipping anything already
   owned by that user so a restart with a big cache is not a long chown,
3. drops to that user for good, and
4. `exec`s the real command, so it still runs as PID 1 and gets signals directly.

Only those few lines ever hold privilege; the app itself runs unprivileged. If
you start the container with an explicit `--user`, the entrypoint leaves the
volume alone and that user has to be able to write to it already.

The app also creates its directories on startup, and `/healthz` reports anything
it cannot write:

```json
{"ok": true, "data_dir": "/data", "data_dir_problems": ["/data is not writable by uid 10001"]}
```

An empty `data_dir_problems` means the volume is set up correctly.

Keep `numReplicas = 1`: each replica runs its own scan schedule, so two replicas
means two sets of API requests against one quota.

Locally the same image runs with:

```bash
docker build -t cfb-edge .
docker run --rm -p 8000:8000 \
  -e ODDS_API_KEY=... -e DASHBOARD_PASSWORD=... \
  -v "$PWD/data:/app/data" cfb-edge
```

The bind mount is owned by whoever ran `docker run`; the entrypoint takes care
of it the same way.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

476 tests, no network access required. The odds conversion and de-vig math are
covered against known values (`test_oddsmath.py`), along with sharp-book
selection and consensus grouping (`test_fair.py`), edge, number-mismatch and
half-point-translation handling (`test_edges.py`), the half-point model itself
(`test_halfpoint.py`), the results fetcher (`test_scores.py`), props and quota
guarding (`test_props.py`), watch-mode diffing (`test_watch.py`), bet logging and
CLV (`test_bets.py`), Discord payloads and de-duplication (`test_notify.py`),
caching (`test_cache.py`), the dashboard's auth, JSON API and scan schedule
(`test_web.py`), the container entrypoint that prepares a mounted volume
(`test_entrypoint.py`), and the CLI end to end against a sample board in
`tests/fixtures/sample_odds.json` (`test_cli.py`).

Network-facing code is tested against stubs. `tests/synthetic.py` generates
historical games with realistic key-number structure so the half-point machinery
can be exercised deterministically — it is scaffolding, not data.

## Limitations

- Edges are only as good as the sharp line. A stale Pinnacle price or a
  consensus built from two soft books on the same bad number will produce edges
  that are not real.
- De-vigging is proportional (multiplicative). It is the standard approach and
  the right default, but it slightly overrates heavy favorites compared to
  power/Shin methods; on big moneyline dogs treat the fair price as approximate.
- The half-point table is an empirical model with the assumptions listed above,
  built from whatever seasons you downloaded. It narrows the gap on a
  half-point difference; it does not close it.
- Props are priced with the same machinery as game markets, but sharp coverage
  is thinner and DK's prop hold is much larger, so edges there need more
  scepticism, not less.
- The tool finds, sizes and logs bets. It does not place them, and it does not
  know whether your account is limited.
