#!/usr/bin/env python3
"""Fetch Polymarket historical weather events for backtesting.

Enumerates weather events via the /events?slug=... endpoint using the
same slug pattern the bot queries:
  highest-temperature-in-{city}-on-{month}-{day}-{year}

Outputs events.jsonl with markets, prices, buckets, volumes.

Usage:
    python fetch_weather_markets.py                      # Default: 30 days back, all cities
    python fetch_weather_markets.py --days 60            # 60 days back
    python fetch_weather_markets.py --city dallas        # One city
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

API = "https://gamma-api.polymarket.com/events"
OUT = Path(__file__).parent / "events.jsonl"

CITIES = ["nyc", "chicago", "miami", "dallas", "seattle", "atlanta",
          "denver", "phoenix", "london", "tokyo", "seoul", "paris"]

# NYC uses "nyc" in some slugs, "new-york-city" in others — check both
SLUG_ALIASES = {
    "nyc": ["nyc", "new-york-city", "new-york"],
}

MONTHS = ["january", "february", "march", "april", "may", "june",
          "july", "august", "september", "october", "november", "december"]


def fetch_event(slug):
    url = f"{API}?slug={slug}"
    req = Request(url, headers={"User-Agent": "weatherbot-backtest/1.0"})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            if e.code == 404:
                return []
            if attempt == 2:
                return None
            time.sleep(1.5 ** attempt)
        except URLError:
            if attempt == 2:
                return None
            time.sleep(1.5 ** attempt)
    return None


def build_slugs(city, date):
    """Return candidate slugs for a city/date combination."""
    month = MONTHS[date.month - 1]
    day = date.day
    year = date.year
    aliases = SLUG_ALIASES.get(city, [city])
    return [f"highest-temperature-in-{a}-on-{month}-{day}-{year}" for a in aliases]


def survey(cities, days_back):
    today = datetime.now(timezone.utc).date()
    dates = [today - timedelta(days=n) for n in range(1, days_back + 1)]  # skip today (unresolved)

    events = []
    total_queries = 0
    hits = 0
    with open(OUT, "w") as f:
        for city in cities:
            city_hits = 0
            for date in dates:
                for slug in build_slugs(city, date):
                    total_queries += 1
                    data = fetch_event(slug)
                    if data and isinstance(data, list) and len(data) > 0:
                        ev = data[0]
                        record = {
                            "city": city,
                            "date": date.isoformat(),
                            "slug": ev.get("slug"),
                            "title": ev.get("title"),
                            "startDate": ev.get("startDate"),
                            "endDate": ev.get("endDate"),
                            "volume": ev.get("volume"),
                            "markets": [
                                {
                                    "question": m.get("question"),
                                    "conditionId": m.get("conditionId"),
                                    "clobTokenIds": m.get("clobTokenIds"),
                                    "outcomes": m.get("outcomes"),
                                    "outcomePrices": m.get("outcomePrices"),
                                    "volume": m.get("volume"),
                                    "volumeNum": m.get("volumeNum"),
                                    "closed": m.get("closed"),
                                }
                                for m in ev.get("markets", [])
                            ],
                        }
                        events.append(record)
                        f.write(json.dumps(record) + "\n")
                        hits += 1
                        city_hits += 1
                        break  # found it; don't try other aliases
                # Small delay to be polite
                time.sleep(0.05)
            print(f"  {city:<8} {city_hits}/{len(dates)} days found", file=sys.stderr)
    print(f"\nTotal queries: {total_queries}", file=sys.stderr)
    print(f"Total events found: {hits}", file=sys.stderr)
    return events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--city", type=str, default=None)
    args = ap.parse_args()

    cities = [args.city] if args.city else CITIES
    events = survey(cities, args.days)

    # Summary
    by_city = {}
    total_volume = 0
    total_markets = 0
    for e in events:
        by_city[e["city"]] = by_city.get(e["city"], 0) + 1
        try:
            total_volume += float(e.get("volume") or 0)
        except (TypeError, ValueError):
            pass
        total_markets += len(e.get("markets", []))

    print(f"\n=== SUMMARY ===")
    print(f"Events found: {len(events)}")
    print(f"Total markets (buckets): {total_markets}")
    print(f"Total volume (USD): ${total_volume:,.0f}")
    print(f"\nPer-city event counts:")
    for c in CITIES:
        n = by_city.get(c, 0)
        marker = "✓" if n > 0 else " "
        print(f"  {marker} {c:<10} {n}")
    print(f"\nSaved: {OUT}")


if __name__ == "__main__":
    main()
