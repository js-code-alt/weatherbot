#!/usr/bin/env python3
"""Phase 2: Download historical prices for every bucket in events.jsonl.

Hits https://clob.polymarket.com/prices-history?market={token}&interval=max
for each market token, storing results in prices.db (SQLite).

Schema:
  markets(
    token_id TEXT PRIMARY KEY,
    event_slug TEXT, event_date TEXT, city TEXT,
    question TEXT, condition_id TEXT,
    outcome TEXT,                  -- "YES" or "NO"
    bucket_low REAL, bucket_high REAL,
    final_price REAL,              -- 0 or 1 (resolution)
    volume REAL, closed INTEGER
  )
  prices(
    token_id TEXT, ts INTEGER, p REAL,
    PRIMARY KEY (token_id, ts)
  )

Resumable: skips tokens already fully downloaded.

Usage:
  python download_prices.py                      # All events from events.jsonl
  python download_prices.py --city dallas        # One city
  python download_prices.py --limit 5            # First N events (smoke test)
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

HERE = Path(__file__).parent
DB_PATH = HERE / "prices.db"
EVENTS = HERE / "events.jsonl"
CLOB = "https://clob.polymarket.com/prices-history"


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS markets (
            token_id TEXT PRIMARY KEY,
            event_slug TEXT,
            event_date TEXT,
            city TEXT,
            question TEXT,
            condition_id TEXT,
            outcome TEXT,
            bucket_low REAL,
            bucket_high REAL,
            final_price REAL,
            volume REAL,
            closed INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_markets_city_date ON markets(city, event_date);
        CREATE INDEX IF NOT EXISTS idx_markets_event ON markets(event_slug);

        CREATE TABLE IF NOT EXISTS prices (
            token_id TEXT NOT NULL,
            ts INTEGER NOT NULL,
            p REAL NOT NULL,
            PRIMARY KEY (token_id, ts)
        );

        CREATE TABLE IF NOT EXISTS fetch_state (
            token_id TEXT PRIMARY KEY,
            fetched_at INTEGER,
            n_points INTEGER,
            status TEXT
        );
    """)
    con.commit()
    return con


def parse_bucket(question):
    """Return (low, high) or None. °F only (bot's format)."""
    if not question:
        return None
    num = r"(-?\d+(?:\.\d+)?)"

    # "X°F or below"
    m = re.search(num + r"\s*°?[FC]\s*or below", question, re.IGNORECASE)
    if m:
        return (-999.0, float(m.group(1)))

    # "X°F or higher"
    m = re.search(num + r"\s*°?[FC]\s*or higher", question, re.IGNORECASE)
    if m:
        return (float(m.group(1)), 999.0)

    # "between X-Y°F"
    m = re.search(r"between\s+" + num + r"\s*-\s*" + num + r"\s*°?[FC]", question, re.IGNORECASE)
    if m:
        return (float(m.group(1)), float(m.group(2)))

    # "be X°F on" (exact)
    m = re.search(r"be\s+" + num + r"\s*°?[FC]\s+on", question, re.IGNORECASE)
    if m:
        v = float(m.group(1))
        return (v - 0.5, v + 0.5)

    return None


def fetch_prices(token_id, max_retries=3):
    url = f"{CLOB}?market={token_id}&interval=max"
    req = Request(url, headers={"User-Agent": "weatherbot-backtest/1.0"})
    for attempt in range(max_retries):
        try:
            with urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())
                return data.get("history", [])
        except HTTPError as e:
            if e.code == 429:
                time.sleep(2 ** attempt + 1)
                continue
            if e.code == 404:
                return []
            if attempt == max_retries - 1:
                raise
        except URLError:
            if attempt == max_retries - 1:
                raise
            time.sleep(2 ** attempt)
    return []


def load_events(city_filter=None, limit=None):
    events = []
    with open(EVENTS) as f:
        for line in f:
            e = json.loads(line)
            if city_filter and e["city"] != city_filter:
                continue
            events.append(e)
            if limit and len(events) >= limit:
                break
    return events


