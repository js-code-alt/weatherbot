#!/usr/bin/env python3
"""Phase 3: Reconstruct what the bot would have forecast for each event.

For each (city, event_date) in prices.db, fetches historical model forecasts
from Open-Meteo archive APIs and replays the bot's consensus + sigma + Monte
Carlo pipeline to produce a bot_prob for each bucket.

LIMITATIONS (important):
- Open-Meteo historical-forecast API returns the model's final settled
  forecast for that day, NOT necessarily what was live when the bot would
  have scanned. Real-time D+0 forecast error is 2-3x archived error.
- Ensemble historical data is not available via Open-Meteo; we approximate
  sigma from model spread only (which is then floored by SIGMA_FLOORS).
- No NWS data (bot uses it for US cities); excluded from reconstruction.
- Bucket probs use the NEW sigma floors (D+0=3.0°) matching the current bot.

Schema addition to prices.db:
  forecasts(
    city TEXT, event_date TEXT,
    ecmwf REAL, gfs REAL, icon REAL,
    consensus REAL, sigma REAL, model_spread REAL,
    actual REAL,
    PRIMARY KEY (city, event_date)
  )
  bot_probs(
    token_id TEXT PRIMARY KEY,
    prob REAL,           -- bot's estimated probability for this bucket
    edge_static REAL     -- prob - initial_market_price (first price observed)
  )

Usage:
  python reconstruct_forecasts.py                # All events
  python reconstruct_forecasts.py --city dallas  # One city
"""

import argparse
import json
import math
import random
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

HERE = Path(__file__).parent
DB_PATH = HERE / "prices.db"

# --- Copied from bot_v3.py to mirror behavior exactly ---
WEIGHTS = {"ecmwf": 0.30, "gfs": 0.20, "nws": 0.15, "icon": 0.15, "ensemble": 0.20}
SIGMA_FLOORS = {0: 3.0, 1: 1.7, 2: 2.2, 3: 2.9, 4: 3.2}  # NEW floors
MC_SIMS = 10000

CITY_BIASES = {
    "nyc":     {"ecmwf": -1.43, "icon": -0.7},
    "chicago": {"ecmwf": -0.64, "gfs": 2.03, "icon": 0.51},
    "miami":   {"ecmwf": -1.55, "gfs": 0.58, "icon": 1.18},
    "dallas":  {"ecmwf": -0.53, "gfs": 1.07, "icon": 1.4},
    "seattle": {"ecmwf": -1.02, "icon": -0.48},
    "atlanta": {"ecmwf": -0.56, "gfs": 1.33},
    "denver":  {"gfs": -1.08, "icon": -0.77},
    "phoenix": {"icon": 0.53},
    "london":  {"icon": 0.55},
    "tokyo":   {"ecmwf": 0.58, "icon": 1.14},
    "seoul":   {"ecmwf": -2.48, "gfs": -6.23, "icon": -4.17},
    "paris":   {"gfs": 0.41, "icon": 0.76},
}

LOCATIONS = {
    "nyc":     {"lat": 40.7772, "lon": -73.8726, "unit": "fahrenheit"},
    "chicago": {"lat": 41.9742, "lon": -87.9073, "unit": "fahrenheit"},
    "miami":   {"lat": 25.7959, "lon": -80.2870, "unit": "fahrenheit"},
    "dallas":  {"lat": 32.8471, "lon": -96.8518, "unit": "fahrenheit"},
    "seattle": {"lat": 47.4502, "lon": -122.3088, "unit": "fahrenheit"},
    "atlanta": {"lat": 33.6407, "lon": -84.4277, "unit": "fahrenheit"},
    "denver":  {"lat": 39.8561, "lon": -104.6737, "unit": "fahrenheit"},
    "phoenix": {"lat": 33.4373, "lon": -112.0078, "unit": "fahrenheit"},
    "london":  {"lat": 51.4700, "lon": -0.4543, "unit": "celsius"},
    "tokyo":   {"lat": 35.5533, "lon": 139.7811, "unit": "celsius"},
    "seoul":   {"lat": 37.4602, "lon": 126.4407, "unit": "celsius"},
    "paris":   {"lat": 49.0097, "lon": 2.5478, "unit": "celsius"},
}


