#!/usr/bin/env python3
"""
Weather Bot Analytics — Calibration, Sigma Tuning, P&L Breakdown
Usage:
    python analytics.py calibration   # Model probability calibration check
    python analytics.py sigma         # Empirical sigma analysis vs floors
    python analytics.py pnl           # P&L by confidence tier and horizon
    python analytics.py all           # Run all three
"""

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

LOG_FILE = Path("logs/weather-signals.ndjson")

# Current sigma floors from bot_v3.py
SIGMA_FLOORS = {0: 3.0, 1: 1.7, 2: 2.2, 3: 2.9, 4: 3.2}

# Locations from bot_v3.py
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

# ── Data Loading ──

def load_signals():
    """Load all signals from NDJSON log."""
    if not LOG_FILE.exists():
        print(f"{C.RED}No signal log found at {LOG_FILE}{C.RESET}")
        sys.exit(1)

    signals = []
    for line in LOG_FILE.read_text().strip().split("\n"):
        if line.strip():
            try:
                signals.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return signals


def derive_horizon(signal):
    """Derive forecast horizon (days_out) from signal data."""
    # Use stored horizon if available
    h = signal.get("horizon")
    if h is not None:
        return h

    # Derive from timestamp and date
    ts = signal.get("timestamp", "")
    date = signal.get("date", "")
    if not ts or not date:
        return None

    try:
        signal_dt = datetime.fromisoformat(ts).date()
        target_dt = datetime.strptime(date, "%Y-%m-%d").date()
        return (target_dt - signal_dt).days
    except Exception:
        return None


def parse_bucket(bucket_str):
    """Parse bucket string like '12.5-13.5' into (low, high)."""
    if not bucket_str:
        return None, None
    try:
        parts = bucket_str.split("-")
        if len(parts) == 2:
            return float(parts[0]), float(parts[1])
        elif len(parts) == 3 and parts[0] == "":
            # Negative: e.g. "-999.0-14.0"
            return float("-" + parts[1]), float(parts[2])
    except ValueError:
        pass
    return None, None


def temp_in_bucket(temp, bucket_str):
    """Check if a temperature falls in a bucket range."""
    low, high = parse_bucket(bucket_str)
    if low is None:
        return None
    if low == -999:
        return temp <= high
    elif high == 999:
        return temp >= low
    else:
        return low <= temp <= high

# ── Calibration Backtest ──

def run_calibration(signals):
    """Compare predicted probabilities vs actual hit rates."""
    print(f"\n{C.BOLD}{C.CYAN}═══ CALIBRATION BACKTEST ═══{C.RESET}\n")

    # Get all edge/ladder signals and resolution signals
    edge_signals = [s for s in signals if s.get("type") in ("edge", "ladder")]
    resolutions = {s["market_id"]: s for s in signals if s.get("type") == "resolution"}

    if not resolutions:
        print(f"{C.YELLOW}No resolved markets yet. Calibration requires resolution data.")
        print(f"The bot has {len(edge_signals)} signals waiting for resolution.{C.RESET}")
        return

    # Match edge signals to resolutions
    # Use the LAST edge signal per market_id (closest to entry)
    latest_edge = {}
    for s in edge_signals:
        mid = s.get("market_id")
        if mid:
            latest_edge[mid] = s

    # Build calibration data: predicted probability vs actual outcome
    bins = defaultdict(lambda: {"count": 0, "hits": 0, "probs": []})

    matched = 0
    for mid, resolution in resolutions.items():
        edge = latest_edge.get(mid)
        if not edge:
            continue

        actual_temp = resolution.get("actual_temp")
        bucket = edge.get("bucket") or resolution.get("bucket")
        model_prob = edge.get("model_probability", 0)

        if actual_temp is None or not bucket:
            continue

        hit = temp_in_bucket(actual_temp, bucket)
        if hit is None:
            continue

        matched += 1

        # Bin by 10% intervals
        bin_idx = min(int(model_prob * 10), 9)  # 0-9
        bin_key = f"{bin_idx*10}-{(bin_idx+1)*10}%"
        bins[bin_key]["count"] += 1
        bins[bin_key]["hits"] += 1 if hit else 0
        bins[bin_key]["probs"].append(model_prob)

    print(f"  Matched {matched} resolved signals to edge predictions\n")

    if matched == 0:
        print(f"{C.YELLOW}  No matched predictions with actual temperatures yet.{C.RESET}")
        return

    # Display calibration table
    print(f"  {'Bin':<10} {'Signals':>8} {'Hits':>6} {'Actual':>8} {'Predicted':>10} {'Status'}")
    print(f"  {'─'*60}")

    total_signals = 0
    total_hits = 0
    for i in range(10):
        bin_key = f"{i*10}-{(i+1)*10}%"
        data = bins[bin_key]
        count = data["count"]
        hits = data["hits"]
        total_signals += count
        total_hits += hits

        if count == 0:
            continue

        actual_rate = hits / count
        predicted_rate = statistics.mean(data["probs"]) if data["probs"] else 0

        # Status
        if count < 3:
            status = f"{C.DIM}(few samples){C.RESET}"
        elif actual_rate < predicted_rate * 0.7:
            status = f"{C.RED}OVERCONFIDENT{C.RESET}"
        elif actual_rate > predicted_rate * 1.3:
            status = f"{C.GREEN}UNDERCONFIDENT{C.RESET}"
        else:
            status = f"{C.GREEN}OK{C.RESET}"

        actual_str = f"{actual_rate:>7.1%}"
        pred_str = f"{predicted_rate:>9.1%}"
        print(f"  {bin_key:<10} {count:>8} {hits:>6} {actual_str} {pred_str}   {status}")

    print(f"  {'─'*60}")
    overall = total_hits / total_signals if total_signals > 0 else 0
    print(f"  {'TOTAL':<10} {total_signals:>8} {total_hits:>6} {overall:>7.1%}")

    # Overall assessment
    print()
    if total_signals < 10:
        print(f"  {C.YELLOW}⚠ Only {total_signals} samples — need 30+ for reliable calibration{C.RESET}")
    elif overall > 0.5:
        print(f"  {C.GREEN}✅ Model appears well-calibrated (or underconfident — good){C.RESET}")
    else:
        print(f"  {C.RED}⚠ Model may be overconfident — review sigma floors{C.RESET}")

