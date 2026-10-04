"""Minute-by-minute Kalshi tape watcher.  python3 watch.py [--once] [--max-minutes N]

PUBLIC ON PURPOSE. This repo holds no credentials beyond DISPATCH_TOKEN (a PAT that can only
fire repository_dispatch at the private repo) and reads only Kalshi's PUBLIC market-data
endpoints — the same trades anyone can see on the site. Everything private (keys, stakes,
balances, the decision to actually trade) lives on the other side of the dispatch: the private
repo re-reads prices itself and never trusts a number from here. GitHub Actions minutes are
free and unlimited on public repos, which is the entire reason this exists as its own repo
(same split as PinnacleBoard).

What it does, per minute, per pick in watchlist.json:
  - pulls the market's public trade tape and buckets it into per-minute medians, expressed as
    the PICK's price (the watchlist's market_side converts: holding 'no' means the pick trades
    as 100 - yes_price on that ticker)
  - logs any minute whose median is >= 55c to ticks.csv (the spec's reporting band)
  - START (kw-start): the first 10-minute window where the per-minute medians moved >= 8c
    cumulatively on >= 10 trades — the tape's own "the match has begun", because advertised
    start times are session placeholders and nothing else is trustworthy
  - BUY (kw-buy): from the detected start, the first minute the median prints >= 70c
  - drops the pick when Kalshi reports the market settled/finalized

Each event fires ONE repository_dispatch at the private repo and is remembered in state.json,
committed here, so a restarted job never re-fires. The private side is idempotent anyway
(kt_inplay is the once-per-pick guard) — two layers because dispatches also get redelivered.

The tape is rebuildable: Kalshi's trades endpoint returns full history, so a restart refetches
from scratch and recomputes the same medians. Nothing here is state that can be lost, except
the fired-flags, which are committed.

Runs as a single Actions job looping on a 60s clock for up to ~5.5h, restarted by a 15-min
cron (concurrency group keeps it to one). Stdlib only — no installs, nothing to break.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import ssl
import statistics
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# Stdlib-only on the Actions runner, where the system certs just work. macOS framework Python
# ships without them, so local testing borrows certifi's bundle when it happens to be around.
try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _CTX = None

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
GITHUB = "https://api.github.com"
PRIVATE_REPO = os.environ.get("PRIVATE_REPO", "Teemotaij/TennisGeneralModel")

TICK_BAND_C = 55          # report minutes at/above this (pick-equivalent median)
START_WINDOW_MIN = 10     # §1 detection window...
START_MOVE_C = 8          # ...cumulative per-minute median movement inside it...
START_TRADES = 10         # ...on at least this many trades
BUY_TRIGGER_C = 70        # §3

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(ROOT, "state.json")
TICKS = os.path.join(ROOT, "ticks.csv")
WATCHLIST = os.path.join(ROOT, "watchlist.json")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def http_json(url: str, headers: dict | None = None, data: dict | None = None,
              timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers={"accept": "application/json", **(headers or {})},
                                 data=json.dumps(data).encode() if data is not None else None,
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        body = r.read()
        return json.loads(body) if body else {}


# ------------------------------------------------------------------------------- kalshi reads
def market_status(ticker: str) -> str:
    try:
        return (http_json(f"{KALSHI}/markets/{ticker}").get("market") or {}).get("status", "")
    except Exception:
        return ""                                  # transient; try again next minute


def trades(ticker: str) -> list[tuple[str, int]]:
    """Full public tape for a ticker: [(created_time_iso, yes_price_cents), ...] oldest-last.

    Kalshi returns the whole history paginated, newest first. Tennis matches run a few
    thousand trades at most, so refetching the lot once a minute is cheap and makes the watcher
    stateless — a restarted job sees the identical tape. yes_price arrives as integer cents
    (legacy) or a fixed-point dollar string (`yes_price_dollars`, the V2 rollout); absent-reads-
    as-zero is the known V2 trap, so the dollars field is parsed rather than defaulted.
    """
    out, cursor = [], None
    while True:
        q = dict(ticker=ticker, limit=1000)
        if cursor:
            q["cursor"] = cursor
        try:
            d = http_json(f"{KALSHI}/markets/trades?{urllib.parse.urlencode(q)}")
        except Exception as e:
            # LOUD on purpose: a watcher that cannot read the tape but keeps looping is the
            # "alive but useless" failure shape — the log must show it every minute it persists.
            print(f"trades({ticker}) failed: {e}", file=sys.stderr, flush=True)
            return out
        ts = d.get("trades", [])
        for t in ts:
            px = t.get("yes_price")
            if px in (None, ""):
                dd = t.get("yes_price_dollars")
                px = int(round(float(dd) * 100)) if dd not in (None, "") else None
            if px is not None:
                out.append((t.get("created_time", ""), int(px)))
        cursor = d.get("cursor")
        if not cursor or not ts:
            return out


def minute_medians(tape: list[tuple[str, int]], market_side: str) -> list[tuple[str, float, int]]:
    """[(minute 'YYYY-MM-DDTHH:MM', pick-equivalent median, n_trades)], chronological."""
    buckets: dict[str, list[int]] = {}
    for created, yes in tape:
        if len(created) < 16:
            continue
        px = yes if market_side == "yes" else 100 - yes
        buckets.setdefault(created[:16], []).append(px)
    return [(m, statistics.median(v), len(v)) for m, v in sorted(buckets.items())]


# --------------------------------------------------------------------------------- detection
def detect_start(meds: list[tuple[str, float, int]]) -> dict | None:
    """§1: first 10-minute window (by clock, not by row) whose consecutive per-minute medians
    moved >= 8c cumulatively on >= 10 trades. Returns the window's first minute as start_at.
    """
    for i in range(len(meds)):
        t0 = meds[i][0]
        lo = datetime.fromisoformat(t0 + ":00+00:00")
        win = [meds[i]]
        for j in range(i + 1, len(meds)):
            tj = datetime.fromisoformat(meds[j][0] + ":00+00:00")
            if (tj - lo).total_seconds() >= START_WINDOW_MIN * 60:
                break
            win.append(meds[j])
        move = sum(abs(win[k][1] - win[k - 1][1]) for k in range(1, len(win)))
        n = sum(w[2] for w in win)
        if move >= START_MOVE_C and n >= START_TRADES:
            return dict(start_at=t0 + ":00Z", window_move_c=round(move, 1), window_trades=n,
                        detected_at=utcnow().isoformat())
    return None


def detect_buy(meds: list[tuple[str, float, int]], start_at: str) -> dict | None:
    """§3 trigger: first minute at/after the detected start whose median >= 70c."""
    floor = start_at[:16]
    for m, med, n in meds:
        if m >= floor and med >= BUY_TRIGGER_C:
            return dict(at=m + ":00Z", tape_px=med)
    return None


# ---------------------------------------------------------------------------------- dispatch
def dispatch(event: str, payload: dict) -> bool:
    token = os.environ.get("DISPATCH_TOKEN", "")
    if not token:
        print(f"DRY (no DISPATCH_TOKEN): {event} {payload}", flush=True)
        return True
    try:
        http_json(f"{GITHUB}/repos/{PRIVATE_REPO}/dispatches",
                  headers={"authorization": f"Bearer {token}",
                           "x-github-api-version": "2022-11-28"},
                  data=dict(event_type=event, client_payload=payload))
        print(f"dispatched {event}: {payload}", flush=True)
        return True
    except Exception as e:
        print(f"dispatch {event} FAILED: {e}", file=sys.stderr, flush=True)
        return False      # not marked fired; retried next minute


# ------------------------------------------------------------------------------------- state
def load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def log_ticks(rows: list[list]) -> None:
    new = not os.path.exists(TICKS)
    with open(TICKS, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["minute", "match_id", "side", "ticker", "median_c", "trades"])
        w.writerows(rows)


def git(*args) -> int:
    return subprocess.call(["git", "-C", ROOT, *args])


def sync(push: bool) -> None:
    """Pull the latest watchlist (the private repo pushes it here) and publish ticks/state.

    Commit-most-runs also keeps GitHub's 60-day schedule-disable clock from starting, the same
    trick PinnacleBoard uses.
    """
    git("add", "ticks.csv", "state.json")
    git("commit", "-q", "-m", f"ticks {utcnow().isoformat()[:16]}")
    git("pull", "-q", "--rebase", "-X", "theirs")
    if push:
        git("push", "-q")


# -------------------------------------------------------------------------------------- loop
def one_pass(st: dict) -> None:
    wl = (load_json(WATCHLIST, {}) or {}).get("picks", [])
    for p in wl:
        key = f"{p['match_id']}/{p['side']}"
        s = st.setdefault(key, {})
        if s.get("done"):
            continue
        status = market_status(p["ticker"])
        if status in ("settled", "finalized"):
            s["done"] = status
            continue
        meds = minute_medians(trades(p["ticker"]), p["market_side"])
        if not meds:
            continue

        last_min, last_med, last_n = meds[-1]
        if last_med >= TICK_BAND_C and s.get("last_logged") != last_min:
            log_ticks([[last_min, p["match_id"], p["side"], p["ticker"],
                        round(last_med, 1), last_n]])
            s["last_logged"] = last_min

        base = dict(match_id=p["match_id"], side=p["side"], ticker=p["ticker"])
        if not s.get("start"):
            d = detect_start(meds)
            if d and dispatch("kw-start", base | d):
                s["start"] = d
        if s.get("start") and not s.get("buy"):
            b = detect_buy(meds, s["start"]["start_at"])
            if b and dispatch("kw-buy", base | s["start"] | b):
                s["buy"] = b


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one pass, no loop, no git")
    ap.add_argument("--max-minutes", type=int, default=330,
                    help="exit before the 6h Actions job limit; the cron restarts us")
    args = ap.parse_args(argv)

    st = load_json(STATE, {})
    if args.once:
        one_pass(st)
        with open(STATE, "w") as f:
            json.dump(st, f, indent=1, sort_keys=True)
        return 0

    deadline = time.time() + args.max_minutes * 60
    push = bool(os.environ.get("GITHUB_ACTIONS"))
    last_sync = 0.0
    while time.time() < deadline:
        t0 = time.time()
        try:
            one_pass(st)
            with open(STATE, "w") as f:
                json.dump(st, f, indent=1, sort_keys=True)
        except Exception as e:                      # a bad minute must not kill the watch
            print(f"pass failed: {e}", file=sys.stderr, flush=True)
        if time.time() - last_sync >= 300:
            try:
                sync(push)
            except Exception as e:
                print(f"sync failed: {e}", file=sys.stderr, flush=True)
            last_sync = time.time()
        time.sleep(max(1.0, 60.0 - (time.time() - t0)))
    sync(push)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