def fetch_json(url, retries=3):
    req = Request(url, headers={"User-Agent": "weatherbot-backtest/1.0"})
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except (URLError, HTTPError):
            if attempt == retries - 1:
                raise
            time.sleep(1.5 ** attempt)
    return None


def fetch_historical_model(city, date_str, model):
    """Fetch a single model's forecast for a date."""
    loc = LOCATIONS[city]
    unit = loc["unit"]
    url = (f"https://historical-forecast-api.open-meteo.com/v1/forecast"
           f"?latitude={loc['lat']}&longitude={loc['lon']}"
           f"&daily=temperature_2m_max&temperature_unit={unit}"
           f"&timezone=auto&start_date={date_str}&end_date={date_str}"
           f"&models={model}")
    try:
        data = fetch_json(url)
        if not data or "daily" not in data:
            return None
        temps = data["daily"].get("temperature_2m_max", [])
        # Output unit stays in the city's native unit (°F for US, °C otherwise)
        # Bot compares against buckets which are °F for US, °C otherwise — matches.
        return temps[0] if temps and temps[0] is not None else None
    except Exception:
        return None


def fetch_actual(city, date_str):
    loc = LOCATIONS[city]
    unit = loc["unit"]
    url = (f"https://archive-api.open-meteo.com/v1/archive"
           f"?latitude={loc['lat']}&longitude={loc['lon']}"
           f"&daily=temperature_2m_max&temperature_unit={unit}"
           f"&timezone=auto&start_date={date_str}&end_date={date_str}")
    try:
        data = fetch_json(url)
        if not data or "daily" not in data:
            return None
        temps = data["daily"].get("temperature_2m_max", [])
        return temps[0] if temps and temps[0] is not None else None
    except Exception:
        return None


def compute_consensus(ecmwf, gfs, icon, city):
    """Mirror of bot_v3.compute_consensus, without NWS/ensemble (unavailable historically)."""
    available = {}
    if ecmwf is not None: available["ecmwf"] = ecmwf
    if gfs is not None: available["gfs"] = gfs
    if icon is not None: available["icon"] = icon

    if not available:
        return None, None, None

    biases = CITY_BIASES.get(city, {})
    corrected = {k: v - biases.get(k, 0) for k, v in available.items()}

    total_w = sum(WEIGHTS[k] for k in corrected)
    consensus = sum(corrected[k] * WEIGHTS[k] / total_w for k in corrected)
    consensus = round(consensus, 1)

    temps = list(corrected.values())
    spread = max(temps) - min(temps) if len(temps) > 1 else 0

    # No ensemble std available historically; use spread/2
    if len(temps) > 1:
        sigma = spread / 2.0
    else:
        sigma = 2.0

    return consensus, sigma, spread


def apply_sigma_floor(sigma, days_out=0):
    return max(sigma, SIGMA_FLOORS.get(days_out, 3.0))


def monte_carlo_bucket_prob(consensus, sigma, bucket_low, bucket_high, n_sims=MC_SIMS, df=5):
    """Student's t Monte Carlo — returns P(bucket contains daily max)."""
    count = 0
    for _ in range(n_sims):
        z = random.gauss(0, 1)
        v = random.gammavariate(df / 2.0, 2.0)
        t_sample = z / math.sqrt(v / df)
        s = consensus + sigma * t_sample
        # Open-ended low bucket ("X or below"): low = -999
        if bucket_low <= -900:
            if s <= bucket_high:
                count += 1
        # Open-ended high bucket ("X or higher"): high = 999
        elif bucket_high >= 900:
            if s >= bucket_low:
                count += 1
        else:
            if bucket_low <= s <= bucket_high:
                count += 1
    return count / n_sims