def expand_markets(events):
    """Flatten each event into per-token market records."""
    rows = []
    for ev in events:
        for m in ev.get("markets", []):
            q = m.get("question") or ""
            bucket = parse_bucket(q) or (None, None)

            tokens = m.get("clobTokenIds")
            if isinstance(tokens, str):
                try:
                    tokens = json.loads(tokens)
                except json.JSONDecodeError:
                    continue
            if not tokens or len(tokens) < 2:
                continue

            outcomes = m.get("outcomes")
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except json.JSONDecodeError:
                    outcomes = ["Yes", "No"]

            outcome_prices = m.get("outcomePrices")
            if isinstance(outcome_prices, str):
                try:
                    outcome_prices = json.loads(outcome_prices)
                except json.JSONDecodeError:
                    outcome_prices = [None, None]

            # Typically clobTokenIds[0] = YES token, [1] = NO
            for i, tok in enumerate(tokens[:2]):
                outcome = (outcomes[i] if outcomes and i < len(outcomes) else ("YES" if i == 0 else "NO")).upper()
                try:
                    final_price = float(outcome_prices[i]) if outcome_prices and i < len(outcome_prices) else None
                except (TypeError, ValueError):
                    final_price = None
                rows.append({
                    "token_id": tok,
                    "event_slug": ev.get("slug"),
                    "event_date": ev.get("date"),
                    "city": ev.get("city"),
                    "question": q,
                    "condition_id": m.get("conditionId"),
                    "outcome": outcome,
                    "bucket_low": bucket[0],
                    "bucket_high": bucket[1],
                    "final_price": final_price,
                    "volume": float(m.get("volumeNum") or 0),
                    "closed": 1 if m.get("closed") else 0,
                })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", type=str, default=None)
    ap.add_argument("--limit", type=int, default=None, help="Limit to first N events")
    ap.add_argument("--skip-no", action="store_true", default=True,
                    help="Skip NO tokens (default): they mirror YES, half the work")
    ap.add_argument("--sleep", type=float, default=0.08, help="Seconds between requests")
    ap.add_argument("--force", action="store_true", help="Re-fetch even if already fetched")
    args = ap.parse_args()

    if not EVENTS.exists():
        print(f"ERROR: {EVENTS} not found. Run fetch_weather_markets.py first.", file=sys.stderr)
        sys.exit(1)

    con = init_db()
    events = load_events(city_filter=args.city, limit=args.limit)
    markets = expand_markets(events)
    if args.skip_no:
        markets = [m for m in markets if m["outcome"] == "YES"]

    # Insert/update market metadata first
    cur = con.cursor()
    for m in markets:
        cur.execute("""
            INSERT OR REPLACE INTO markets
            (token_id, event_slug, event_date, city, question, condition_id,
             outcome, bucket_low, bucket_high, final_price, volume, closed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (m["token_id"], m["event_slug"], m["event_date"], m["city"],
              m["question"], m["condition_id"], m["outcome"],
              m["bucket_low"], m["bucket_high"], m["final_price"],
              m["volume"], m["closed"]))
    con.commit()
    print(f"Registered {len(markets)} markets (YES tokens{', skipping NO' if args.skip_no else ''})", file=sys.stderr)

    # Figure out what's already done
    already = {}
    if not args.force:
        for row in cur.execute("SELECT token_id, n_points FROM fetch_state WHERE status='ok'"):
            already[row[0]] = row[1]

    todo = [m for m in markets if m["token_id"] not in already]
    print(f"Already fetched: {len(already)}. Remaining: {len(todo)}", file=sys.stderr)

    # Fetch loop
    errors = 0
    t0 = time.time()
    for i, m in enumerate(todo):
        tok = m["token_id"]
        try:
            history = fetch_prices(tok)
        except Exception as e:
            cur.execute(
                "INSERT OR REPLACE INTO fetch_state (token_id, fetched_at, n_points, status) VALUES (?,?,?,?)",
                (tok, int(time.time()), 0, f"err: {e}")
            )
            errors += 1
            if errors > 20:
                print(f"\n{errors} errors — aborting", file=sys.stderr)
                break
            continue

        if history:
            rows = [(tok, int(pt["t"]), float(pt["p"])) for pt in history if pt.get("t") and pt.get("p") is not None]
            cur.executemany(
                "INSERT OR IGNORE INTO prices (token_id, ts, p) VALUES (?, ?, ?)",
                rows
            )
        cur.execute(
            "INSERT OR REPLACE INTO fetch_state (token_id, fetched_at, n_points, status) VALUES (?,?,?,?)",
            (tok, int(time.time()), len(history), "ok")
        )
        if (i + 1) % 50 == 0:
            con.commit()
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            eta = (len(todo) - i - 1) / rate if rate > 0 else 0
            print(f"  {i+1}/{len(todo)} tokens | {rate:.1f} req/s | ETA {eta:.0f}s", file=sys.stderr)
        time.sleep(args.sleep)

    con.commit()
    con.close()
    print(f"\nDone. Errors: {errors}", file=sys.stderr)


if __name__ == "__main__":
    main()