# ── Sigma Analysis ──

def fetch_json(url, timeout=15):
    """Simple HTTP GET returning parsed JSON."""
    try:
        req = Request(url, headers={"User-Agent": "weatherbot-analytics/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (URLError, json.JSONDecodeError, Exception) as e:
        return None


def run_sigma(signals):
    """Analyze forecast errors vs sigma floors."""
    print(f"\n{C.BOLD}{C.CYAN}═══ SIGMA FLOOR ANALYSIS ═══{C.RESET}\n")

    # Part 1: From resolved trades
    resolutions = [s for s in signals if s.get("type") == "resolution"]

    errors_by_horizon = defaultdict(list)

    print(f"  {C.BOLD}Part 1: Resolved Trade Errors{C.RESET}")
    print(f"  {len(resolutions)} resolved trades found\n")

    for r in resolutions:
        actual = r.get("actual_temp")
        consensus = r.get("consensus_temp")
        if actual is None or consensus is None or consensus == 0:
            continue

        horizon = derive_horizon(r)
        if horizon is None:
            horizon = 1  # Default assumption

        error = abs(actual - consensus)
        errors_by_horizon[horizon].append(error)

    if errors_by_horizon:
        print(f"  {'Horizon':<10} {'Samples':>8} {'Mean Err':>10} {'Std Dev':>10} {'Current σ':>10} {'Status'}")
        print(f"  {'─'*65}")

        for h in sorted(errors_by_horizon.keys()):
            errs = errors_by_horizon[h]
            n = len(errs)
            mean_err = statistics.mean(errs)
            std_err = statistics.stdev(errs) if n > 1 else mean_err
            current = SIGMA_FLOORS.get(h, 3.0)

            if std_err > current * 1.3:
                status = f"{C.RED}TOO TIGHT (raise to {std_err:.1f}°){C.RESET}"
            elif std_err < current * 0.7:
                status = f"{C.YELLOW}CONSERVATIVE{C.RESET}"
            else:
                status = f"{C.GREEN}OK{C.RESET}"

            print(f"  D+{h:<8} {n:>8} {mean_err:>9.2f}° {std_err:>9.2f}° {current:>9.1f}°  {status}")
    else:
        print(f"  {C.YELLOW}No resolved trades with consensus + actual temps yet.{C.RESET}")

    # Part 2: Historical backtest from Open-Meteo
    print(f"\n  {C.BOLD}Part 2: Historical Forecast Error (last 14 days){C.RESET}\n")

    # Fetch historical actuals and forecasts for a sample of cities
    sample_cities = ["london", "tokyo", "seoul", "nyc", "chicago", "miami"]
    today = datetime.now(timezone.utc).date()

    hist_errors = defaultdict(list)
    cities_processed = 0

    for city_slug in sample_cities:
        loc = LOCATIONS[city_slug]
        # Fetch last 14 days of actual temps
        end_date = today.isoformat()
        start_date = (today.__class__(today.year, today.month, today.day - 14 if today.day > 14 else 1)).isoformat()

        try:
            from datetime import timedelta
            start_dt = today - timedelta(days=14)
            start_date = start_dt.isoformat()
        except Exception:
            continue

        # Fetch actuals from archive
        archive_url = (
            f"https://archive-api.open-meteo.com/v1/archive"
            f"?latitude={loc['lat']}&longitude={loc['lon']}"
            f"&daily=temperature_2m_max&temperature_unit={loc['unit']}"
            f"&timezone=auto&start_date={start_date}&end_date={end_date}"
        )
        archive = fetch_json(archive_url)
        if not archive or "daily" not in archive:
            continue

        actual_dates = archive["daily"].get("time", [])
        actual_temps = archive["daily"].get("temperature_2m_max", [])
        actuals = {d: t for d, t in zip(actual_dates, actual_temps) if t is not None}

        # Fetch ECMWF forecasts for the same period (what was predicted)
        # Use the forecast API for each day as if we were forecasting from days prior
        # Simplified: compare today's forecast model output vs archive for recent days
        ecmwf_url = (
            f"https://api.open-meteo.com/v1/ecmwf"
            f"?latitude={loc['lat']}&longitude={loc['lon']}"
            f"&daily=temperature_2m_max,temperature_2m_min"
            f"&temperature_unit={loc['unit']}&timezone=auto&forecast_days=5"
        )
        ecmwf = fetch_json(ecmwf_url)

        gfs_url = (
            f"https://api.open-meteo.com/v1/gfs"
            f"?latitude={loc['lat']}&longitude={loc['lon']}"
            f"&daily=temperature_2m_max,temperature_2m_min"
            f"&temperature_unit={loc['unit']}&timezone=auto&forecast_days=5"
        )
        gfs = fetch_json(gfs_url)

        if not ecmwf or not gfs:
            continue

        # Compare forecast vs actual for overlapping dates
        ecmwf_dates = ecmwf.get("daily", {}).get("time", [])
        ecmwf_temps = ecmwf.get("daily", {}).get("temperature_2m_max", [])
        ecmwf_map = {d: t for d, t in zip(ecmwf_dates, ecmwf_temps) if t is not None}

        gfs_dates = gfs.get("daily", {}).get("time", [])
        gfs_temps = gfs.get("daily", {}).get("temperature_2m_max", [])
        gfs_map = {d: t for d, t in zip(gfs_dates, gfs_temps) if t is not None}

        for date_str in actuals:
            actual = actuals[date_str]
            forecasts = []
            if date_str in ecmwf_map:
                forecasts.append(ecmwf_map[date_str])
            if date_str in gfs_map:
                forecasts.append(gfs_map[date_str])

            if not forecasts:
                continue

            consensus = statistics.mean(forecasts)
            error = abs(actual - consensus)

            # Estimate horizon: how many days ahead was this forecast?
            try:
                target = datetime.strptime(date_str, "%Y-%m-%d").date()
                horizon = (target - today).days
                if horizon < 0:
                    # It's a past date — the forecast horizon when it was made
                    # The current forecast is for "today + offset", so past dates
                    # were forecasted with horizon = offset from when forecast was issued
                    continue  # Skip past dates for current forecast comparison
            except Exception:
                continue

            hist_errors[horizon].append((city_slug, error))

        cities_processed += 1

    if hist_errors:
        print(f"  Analyzed {cities_processed} cities with current forecast vs recent actuals\n")
        print(f"  {'Horizon':<10} {'Samples':>8} {'Mean Err':>10} {'Max Err':>9} {'Current σ':>10} {'Suggested σ'}")
        print(f"  {'─'*70}")

        for h in sorted(hist_errors.keys()):
            errs = [e[1] for e in hist_errors[h]]
            n = len(errs)
            if n == 0:
                continue
            mean_err = statistics.mean(errs)
            max_err = max(errs)
            std_err = statistics.stdev(errs) if n > 1 else mean_err
            current = SIGMA_FLOORS.get(h, 3.0)
            suggested = round(max(std_err, mean_err * 1.2), 1)

            print(f"  D+{h:<8} {n:>8} {mean_err:>9.2f}° {max_err:>8.1f}° {current:>9.1f}°  {suggested:>9.1f}°")
    else:
        print(f"  {C.YELLOW}Could not fetch historical data. Check network.{C.RESET}")

    # Summary
    print(f"\n  {C.BOLD}Current Sigma Floors:{C.RESET}")
    for h in sorted(SIGMA_FLOORS.keys()):
        print(f"    D+{h}: {SIGMA_FLOORS[h]}°")

    if errors_by_horizon:
        print(f"\n  {C.BOLD}Recommendation:{C.RESET}")
        for h in sorted(errors_by_horizon.keys()):
            errs = errors_by_horizon[h]
            observed = statistics.stdev(errs) if len(errs) > 1 else statistics.mean(errs)
            current = SIGMA_FLOORS.get(h, 3.0)
            if observed > current * 1.3:
                print(f"    {C.RED}D+{h}: Raise from {current}° to {observed:.1f}° (observed errors are larger){C.RESET}")
            else:
                print(f"    {C.GREEN}D+{h}: {current}° looks reasonable{C.RESET}")

# ── P&L Analysis ──

def run_pnl(signals):
    """Break down P&L by confidence tier and horizon."""
    print(f"\n{C.BOLD}{C.CYAN}═══ P&L BREAKDOWN ═══{C.RESET}\n")

    # Collect all exit signals (resolution, stop_loss, take_profit)
    exits = [s for s in signals if s.get("type") in ("resolution", "stop_loss", "take_profit")]

    if not exits:
        print(f"{C.YELLOW}  No completed trades yet. P&L analysis requires resolved or exited positions.{C.RESET}")
        return

    # Match exits to their entry edge signals for confidence/horizon data
    edge_signals = {}
    for s in signals:
        if s.get("type") in ("edge", "ladder"):
            mid = s.get("market_id")
            if mid:
                edge_signals[mid] = s  # Keep latest

    # ── By Confidence Tier ──
    print(f"  {C.BOLD}By Confidence Tier{C.RESET}\n")

    tier_data = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "pnls": []})

    for ex in exits:
        mid = ex.get("market_id")
        edge = edge_signals.get(mid, {})
        conf = edge.get("confidence") or ex.get("confidence") or "UNKNOWN"
        pnl = ex.get("pnl", 0)

        tier_data[conf]["trades"] += 1
        tier_data[conf]["pnl"] += pnl
        tier_data[conf]["pnls"].append(pnl)
        if pnl >= 0:
            tier_data[conf]["wins"] += 1
        else:
            tier_data[conf]["losses"] += 1

    print(f"  {'Tier':<10} {'Trades':>7} {'W':>4} {'L':>4} {'Win%':>6} {'Total P&L':>11} {'Avg P&L':>9}")
    print(f"  {'─'*55}")

    for tier in ["HIGH", "MEDIUM", "LOW", "UNKNOWN"]:
        d = tier_data[tier]
        if d["trades"] == 0:
            continue
        win_rate = d["wins"] / d["trades"] * 100
        avg_pnl = d["pnl"] / d["trades"]

        pnl_color = C.GREEN if d["pnl"] >= 0 else C.RED
        tier_color = {"HIGH": C.GREEN, "MEDIUM": C.YELLOW, "LOW": C.RED}.get(tier, C.WHITE)

        print(f"  {tier_color}{tier:<10}{C.RESET} {d['trades']:>7} {d['wins']:>4} {d['losses']:>4} "
              f"{win_rate:>5.0f}% {pnl_color}${d['pnl']:>+9.2f}{C.RESET} ${avg_pnl:>+7.2f}")

    total_pnl = sum(d["pnl"] for d in tier_data.values())
    total_trades = sum(d["trades"] for d in tier_data.values())
    print(f"  {'─'*55}")
    pnl_color = C.GREEN if total_pnl >= 0 else C.RED
    print(f"  {'TOTAL':<10} {total_trades:>7} {' ':>4} {' ':>4} {' ':>6} {pnl_color}${total_pnl:>+9.2f}{C.RESET}")

    # ── By Horizon ──
    print(f"\n  {C.BOLD}By Forecast Horizon{C.RESET}\n")

    horizon_data = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})

    for ex in exits:
        mid = ex.get("market_id")
        edge = edge_signals.get(mid, {})
        h = edge.get("horizon") if edge else None
        if h is None:
            h = derive_horizon(ex)
        if h is None:
            h = -1  # Unknown

        label = f"D+{h}" if h >= 0 else "D+?"
        horizon_data[label]["trades"] += 1
        horizon_data[label]["pnl"] += ex.get("pnl", 0)
        if ex.get("pnl", 0) >= 0:
            horizon_data[label]["wins"] += 1
        else:
            horizon_data[label]["losses"] += 1

    print(f"  {'Horizon':<10} {'Trades':>7} {'W':>4} {'L':>4} {'Win%':>6} {'Total P&L':>11} {'Avg P&L':>9}")
    print(f"  {'─'*55}")

    for label in sorted(horizon_data.keys()):
        d = horizon_data[label]
        if d["trades"] == 0:
            continue
        win_rate = d["wins"] / d["trades"] * 100
        avg_pnl = d["pnl"] / d["trades"]
        pnl_color = C.GREEN if d["pnl"] >= 0 else C.RED

        print(f"  {label:<10} {d['trades']:>7} {d['wins']:>4} {d['losses']:>4} "
              f"{win_rate:>5.0f}% {pnl_color}${d['pnl']:>+9.2f}{C.RESET} ${avg_pnl:>+7.2f}")

    # ── By Exit Type ──
    print(f"\n  {C.BOLD}By Exit Type{C.RESET}\n")

    type_data = defaultdict(lambda: {"trades": 0, "pnl": 0.0})
    for ex in exits:
        t = ex.get("type", "?")
        type_data[t]["trades"] += 1
        type_data[t]["pnl"] += ex.get("pnl", 0)

    print(f"  {'Type':<15} {'Trades':>7} {'Total P&L':>11} {'Avg P&L':>9}")
    print(f"  {'─'*45}")

    for t in ["resolution", "take_profit", "stop_loss"]:
        d = type_data[t]
        if d["trades"] == 0:
            continue
        avg = d["pnl"] / d["trades"]
        pnl_color = C.GREEN if d["pnl"] >= 0 else C.RED
        label = {"resolution": "Resolution", "take_profit": "Take Profit", "stop_loss": "Stop Loss"}.get(t, t)
        print(f"  {label:<15} {d['trades']:>7} {pnl_color}${d['pnl']:>+9.2f}{C.RESET} ${avg:>+7.2f}")

    # Insights
    print(f"\n  {C.BOLD}Insights:{C.RESET}")

    if type_data["stop_loss"]["trades"] > 0:
        sl_pnl = type_data["stop_loss"]["pnl"]
        sl_n = type_data["stop_loss"]["trades"]
        print(f"  {C.RED}⚠ Stop-losses cost ${abs(sl_pnl):.2f} across {sl_n} trades "
              f"(avg ${sl_pnl/sl_n:.2f}/trade){C.RESET}")
        print(f"    Consider: stop-losses removed in latest version")

    best_tier = max(tier_data.items(), key=lambda x: x[1]["pnl"] / max(x[1]["trades"], 1))
    if best_tier[1]["trades"] > 0:
        print(f"  Best tier: {best_tier[0]} (avg ${best_tier[1]['pnl']/best_tier[1]['trades']:+.2f}/trade)")

    worst_tier = min(tier_data.items(), key=lambda x: x[1]["pnl"] / max(x[1]["trades"], 1))
    if worst_tier[1]["trades"] > 0 and worst_tier[0] != best_tier[0]:
        print(f"  Worst tier: {worst_tier[0]} (avg ${worst_tier[1]['pnl']/worst_tier[1]['trades']:+.2f}/trade)")

# ── Main ──

def main():
    parser = argparse.ArgumentParser(description="Weather Bot Analytics")
    parser.add_argument("command", choices=["calibration", "sigma", "pnl", "all"],
                        help="Analysis to run")
    args = parser.parse_args()

    signals = load_signals()
    print(f"\n{C.DIM}  Loaded {len(signals)} signals from {LOG_FILE}{C.RESET}")

    if args.command in ("calibration", "all"):
        run_calibration(signals)

    if args.command in ("sigma", "all"):
        run_sigma(signals)

    if args.command in ("pnl", "all"):
        run_pnl(signals)

    print()


if __name__ == "__main__":
    main()