def init_schema(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS forecasts (
            city TEXT,
            event_date TEXT,
            ecmwf REAL,
            gfs REAL,
            icon REAL,
            consensus REAL,
            sigma_raw REAL,
            sigma_floored REAL,
            model_spread REAL,
            actual REAL,
            PRIMARY KEY (city, event_date)
        );
        CREATE TABLE IF NOT EXISTS bot_probs (
            token_id TEXT PRIMARY KEY,
            prob REAL,
            initial_market_price REAL,
            edge_static REAL
        );
    """)
    con.commit()


def get_initial_market_price(con, token_id):
    """Get first observed price for a token."""
    row = con.execute(
        "SELECT p FROM prices WHERE token_id=? ORDER BY ts ASC LIMIT 1",
        (token_id,)
    ).fetchone()
    return row[0] if row else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", type=str, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.1)
    args = ap.parse_args()

    random.seed(42)  # reproducibility

    con = sqlite3.connect(DB_PATH)
    init_schema(con)

    # Distinct (city, date) from markets table
    where = "WHERE city=?" if args.city else ""
    params = (args.city,) if args.city else ()
    event_pairs = con.execute(
        f"SELECT DISTINCT city, event_date FROM markets {where} ORDER BY city, event_date",
        params
    ).fetchall()
    print(f"Events to reconstruct: {len(event_pairs)}", file=sys.stderr)

    already_done = set()
    if not args.force:
        for row in con.execute("SELECT city, event_date FROM forecasts WHERE consensus IS NOT NULL"):
            already_done.add((row[0], row[1]))

    todo = [(c, d) for (c, d) in event_pairs if (c, d) not in already_done]
    print(f"Already done: {len(already_done)}. Remaining: {len(todo)}", file=sys.stderr)

    t0 = time.time()
    for i, (city, date) in enumerate(todo):
        e = fetch_historical_model(city, date, "ecmwf_ifs025"); time.sleep(args.sleep)
        g = fetch_historical_model(city, date, "gfs_seamless"); time.sleep(args.sleep)
        ic = fetch_historical_model(city, date, "icon_seamless"); time.sleep(args.sleep)
        actual = fetch_actual(city, date); time.sleep(args.sleep)

        consensus, sigma_raw, spread = compute_consensus(e, g, ic, city)
        sigma_floored = apply_sigma_floor(sigma_raw, days_out=0) if sigma_raw is not None else None

        con.execute("""
            INSERT OR REPLACE INTO forecasts
            (city, event_date, ecmwf, gfs, icon, consensus, sigma_raw, sigma_floored, model_spread, actual)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (city, date, e, g, ic, consensus, sigma_raw, sigma_floored, spread, actual))

        # Compute bot_probs for every YES token in this event
        if consensus is not None and sigma_floored is not None:
            rows = con.execute(
                "SELECT token_id, bucket_low, bucket_high FROM markets WHERE city=? AND event_date=?",
                (city, date)
            ).fetchall()
            for tok, bl, bh in rows:
                if bl is None or bh is None:
                    continue
                prob = monte_carlo_bucket_prob(consensus, sigma_floored, bl, bh)
                init_price = get_initial_market_price(con, tok)
                edge = prob - init_price if init_price is not None else None
                con.execute(
                    "INSERT OR REPLACE INTO bot_probs (token_id, prob, initial_market_price, edge_static) VALUES (?,?,?,?)",
                    (tok, prob, init_price, edge)
                )

        if (i + 1) % 20 == 0:
            con.commit()
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            eta = (len(todo) - i - 1) / rate if rate > 0 else 0
            print(f"  {i+1}/{len(todo)} events | {rate:.2f} ev/s | ETA {eta:.0f}s", file=sys.stderr)

    con.commit()
    con.close()
    print(f"\nDone.", file=sys.stderr)


if __name__ == "__main__":
    main()
