#!/usr/bin/env python3
"""
Weather Bot Backtest — Historical calibration using Open-Meteo data.

Pulls 30 days of historical forecasts and actuals for all 12 cities,
runs the Monte Carlo probability engine, and checks calibration.

Usage:
    python backtest.py                # Run full 30-day backtest
    python backtest.py --days 14      # Last 14 days
    python backtest.py --city seoul   # Single city
"""

import argparse
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError

# ── Config (must match bot_v3.py) ──

WEIGHTS = {"ecmwf": 0.35, "gfs": 0.25, "ensemble": 0.20}
SIGMA_FLOORS = {0: 1.3, 1: 1.5, 2: 1.8, 3: 2.5, 4: 3.0}
MC_SIMS = 10000

LOCATIONS = {
    "nyc":     {"lat": 40.7772, "lon": -73.8726, "unit": "fahrenheit", "name": "New York"},
    "chicago": {"lat": 41.9742, "lon": -87.9073, "unit": "fahrenheit", "name": "Chicago"},
    "miami":   {"lat": 25.7959, "lon": -80.2870, "unit": "fahrenheit", "name": "Miami"},
    "dallas":  {"lat": 32.8471, "lon": -96.8518, "unit": "fahrenheit", "name": "Dallas"},
    "seattle": {"lat": 47.4502, "lon": -122.3088, "unit": "fahrenheit", "name": "Seattle"},
    "atlanta": {"lat": 33.6407, "lon": -84.4277, "unit": "fahrenheit", "name": "Atlanta"},
    "denver":  {"lat": 39.8561, "lon": -104.6737, "unit": "fahrenheit", "name": "Denver"},
    "phoenix": {"lat": 33.4373, "lon": -112.0078, "unit": "fahrenheit", "name": "Phoenix"},
    "london":  {"lat": 51.4700, "lon": -0.4543, "unit": "celsius", "name": "London"},
    "tokyo":   {"lat": 35.5533, "lon": 139.7811, "unit": "celsius", "name": "Tokyo"},
    "seoul":   {"lat": 37.4602, "lon": 126.4407, "unit": "celsius", "name": "Seoul"},
    "paris":   {"lat": 49.0097, "lon": 2.5478, "unit": "celsius", "name": "Paris"},
}

# Polymarket uses 1°C or 1°F buckets centered on whole numbers
# e.g., "13°C" = 12.5-13.5, "≥23°C" = 23.0-999, "76-77°F" = 76.0-77.0
def generate_buckets(unit, actual_temp):
    """Generate realistic Polymarket-style temperature buckets around a temperature."""
    if unit == "celsius":
        center = round(actual_temp)
        buckets = {}
        # Create buckets from center-5 to center+5, plus edge buckets
        low = center - 5
        high = center + 5
        buckets[f"{low - 0.5 - 999.0}-{low - 0.5}"] = (-999.0, low - 0.5)
        for t in range(low, high + 1):
            buckets[f"{t - 0.5}-{t + 0.5}"] = (t - 0.5, t + 0.5)
        buckets[f"{high + 0.5}-999.0"] = (high + 0.5, 999.0)
        return buckets
    else:  # fahrenheit
        center = round(actual_temp)
        buckets = {}
        low = center - 8
        high = center + 8
        buckets[f"{low - 1.0}-{low}"] = (-999.0, float(low))
        for t in range(low, high + 1):
            buckets[f"{t}.0-{t + 1}.0"] = (float(t), float(t + 1))
        buckets[f"{high + 1}.0-999.0"] = (float(high + 1), 999.0)
        return buckets

# ── Colors ──
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"

# ── API Fetching ──

def fetch_json(url, timeout=20):
    try:
        req = Request(url, headers={"User-Agent": "weatherbot-backtest/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return None


def fetch_historical_actuals(loc, start_date, end_date):
    """Fetch actual daily max temps from Open-Meteo Archive."""
    url = (f"https://archive-api.open-meteo.com/v1/archive"
           f"?latitude={loc['lat']}&longitude={loc['lon']}"
           f"&daily=temperature_2m_max&temperature_unit={loc['unit']}"
           f"&timezone=auto&start_date={start_date}&end_date={end_date}")
    data = fetch_json(url)
    if not data or "daily" not in data:
        return {}
    dates = data["daily"].get("time", [])
    temps = data["daily"].get("temperature_2m_max", [])
    return {d: t for d, t in zip(dates, temps) if t is not None}


def fetch_historical_ecmwf(loc, start_date, end_date):
    """Fetch ECMWF historical daily max temps."""
    url = (f"https://historical-forecast-api.open-meteo.com/v1/forecast"
           f"?latitude={loc['lat']}&longitude={loc['lon']}"
           f"&daily=temperature_2m_max&temperature_unit={loc['unit']}"
           f"&timezone=auto&start_date={start_date}&end_date={end_date}"
           f"&models=ecmwf_ifs025")
    data = fetch_json(url)
    if not data or "daily" not in data:
        return {}
    dates = data["daily"].get("time", [])
    temps = data["daily"].get("temperature_2m_max", [])
    return {d: t for d, t in zip(dates, temps) if t is not None}


def fetch_historical_gfs(loc, start_date, end_date):
    """Fetch GFS historical daily max temps."""
    url = (f"https://historical-forecast-api.open-meteo.com/v1/forecast"
           f"?latitude={loc['lat']}&longitude={loc['lon']}"
           f"&daily=temperature_2m_max&temperature_unit={loc['unit']}"
           f"&timezone=auto&start_date={start_date}&end_date={end_date}"
           f"&models=gfs_seamless")
    data = fetch_json(url)
    if not data or "daily" not in data:
        return {}
    dates = data["daily"].get("time", [])
    temps = data["daily"].get("temperature_2m_max", [])
    return {d: t for d, t in zip(dates, temps) if t is not None}


# ── Core Engine (copied from bot_v3.py) ──

def compute_consensus(ecmwf_temp, gfs_temp, ensemble_mean=None, ensemble_std=None):
    available = {}
    if ecmwf_temp is not None:
        available["ecmwf"] = ecmwf_temp
    if gfs_temp is not None:
        available["gfs"] = gfs_temp
    if ensemble_mean is not None:
        available["ensemble"] = ensemble_mean

    if not available:
        return None, None, None

    total_weight = sum(WEIGHTS[k] for k in available)
    consensus = sum(available[k] * WEIGHTS[k] / total_weight for k in available)
    consensus = round(consensus, 1)

    temps = list(available.values())
    spread = max(temps) - min(temps) if len(temps) > 1 else 0

    if ensemble_std is not None:
        sigma = ensemble_std
    elif len(temps) > 1:
        sigma = spread / 2.0
    else:
        sigma = 2.0

    return consensus, sigma, spread


def apply_sigma_floor(sigma, horizon):
    floor = SIGMA_FLOORS.get(min(horizon, 4), 3.0)
    return max(sigma, floor)


def monte_carlo_bucket_probs(consensus, sigma, buckets, n_sims=MC_SIMS):
    sims = [random.gauss(consensus, sigma) for _ in range(n_sims)]
    bucket_probs = {}
    for bkey, (t_low, t_high) in buckets.items():
        count = 0
        for s in sims:
            if t_low == -999 or t_low < -900:
                if s <= t_high:
                    count += 1
            elif t_high == 999 or t_high > 900:
                if s >= t_low:
                    count += 1
            else:
                if t_low <= s <= t_high:
                    count += 1
        bucket_probs[bkey] = count / n_sims
    return bucket_probs


def find_actual_bucket(actual_temp, buckets):
    """Find which bucket the actual temperature falls in."""
    for bkey, (t_low, t_high) in buckets.items():
        if t_low == -999 or t_low < -900:
            if actual_temp <= t_high:
                return bkey
        elif t_high == 999 or t_high > 900:
            if actual_temp >= t_low:
                return bkey
        else:
            if t_low <= actual_temp <= t_high:
                return bkey
    return None

# ── Backtest Runner ──

def run_backtest(days=30, city_filter=None):
    print(f"\n{C.BOLD}{C.CYAN}═══ WEATHER BOT BACKTEST — {days} DAYS ═══{C.RESET}\n")

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days + 1)  # +1 buffer
    end = today - timedelta(days=1)  # Don't include today (incomplete)

    start_str = start.isoformat()
    end_str = end.isoformat()

    cities = list(LOCATIONS.keys())
    if city_filter:
        cities = [c for c in cities if c == city_filter]
        if not cities:
            print(f"{C.RED}  Unknown city: {city_filter}{C.RESET}")
            return

    # ── Collect data ──
    print(f"  Fetching data for {len(cities)} cities, {start_str} to {end_str}...")

    all_results = []  # (city, date, horizon, consensus, sigma, bucket_prob_for_actual, actual_temp, predicted_bucket, hit)

    for city_slug in cities:
        loc = LOCATIONS[city_slug]
        unit = loc["unit"]

        # Fetch historical data
        actuals = fetch_historical_actuals(loc, start_str, end_str)
        ecmwf = fetch_historical_ecmwf(loc, start_str, end_str)
        gfs = fetch_historical_gfs(loc, start_str, end_str)

        if not actuals:
            print(f"  {C.YELLOW}⚠ No actual data for {loc['name']}{C.RESET}")
            continue

        fetched_dates = sorted(set(actuals.keys()) & set(ecmwf.keys() if ecmwf else set()) & set(gfs.keys() if gfs else set()))

        if not fetched_dates:
            print(f"  {C.YELLOW}⚠ No overlapping forecast+actual data for {loc['name']}{C.RESET}")
            continue

        city_hits = 0
        city_total = 0

        for date_str in fetched_dates:
            actual = actuals.get(date_str)
            ecmwf_temp = ecmwf.get(date_str) if ecmwf else None
            gfs_temp = gfs.get(date_str) if gfs else None

            if actual is None or (ecmwf_temp is None and gfs_temp is None):
                continue

            # Simulate different horizons
            # Historical forecast API returns the "best available" forecast
            # We approximate D+0 for simplicity (conservative — real D+1/D+2 would be worse)
            horizon = 0  # Historical forecasts are roughly D+0 quality

            consensus, sigma, spread = compute_consensus(ecmwf_temp, gfs_temp)
            if consensus is None:
                continue

            sigma = apply_sigma_floor(sigma, horizon)

            # Generate buckets
            buckets = generate_buckets(unit, actual)

            # Run MC
            random.seed(hash((city_slug, date_str)))  # Reproducible
            probs = monte_carlo_bucket_probs(consensus, sigma, buckets)

            # Find which bucket the actual temp landed in
            actual_bucket = find_actual_bucket(actual, buckets)

            # What probability did the model assign to the actual bucket?
            actual_prob = probs.get(actual_bucket, 0) if actual_bucket else 0

            # What was the model's top predicted bucket?
            top_bucket = max(probs, key=probs.get) if probs else None
            top_prob = probs.get(top_bucket, 0) if top_bucket else 0

            hit = (top_bucket == actual_bucket) if top_bucket and actual_bucket else False

            error = abs(actual - consensus)

            all_results.append({
                "city": city_slug,
                "city_name": loc["name"],
                "date": date_str,
                "horizon": horizon,
                "consensus": consensus,
                "actual": actual,
                "error": error,
                "sigma": sigma,
                "spread": spread,
                "actual_bucket": actual_bucket,
                "actual_prob": actual_prob,
                "top_bucket": top_bucket,
                "top_prob": top_prob,
                "hit": hit,
                "unit": unit,
            })

            city_total += 1
            if hit:
                city_hits += 1

        hit_rate = city_hits / city_total * 100 if city_total > 0 else 0
        color = C.GREEN if hit_rate > 30 else C.YELLOW if hit_rate > 15 else C.RED
        print(f"  {loc['name']:<12} {city_total:>3} days  |  Top-bucket hit rate: {color}{hit_rate:.0f}%{C.RESET}  |  Avg error: {statistics.mean([r['error'] for r in all_results if r['city'] == city_slug]):.1f}°")

    if not all_results:
        print(f"\n{C.RED}  No data to analyze.{C.RESET}")
        return

    # ═══ CALIBRATION CURVE ═══
    print(f"\n{C.BOLD}{C.CYAN}── Calibration Curve ──{C.RESET}\n")
    print(f"  For each probability bin: what fraction of the time did the model's")
    print(f"  predicted bucket actually contain the observed temperature?\n")

    # Bin ALL bucket probabilities (not just the top one)
    # For each result, we have probs for every bucket — check if each bucket hit
    bins = defaultdict(lambda: {"count": 0, "hits": 0})

    for r in all_results:
        # The actual_prob is the probability the model assigned to the bucket
        # that actually contained the observed temperature
        prob = r["actual_prob"]
        bin_idx = min(int(prob * 10), 9)
        bin_key = f"{bin_idx * 10:>2}-{(bin_idx + 1) * 10}%"
        bins[bin_key]["count"] += 1
        bins[bin_key]["hits"] += 1  # By definition, the actual bucket was hit

    # Better approach: for each bucket probability prediction, did it hit?
    # We need to look at ALL buckets, not just the actual one
    cal_bins = defaultdict(lambda: {"predictions": 0, "hits": 0, "probs": []})

    for r in all_results:
        # Regenerate buckets and probs for this result
        random.seed(hash((r["city"], r["date"])))
        buckets = generate_buckets(r["unit"], r["actual"])
        probs = monte_carlo_bucket_probs(r["consensus"], r["sigma"], buckets)

        for bkey, prob in probs.items():
            if prob < 0.01:  # Skip near-zero predictions
                continue

            hit = (bkey == r["actual_bucket"])
            bin_idx = min(int(prob * 10), 9)
            bin_key = f"{bin_idx * 10:>2}-{(bin_idx + 1) * 10}%"
            cal_bins[bin_key]["predictions"] += 1
            cal_bins[bin_key]["hits"] += 1 if hit else 0
            cal_bins[bin_key]["probs"].append(prob)

    print(f"  {'Bin':<10} {'Predictions':>12} {'Hits':>6} {'Actual%':>8} {'Expected%':>10} {'Status'}")
    print(f"  {'─' * 65}")

    for i in range(10):
        bin_key = f"{i * 10:>2}-{(i + 1) * 10}%"
        d = cal_bins[bin_key]
        if d["predictions"] == 0:
            continue

        actual_rate = d["hits"] / d["predictions"]
        expected_rate = statistics.mean(d["probs"])

        ratio = actual_rate / expected_rate if expected_rate > 0 else 0
        if d["predictions"] < 10:
            status = f"{C.DIM}(few samples){C.RESET}"
        elif ratio < 0.6:
            status = f"{C.RED}OVERCONFIDENT{C.RESET}"
        elif ratio > 1.5:
            status = f"{C.GREEN}UNDERCONFIDENT{C.RESET}"
        else:
            status = f"{C.GREEN}CALIBRATED{C.RESET}"

        print(f"  {bin_key:<10} {d['predictions']:>12} {d['hits']:>6} {actual_rate:>7.1%} {expected_rate:>9.1%}   {status}")

    # ═══ ERROR ANALYSIS ═══
    print(f"\n{C.BOLD}{C.CYAN}── Forecast Error Analysis ──{C.RESET}\n")

    errors = [r["error"] for r in all_results]
    print(f"  Total predictions: {len(all_results)}")
    print(f"  Mean absolute error: {statistics.mean(errors):.2f}°")
    print(f"  Median error: {statistics.median(errors):.2f}°")
    print(f"  Std dev of error: {statistics.stdev(errors):.2f}°")
    print(f"  Max error: {max(errors):.1f}°")
    print(f"  Pct within 1°: {sum(1 for e in errors if e <= 1) / len(errors):.0%}")
    print(f"  Pct within 2°: {sum(1 for e in errors if e <= 2) / len(errors):.0%}")
    print(f"  Pct within 3°: {sum(1 for e in errors if e <= 3) / len(errors):.0%}")

    # ═══ ERROR BY CITY ═══
    print(f"\n{C.BOLD}{C.CYAN}── Error By City ──{C.RESET}\n")
    print(f"  {'City':<12} {'Days':>5} {'Mean Err':>10} {'Std Err':>9} {'Top Hit%':>9} {'Worst':>7}")
    print(f"  {'─' * 55}")

    city_groups = defaultdict(list)
    for r in all_results:
        city_groups[r["city"]].append(r)

    for city in sorted(city_groups.keys()):
        results = city_groups[city]
        errs = [r["error"] for r in results]
        hits = sum(1 for r in results if r["hit"])
        hit_rate = hits / len(results) * 100
        mean_err = statistics.mean(errs)
        std_err = statistics.stdev(errs) if len(errs) > 1 else 0
        worst = max(errs)

        name = LOCATIONS[city]["name"]
        color = C.GREEN if mean_err < 2 else C.YELLOW if mean_err < 3 else C.RED
        print(f"  {name:<12} {len(results):>5} {color}{mean_err:>9.2f}°{C.RESET} {std_err:>8.2f}° {hit_rate:>8.0f}% {worst:>6.1f}°")

    # ═══ SIGMA RECOMMENDATION ═══
    print(f"\n{C.BOLD}{C.CYAN}── Sigma Floor Recommendations ──{C.RESET}\n")
    print(f"  Based on observed forecast errors (using historical forecast API ≈ D+0 quality):\n")

    overall_std = statistics.stdev(errors) if len(errors) > 1 else 2.0
    overall_mean = statistics.mean(errors)

    # The historical forecast API gives ~D+0 quality forecasts
    # Real D+1, D+2 would have larger errors
    print(f"  {'Horizon':<10} {'Current':>9} {'Recommended':>12} {'Reasoning'}")
    print(f"  {'─' * 60}")

    # D+0: use observed error directly
    d0_rec = round(max(overall_std, overall_mean * 0.8), 1)
    d0_status = f"{C.RED}RAISE{C.RESET}" if d0_rec > SIGMA_FLOORS[0] else f"{C.GREEN}OK{C.RESET}"
    print(f"  D+0       {SIGMA_FLOORS[0]:>8.1f}° {d0_rec:>11.1f}°  Observed error std={overall_std:.2f}° {d0_status}")

    # D+1: typically 1.2-1.5x D+0
    d1_rec = round(d0_rec * 1.3, 1)
    d1_status = f"{C.RED}RAISE{C.RESET}" if d1_rec > SIGMA_FLOORS[1] else f"{C.GREEN}OK{C.RESET}"
    print(f"  D+1       {SIGMA_FLOORS[1]:>8.1f}° {d1_rec:>11.1f}°  ~1.3x D+0 {d1_status}")

    # D+2: typically 1.5-2x D+0
    d2_rec = round(d0_rec * 1.7, 1)
    d2_status = f"{C.RED}RAISE{C.RESET}" if d2_rec > SIGMA_FLOORS[2] else f"{C.GREEN}OK{C.RESET}"
    print(f"  D+2       {SIGMA_FLOORS[2]:>8.1f}° {d2_rec:>11.1f}°  ~1.7x D+0 {d2_status}")

    # D+3: typically 2-2.5x D+0
    d3_rec = round(d0_rec * 2.2, 1)
    d3_status = f"{C.RED}RAISE{C.RESET}" if d3_rec > SIGMA_FLOORS[3] else f"{C.GREEN}OK{C.RESET}"
    print(f"  D+3       {SIGMA_FLOORS[3]:>8.1f}° {d3_rec:>11.1f}°  ~2.2x D+0 {d3_status}")

    d4_rec = round(d0_rec * 2.5, 1)
    d4_status = f"{C.RED}RAISE{C.RESET}" if d4_rec > SIGMA_FLOORS[4] else f"{C.GREEN}OK{C.RESET}"
    print(f"  D+4+      {SIGMA_FLOORS[4]:>8.1f}° {d4_rec:>11.1f}°  ~2.5x D+0 {d4_status}")

    # ═══ SUMMARY ═══
    overall_hit = sum(1 for r in all_results if r["hit"]) / len(all_results) * 100
    print(f"\n{C.BOLD}{C.CYAN}── Summary ──{C.RESET}\n")
    print(f"  {len(all_results)} predictions across {len(city_groups)} cities, {days} days")
    print(f"  Top-bucket hit rate: {overall_hit:.0f}% (model's #1 prediction was correct)")
    print(f"  Mean forecast error: {overall_mean:.2f}° (consensus vs actual)")
    print(f"  Error std dev: {overall_std:.2f}° (key input for sigma floors)")

    worst_cities = sorted(city_groups.items(), key=lambda x: statistics.mean([r["error"] for r in x[1]]), reverse=True)[:3]
    print(f"\n  Worst cities by error:")
    for city, results in worst_cities:
        mean_err = statistics.mean([r["error"] for r in results])
        print(f"    {C.RED}{LOCATIONS[city]['name']}: {mean_err:.2f}° avg error{C.RESET}")

    best_cities = sorted(city_groups.items(), key=lambda x: statistics.mean([r["error"] for r in x[1]]))[:3]
    print(f"\n  Best cities by error:")
    for city, results in best_cities:
        mean_err = statistics.mean([r["error"] for r in results])
        print(f"    {C.GREEN}{LOCATIONS[city]['name']}: {mean_err:.2f}° avg error{C.RESET}")

    print()


def main():
    parser = argparse.ArgumentParser(description="Weather Bot Backtest")
    parser.add_argument("--days", type=int, default=30, help="Number of days to backtest (default: 30)")
    parser.add_argument("--city", type=str, default=None, help="Single city to test (e.g., seoul)")
    args = parser.parse_args()

    run_backtest(days=args.days, city_filter=args.city)


if __name__ == "__main__":
    main()
