#!/usr/bin/env python3
"""
Weather Trading Bot v3 — Polymarket
Multi-model forecasts + Monte Carlo + Temperature Laddering + Kelly Sizing

Usage:
    python bot_v3.py              # Scan and show signals (paper mode)
    python bot_v3.py --live       # Execute paper trades against virtual balance
    python bot_v3.py --positions  # Show open positions
    python bot_v3.py --reset      # Reset simulation
"""

import re
import os
import sys
import json
import math
import random
import argparse
import statistics
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# =============================================================================
# CONFIG
# =============================================================================

with open("config.json") as f:
    _cfg = json.load(f)

BANKROLL        = _cfg.get("balance", 10000.0)
MIN_EDGE        = _cfg.get("min_edge", 0.05)        # 5% min edge for ladder rungs
SINGLE_MIN_EDGE = _cfg.get("single_min_edge", 0.15)  # min edge for any rung to trigger
MAX_PRICE       = _cfg.get("max_price", 0.45)        # don't enter if market already overpriced
MIN_ENTRY_PRICE = _cfg.get("min_entry_price", 0.05)  # skip penny-priced longshots
MIN_HOURS       = _cfg.get("min_hours", 2.0)
MAX_HOURS       = _cfg.get("max_hours", 72.0)
KELLY_FRACTION  = _cfg.get("kelly_fraction", 0.25)
LADDER_BUDGET   = _cfg.get("ladder_budget", 0.25)    # 25% of bankroll per ladder
MC_SIMS         = _cfg.get("mc_sims", 10000)
MAX_LADDER_RUNGS = _cfg.get("max_ladder_rungs", 5)
SCAN_INTERVAL   = _cfg.get("scan_interval", 3600)

# Forecast weights: ECMWF 30%, GFS 20%, NWS 15%, ICON 15%, Ensemble mean 20%
WEIGHTS = {"ecmwf": 0.30, "gfs": 0.20, "nws": 0.15, "icon": 0.15, "ensemble": 0.20}

# Per-city bias correction (forecast - actual). Subtract from model before consensus.
# Computed via: python backtest.py --compute-biases (2026-04-13, 30-day window)
CITY_BIASES = {
    "nyc": {"ecmwf": -1.43, "icon": -0.7},
    "chicago": {"ecmwf": -0.64, "gfs": 2.03, "icon": 0.51},
    "miami": {"ecmwf": -1.55, "gfs": 0.58, "icon": 1.18},
    "dallas": {"ecmwf": -0.53, "gfs": 1.07, "icon": 1.4},
    "seattle": {"ecmwf": -1.02, "icon": -0.48},
    "atlanta": {"ecmwf": -0.56, "gfs": 1.33},
    "denver": {"gfs": -1.08, "icon": -0.77},
    "phoenix": {"icon": 0.53},
    "london": {"icon": 0.55},
    "tokyo": {"ecmwf": 0.58, "icon": 1.14},
    "paris": {"gfs": 0.41, "icon": 0.76},
}

SIM_FILE = "simulation_v3.json"
LOG_DIR  = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "weather-signals.ndjson"

# Sigma floors by forecast horizon (days out)
SIGMA_FLOORS = {0: 3.0, 1: 1.7, 2: 2.2, 3: 2.9, 4: 3.2}

# Thesis-break exit thresholds.
# Triggered during the main scan when we have a fresh model_prob for an open
# position's market. Exits at the current market price (a partial recovery beats
# riding to a near-certain loss).
THESIS_SOURCE_DROP        = 2     # exit if entry-source count drops by ≥ this
THESIS_MIN_ENTRY_SOURCES  = 3     # only apply source rule if entry had ≥ this
THESIS_MODEL_PROB_DROP    = 0.10  # exit if current model_prob falls ≥ this from entry
THESIS_MIN_EDGE           = 0.15  # exit if current edge (model_prob - market_price) drops below this

# =============================================================================
# LOCATIONS — Airport stations matching Polymarket resolution
# =============================================================================

LOCATIONS = {
    # US stations (NWS + Open-Meteo)
    "nyc":     {"lat": 40.7772, "lon": -73.8726, "name": "New York City", "station": "KLGA", "unit": "fahrenheit", "nws": "OKX/37,39"},
    "chicago": {"lat": 41.9742, "lon": -87.9073, "name": "Chicago",      "station": "KORD", "unit": "fahrenheit", "nws": "LOT/66,77"},
    "miami":   {"lat": 25.7959, "lon": -80.2870, "name": "Miami",        "station": "KMIA", "unit": "fahrenheit", "nws": "MFL/106,51"},
    "dallas":  {"lat": 32.8471, "lon": -96.8518, "name": "Dallas",       "station": "KDAL", "unit": "fahrenheit", "nws": "FWD/87,107"},
    "seattle": {"lat": 47.4502, "lon":-122.3088, "name": "Seattle",      "station": "KSEA", "unit": "fahrenheit", "nws": "SEW/124,61"},
    "atlanta": {"lat": 33.6407, "lon": -84.4277, "name": "Atlanta",      "station": "KATL", "unit": "fahrenheit", "nws": "FFC/50,82"},
    "denver":  {"lat": 39.8561, "lon":-104.6737, "name": "Denver",       "station": "KDEN", "unit": "fahrenheit", "nws": "BOU/74,66"},
    "phoenix": {"lat": 33.4373, "lon":-112.0078, "name": "Phoenix",      "station": "KPHX", "unit": "fahrenheit", "nws": "PSR/161,57"},
    # International stations (Open-Meteo only)
    "london":  {"lat": 51.4700, "lon":  -0.4543, "name": "London",       "station": "EGLL", "unit": "celsius"},
    "tokyo":   {"lat": 35.5533, "lon": 139.7811, "name": "Tokyo",        "station": "RJTT", "unit": "celsius"},
    "seoul":   {"lat": 37.4602, "lon": 126.4407, "name": "Seoul",        "station": "RKSI", "unit": "celsius"},
    "paris":   {"lat": 49.0097, "lon":   2.5478, "name": "Paris",        "station": "LFPG", "unit": "celsius"},
}

MONTHS = ["january","february","march","april","may","june",
          "july","august","september","october","november","december"]

ACTIVE_LOCATIONS = _cfg.get("locations", ",".join(LOCATIONS.keys())).split(",")
ACTIVE_LOCATIONS = [l.strip().lower() for l in ACTIVE_LOCATIONS if l.strip()]

# =============================================================================
# COLORS
# =============================================================================

class C:
    GREEN  = "\033[92m"; YELLOW = "\033[93m"; RED    = "\033[91m"
    CYAN   = "\033[96m"; GRAY   = "\033[90m"; RESET  = "\033[0m"
    BOLD   = "\033[1m";  MAGENTA= "\033[95m"

def ok(msg):   print(f"{C.GREEN}  ✅ {msg}{C.RESET}")
def warn(msg): print(f"{C.YELLOW}  ⚠️  {msg}{C.RESET}")
def info(msg): print(f"{C.CYAN}  {msg}{C.RESET}")
def skip(msg): print(f"{C.GRAY}  ⏸️  {msg}{C.RESET}")

# =============================================================================
# SIMULATION STATE
# =============================================================================

def load_sim() -> dict:
    try:
        with open(SIM_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {
            "balance": BANKROLL, "starting_balance": BANKROLL,
            "positions": {}, "trades": [], "total_trades": 0,
            "wins": 0, "losses": 0, "peak_balance": BANKROLL,
        }

def save_sim(sim: dict):
    with open(SIM_FILE, "w") as f:
        json.dump(sim, f, indent=2)

# =============================================================================
# LOGGING — NDJSON signal log
# =============================================================================

def log_signal(entry: dict):
    entry["timestamp"] = datetime.now(timezone.utc).isoformat()
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")

# =============================================================================
# FORECAST FETCHERS
# =============================================================================

_session = requests.Session()
_session.headers.update({"User-Agent": "weatherbot/3.0"})

def _get(url, timeout=12):
    return _session.get(url, timeout=timeout)

def fetch_ecmwf(loc: dict, dates: list) -> dict:
    """ECMWF via Open-Meteo. Works globally."""
    temp_unit = loc["unit"]
    url = (f"https://api.open-meteo.com/v1/ecmwf"
           f"?latitude={loc['lat']}&longitude={loc['lon']}"
           f"&daily=temperature_2m_max,temperature_2m_min"
           f"&temperature_unit={temp_unit}&timezone=auto&forecast_days=5")
    try:
        data = _get(url).json()
        if "error" not in data:
            result = {}
            for d, t in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                if d in dates and t is not None:
                    result[d] = round(t, 1) if temp_unit == "celsius" else round(t)
            return result
    except Exception as e:
        warn(f"ECMWF {loc['name']}: {e}")
    return {}

def fetch_gfs(loc: dict, dates: list) -> dict:
    """GFS deterministic via Open-Meteo. Works globally."""
    temp_unit = loc["unit"]
    url = (f"https://api.open-meteo.com/v1/gfs"
           f"?latitude={loc['lat']}&longitude={loc['lon']}"
           f"&daily=temperature_2m_max,temperature_2m_min"
           f"&temperature_unit={temp_unit}&timezone=auto&forecast_days=5")
    try:
        data = _get(url).json()
        if "error" not in data:
            result = {}
            for d, t in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                if d in dates and t is not None:
                    result[d] = round(t, 1) if temp_unit == "celsius" else round(t)
            return result
    except Exception as e:
        warn(f"GFS {loc['name']}: {e}")
    return {}

def fetch_icon(loc: dict, dates: list) -> dict:
    """DWD ICON via Open-Meteo. Works globally."""
    temp_unit = loc["unit"]
    url = (f"https://api.open-meteo.com/v1/dwd-icon"
           f"?latitude={loc['lat']}&longitude={loc['lon']}"
           f"&daily=temperature_2m_max,temperature_2m_min"
           f"&temperature_unit={temp_unit}&timezone=auto&forecast_days=5")
    try:
        data = _get(url).json()
        if "error" not in data:
            result = {}
            for d, t in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                if d in dates and t is not None:
                    result[d] = round(t, 1) if temp_unit == "celsius" else round(t)
            return result
    except Exception as e:
        warn(f"ICON {loc['name']}: {e}")
    return {}

def fetch_nws(loc: dict, dates: list) -> dict:
    """NWS hourly forecast. US stations only."""
    nws_grid = loc.get("nws")
    if not nws_grid:
        return {}
    url = f"https://api.weather.gov/gridpoints/{nws_grid}/forecast/hourly"
    try:
        data = _get(url).json()
        daily_max = {}
        for p in data["properties"]["periods"]:
            date = p["startTime"][:10]
            if date not in dates:
                continue
            temp = p["temperature"]
            if p.get("temperatureUnit") == "C":
                temp = round(temp * 9/5 + 32)
            if date not in daily_max or temp > daily_max[date]:
                daily_max[date] = temp
        return daily_max
    except Exception as e:
        warn(f"NWS {loc['name']}: {e}")
    return {}

def fetch_ensemble(loc: dict, dates: list) -> dict:
    """GFS Ensemble (30 members) via Open-Meteo. Returns per-date member arrays."""
    temp_unit = loc["unit"]
    url = (f"https://ensemble-api.open-meteo.com/v1/ensemble"
           f"?latitude={loc['lat']}&longitude={loc['lon']}"
           f"&hourly=temperature_2m&temperature_unit={temp_unit}"
           f"&timezone=auto&forecast_days=5&models=gfs_seamless")
    try:
        data = _get(url, timeout=30).json()
        if "error" in data:
            return {}
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        member_keys = sorted([k for k in hourly if "member" in k])
        if not member_keys:
            return {}

        # Compute daily max for each member
        result = {}
        for date in dates:
            indices = [i for i, t in enumerate(times) if t[:10] == date]
            if not indices:
                continue
            member_maxes = []
            for mk in member_keys:
                vals = [hourly[mk][i] for i in indices if hourly[mk][i] is not None]
                if vals:
                    member_maxes.append(max(vals))
            if member_maxes:
                result[date] = {
                    "members": member_maxes,
                    "mean": round(statistics.mean(member_maxes), 1),
                    "std": round(statistics.stdev(member_maxes), 2) if len(member_maxes) > 1 else 2.0,
                }
        return result
    except Exception as e:
        warn(f"Ensemble {loc['name']}: {e}")
    return {}

# =============================================================================
# WEIGHTED CONSENSUS + UNCERTAINTY
# =============================================================================

def compute_consensus(ecmwf_temp, gfs_temp, nws_temp, icon_temp, ensemble_data, city_slug=None):
    """Weighted consensus from available models. Returns (consensus_temp, sigma, model_spread, sources)."""
    available = {}
    if ecmwf_temp is not None:
        available["ecmwf"] = ecmwf_temp
    if gfs_temp is not None:
        available["gfs"] = gfs_temp
    if nws_temp is not None:
        available["nws"] = nws_temp
    if icon_temp is not None:
        available["icon"] = icon_temp
    if ensemble_data and "mean" in ensemble_data:
        available["ensemble"] = ensemble_data["mean"]

    if not available:
        return None, None, None, {}

    # Apply per-city bias correction (subtract known forecast bias)
    biases = CITY_BIASES.get(city_slug, {}) if city_slug else {}
    corrected = {}
    for k, v in available.items():
        corrected[k] = v - biases.get(k, 0)

    # Redistribute weights proportionally among available models
    total_weight = sum(WEIGHTS[k] for k in corrected)
    weighted_sum = sum(corrected[k] * WEIGHTS[k] / total_weight for k in corrected)
    consensus = round(weighted_sum, 1)

    # Model spread
    temps = list(corrected.values())
    model_spread = max(temps) - min(temps) if len(temps) > 1 else 0

    # Sigma estimation: blend ensemble std with model spread
    if ensemble_data and "std" in ensemble_data:
        ens_sigma = ensemble_data["std"]
        if len(temps) > 1:
            model_sigma = model_spread / 2.0
            sigma = 0.7 * ens_sigma + 0.3 * model_sigma
        else:
            sigma = ens_sigma
    elif len(temps) > 1:
        sigma = model_spread / 2.0
    else:
        sigma = 2.0

    return consensus, sigma, model_spread, available

def apply_sigma_floor(sigma, days_out):
    """Apply minimum sigma based on forecast horizon."""
    floor = SIGMA_FLOORS.get(days_out, 3.0)
    return max(sigma, floor)

# =============================================================================
# MONTE CARLO PROBABILITY ENGINE
# =============================================================================

def monte_carlo_bucket_probs(consensus, sigma, buckets, n_sims=MC_SIMS, df=5):
    """Run Monte Carlo simulations to estimate probability of each temperature bucket.

    Samples from Student's t(consensus, sigma, df) for heavier tails than Gaussian.
    df=5 gives ~10% more tail mass, better matching real forecast error distributions.

    Returns dict of bucket_key -> probability.
    """
    sims = []
    for _ in range(n_sims):
        # Student's t via Gaussian/chi-squared ratio (no scipy needed)
        z = random.gauss(0, 1)
        v = random.gammavariate(df / 2.0, 2.0)
        t_sample = z / math.sqrt(v / df)
        sims.append(consensus + sigma * t_sample)

    # Count how many simulations land in each bucket
    bucket_probs = {}
    for bkey, (t_low, t_high) in buckets.items():
        count = 0
        for s in sims:
            if t_low == -999:
                if s <= t_high:
                    count += 1
            elif t_high == 999:
                if s >= t_low:
                    count += 1
            else:
                if t_low <= s <= t_high:
                    count += 1
        bucket_probs[bkey] = count / n_sims

    return bucket_probs

# =============================================================================
# KELLY CRITERION
# =============================================================================

def compute_kelly(model_prob, market_price):
    """Quarter-Kelly sizing."""
    if model_prob <= market_price or market_price <= 0 or market_price >= 1:
        return 0.0
    p = model_prob
    q = 1.0 - p
    b = (1.0 - market_price) / market_price
    kelly = (p * b - q) / b
    return max(kelly, 0.0)

MIN_BET = _cfg.get("min_bet", 5.0)
MAX_BET = _cfg.get("max_bet", 100.0)

def kelly_bet_size(kelly_frac, bankroll):
    """Bet size from Kelly fraction, clamped to min/max from config."""
    adjusted = kelly_frac * KELLY_FRACTION  # quarter-Kelly
    raw = adjusted * bankroll
    return round(min(max(raw, MIN_BET), MAX_BET), 2)

# =============================================================================
# CONFIDENCE LEVELS
# =============================================================================

def classify_confidence(edge, model_spread):
    """Assign confidence based on edge strength and model agreement."""
    if edge >= 0.40 and model_spread < 3.0:
        return "HIGH"
    elif edge >= 0.25:
        return "MEDIUM"
    elif edge >= 0.15:
        return "LOW"
    return None  # below threshold

# =============================================================================
# POLYMARKET API (kept from existing bot)
# =============================================================================

def get_polymarket_event(city_slug, month, day, year):
    slug = f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    try:
        r = _get(f"https://gamma-api.polymarket.com/events?slug={slug}")
        data = r.json()
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
    except Exception:
        pass
    return None

def parse_temp_range(question):
    if not question:
        return None
    num = r'(-?\d+(?:\.\d+)?)'
    if re.search(r'or below', question, re.IGNORECASE):
        m = re.search(num + r'[°]?[FC] or below', question, re.IGNORECASE)
        if m: return (-999.0, float(m.group(1)))
    if re.search(r'or higher', question, re.IGNORECASE):
        m = re.search(num + r'[°]?[FC] or higher', question, re.IGNORECASE)
        if m: return (float(m.group(1)), 999.0)
    m = re.search(r'between ' + num + r'-' + num + r'[°]?[FC]', question, re.IGNORECASE)
    if m: return (float(m.group(1)), float(m.group(2)))
    m = re.search(r'be ' + num + r'[°]?[FC] on', question, re.IGNORECASE)
    if m:
        v = float(m.group(1))
        return (v - 0.5, v + 0.5)
    return None

def hours_until_resolution(event):
    try:
        end = event.get("endDate") or event.get("end_date_iso")
        if not end:
            return 999
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return max(0, (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600)
    except Exception:
        return 999

# =============================================================================
# TEMPERATURE LADDERING
# =============================================================================

def build_ladder(buckets, bucket_probs, market_prices, consensus, bankroll, model_spread):
    """Build a ladder of 2-5 adjacent underpriced buckets around consensus.

    Returns list of ladder rungs sorted by proximity to consensus, or empty list.
    Each rung: {bucket_key, range, model_prob, market_price, edge, kelly, bet_size, ev_per_dollar}
    """
    # Find all positive-EV buckets passing all gates:
    #   - price in a tradable range (MIN_ENTRY_PRICE ≤ price ≤ MAX_PRICE)
    #   - edge at least SINGLE_MIN_EDGE (config-driven) AND at least MIN_EDGE (ladder floor)
    candidates = []
    for bkey, prob in bucket_probs.items():
        price = market_prices.get(bkey, 1.0)
        if price <= 0 or price >= 1:
            continue
        if price < MIN_ENTRY_PRICE:
            continue  # penny-priced longshots: high variance, small forecast errors dominate
        if price > MAX_PRICE:
            continue  # market already overpriced relative to our tolerance
        edge = prob - price
        if edge < MIN_EDGE:
            continue
        if edge < SINGLE_MIN_EDGE:
            continue  # config-driven primary edge gate (previously unused bug)

        t_low, t_high = buckets[bkey]
        # Distance from consensus to bucket midpoint
        if t_low == -999:
            mid = t_high - 2
        elif t_high == 999:
            mid = t_low + 2
        else:
            mid = (t_low + t_high) / 2
        dist = abs(consensus - mid)

        kelly = compute_kelly(prob, price)
        if kelly <= 0:
            continue

        confidence = classify_confidence(edge, model_spread)
        if confidence is None:
            continue

        ev_per_dollar = (prob * (1.0 / price - 1.0) - (1.0 - prob))

        candidates.append({
            "bucket_key": bkey,
            "range": buckets[bkey],
            "model_prob": round(prob, 4),
            "market_price": round(price, 4),
            "edge": round(edge, 4),
            "kelly_raw": round(kelly, 4),
            "ev_per_dollar": round(ev_per_dollar, 4),
            "distance": dist,
            "confidence": confidence,
        })

    if not candidates:
        return []

    # Sort by proximity to consensus (closest first)
    candidates.sort(key=lambda x: x["distance"])
    ladder = candidates[:MAX_LADDER_RUNGS]

    # Allocate capital proportional to edge strength
    total_edge = sum(r["edge"] for r in ladder)
    budget = bankroll * LADDER_BUDGET
    for rung in ladder:
        frac = rung["edge"] / total_edge
        raw_bet = frac * budget
        rung["bet_size"] = round(min(max(raw_bet, MIN_BET), MAX_BET), 2)

    # Combined hit probability: 1 - product(1 - prob_i)
    combined_prob = 1.0 - math.prod(1.0 - r["model_prob"] for r in ladder)

    for rung in ladder:
        rung["combined_hit_prob"] = round(combined_prob, 4)

    return ladder

# =============================================================================
# POSITION MANAGEMENT — exits, resolution, take-profit
# =============================================================================

def check_market_resolved(market_id):
    """Check if Polymarket resolved the market. Returns None/True/False."""
    try:
        r = _get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=8)
        data = r.json()
        if not data.get("closed", False):
            return None
        prices = json.loads(data.get("outcomePrices", "[0.5,0.5]"))
        yes_price = float(prices[0])
        if yes_price >= 0.95:
            return True   # YES won
        elif yes_price <= 0.05:
            return False  # NO won
        return None  # not yet determined
    except Exception:
        return None

def get_current_price(market_id):
    """Fetch current YES price for a market."""
    try:
        r = _get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=5)
        data = r.json()
        prices = json.loads(data.get("outcomePrices", "[0.5,0.5]"))
        return float(prices[0])
    except Exception:
        return None

def check_exits(sim):
    """Check all open positions for resolution or take-profit exits.

    Resolution: market closed on Polymarket → record win/loss.
    Take-profit: price >= 0.75 with > 24h left → sell early.
    Stop-loss: price dropped > 50% from entry → cut losses.

    Returns (updated_sim, n_closed).
    """
    positions = sim["positions"]
    balance = sim["balance"]
    closed_ids = []
    n_closed = 0

    for mid, pos in list(positions.items()):
        # 1. Check if market resolved
        resolved = check_market_resolved(mid)
        if resolved is not None:
            entry = pos["entry_price"]
            cost = pos["cost"]
            shares = pos["shares"]

            # Fetch actual observed temperature for calibration
            actual_temp = None
            city_slug = pos.get("city", "")
            if city_slug in LOCATIONS:
                loc_info = LOCATIONS[city_slug]
                try:
                    obs_url = (f"https://archive-api.open-meteo.com/v1/archive"
                               f"?latitude={loc_info['lat']}&longitude={loc_info['lon']}"
                               f"&daily=temperature_2m_max&temperature_unit={loc_info['unit']}"
                               f"&timezone=auto&start_date={pos.get('date')}&end_date={pos.get('date')}")
                    obs_data = _get(obs_url, timeout=10).json()
                    obs_temps = obs_data.get("daily", {}).get("temperature_2m_max", [])
                    if obs_temps and obs_temps[0] is not None:
                        actual_temp = obs_temps[0]
                except Exception:
                    pass

            if resolved:  # YES won — we win
                pnl = round(shares * 1.0 - cost, 2)
                balance += cost + pnl
                sim["wins"] += 1
                result_str = f"{C.GREEN}WIN +${pnl:.2f}{C.RESET}"
            else:  # NO won — we lose
                pnl = round(-cost, 2)
                sim["losses"] += 1
                result_str = f"{C.RED}LOSS -${cost:.2f}{C.RESET}"

            actual_str = f" | Actual: {actual_temp}°" if actual_temp else ""
            ok(f"RESOLVED: {pos['question'][:50]}... | {result_str}{actual_str}")
            log_signal({
                "type": "resolution",
                "city": pos.get("city", ""),
                "date": pos.get("date", ""),
                "question": pos["question"],
                "entry_price": entry,
                "exit_price": 1.0 if resolved else 0.0,
                "pnl": pnl,
                "result": "win" if resolved else "loss",
                "market_id": mid,
                "actual_temp": actual_temp,
                "consensus_temp": pos.get("consensus_temp"),
                "model_probability": pos.get("model_prob"),
                "model_spread": pos.get("model_spread"),
                "sigma_used": pos.get("sigma_used"),
                "confidence": pos.get("confidence"),
                "edge": pos.get("edge"),
                "horizon": pos.get("horizon"),
                "bucket": pos.get("bucket"),
            })

            sim["trades"].append({
                "type": "exit", "reason": "resolved",
                "question": pos["question"],
                "entry_price": entry,
                "exit_price": 1.0 if resolved else 0.0,
                "pnl": pnl,
                "closed_at": datetime.now().isoformat(),
            })
            closed_ids.append(mid)
            n_closed += 1
            continue

        # 2. Check take-profit / stop-loss on still-open markets
        current = get_current_price(mid)
        if current is None:
            continue

        entry = pos["entry_price"]
        cost = pos["cost"]
        shares = pos["shares"]
        reason = None

        # Take-profit: price >= 0.75 (lock in gains)
        if current >= 0.75:
            reason = "take_profit"
        # No stop-loss: these are binary markets with capped downside ($cost).
        # Price drops before resolution are noise, not a reason to sell.
        # Max loss is already limited to the $100 bet.

        if reason:
            pnl = round((current - entry) * shares, 2)
            balance += cost + pnl
            color = C.GREEN if pnl >= 0 else C.RED
            tag = "TAKE PROFIT" if reason == "take_profit" else "STOP LOSS"

            if pnl >= 0:
                sim["wins"] += 1
            else:
                sim["losses"] += 1

            ok(f"{tag}: {pos['question'][:50]}... | "
               f"${entry:.3f}→${current:.3f} | {color}{'+'if pnl>=0 else ''}{pnl:.2f}{C.RESET}")

            log_signal({
                "type": reason,
                "city": pos.get("city", ""),
                "date": pos.get("date", ""),
                "question": pos["question"],
                "entry_price": entry, "exit_price": current,
                "pnl": pnl, "market_id": mid,
            })

            sim["trades"].append({
                "type": "exit", "reason": reason,
                "question": pos["question"],
                "entry_price": entry, "exit_price": current,
                "pnl": pnl, "closed_at": datetime.now().isoformat(),
            })
            closed_ids.append(mid)
            n_closed += 1

    # Remove closed positions
    for mid in closed_ids:
        del positions[mid]

    sim["balance"] = round(balance, 2)
    sim["peak_balance"] = max(sim.get("peak_balance", balance), balance)
    return sim, n_closed


def evaluate_thesis_break(pos, current_sources, current_model_prob, current_market_price):
    """Decide whether an open position's original thesis has broken.

    Returns (reason, detail) or (None, None). Reasons:
      - "source_degradation": forecasts we relied on at entry are no longer available
      - "model_decay":        model_prob has dropped meaningfully since entry
      - "edge_collapse":      current edge has fallen below our LOW threshold
    """
    entry_sources = pos.get("entry_sources") or []
    entry_prob = pos.get("model_prob")

    if isinstance(entry_sources, list) and len(entry_sources) >= THESIS_MIN_ENTRY_SOURCES:
        missing = len(entry_sources) - len(current_sources)
        if missing >= THESIS_SOURCE_DROP:
            dropped = sorted(set(entry_sources) - set(current_sources))
            return ("source_degradation", f"-{missing} sources ({','.join(dropped)})")

    if entry_prob is not None:
        prob_drop = entry_prob - current_model_prob
        if prob_drop >= THESIS_MODEL_PROB_DROP:
            return ("model_decay", f"prob {entry_prob:.1%} → {current_model_prob:.1%}")

    current_edge = current_model_prob - current_market_price
    if current_edge < THESIS_MIN_EDGE:
        return ("edge_collapse", f"edge {current_edge:+.1%}")

    return (None, None)


def apply_thesis_break_exits(sim, balance, sources, bucket_probs, market_prices, market_ids):
    """Inspect open positions whose markets appear in this scan and close any
    where the original thesis has broken. Returns (sim, balance, n_closed)."""
    n_closed = 0
    current_sources = list(sources.keys()) if sources else []

    for bkey, prob in bucket_probs.items():
        mid = market_ids.get(bkey, "")
        if not mid or mid not in sim["positions"]:
            continue
        price = market_prices.get(bkey)
        if price is None or price <= 0 or price >= 1:
            continue

        pos = sim["positions"][mid]
        reason, detail = evaluate_thesis_break(pos, current_sources, prob, price)
        if not reason:
            continue

        entry = pos["entry_price"]
        cost = pos["cost"]
        shares = pos["shares"]
        proceeds = round(shares * price, 2)
        pnl = round(proceeds - cost, 2)
        balance += proceeds

        if pnl >= 0:
            sim["wins"] += 1
        else:
            sim["losses"] += 1

        color = C.GREEN if pnl >= 0 else C.RED
        ok(f"THESIS BREAK ({reason}): {pos['question'][:50]}... | "
           f"${entry:.3f}→${price:.3f} | {color}{'+'if pnl>=0 else ''}{pnl:.2f}{C.RESET} | {detail}")

        log_signal({
            "type": "thesis_break",
            "reason": reason,
            "detail": detail,
            "city": pos.get("city", ""),
            "date": pos.get("date", ""),
            "question": pos["question"],
            "entry_price": entry,
            "exit_price": price,
            "entry_model_prob": pos.get("model_prob"),
            "current_model_prob": prob,
            "entry_sources": pos.get("entry_sources"),
            "current_sources": current_sources,
            "pnl": pnl,
            "market_id": mid,
        })
        sim["trades"].append({
            "type": "exit", "reason": f"thesis_break:{reason}",
            "question": pos["question"],
            "entry_price": entry, "exit_price": price,
            "pnl": pnl, "closed_at": datetime.now().isoformat(),
        })
        del sim["positions"][mid]
        n_closed += 1

    sim["balance"] = round(balance, 2)
    return sim, balance, n_closed

# =============================================================================
# SHOW POSITIONS
# =============================================================================

def show_positions():
    sim = load_sim()
    positions = sim["positions"]
    print(f"\n{C.BOLD}📊 Open Positions:{C.RESET}")
    if not positions:
        print("  No open positions")
        return

    total_pnl = 0
    for mid, pos in positions.items():
        try:
            r = _get(f"https://gamma-api.polymarket.com/markets/{mid}", timeout=5)
            prices = json.loads(r.json().get("outcomePrices", "[0.5,0.5]"))
            current = float(prices[0])
        except Exception:
            current = pos["entry_price"]

        pnl = (current - pos["entry_price"]) * pos["shares"]
        total_pnl += pnl
        color = C.GREEN if pnl >= 0 else C.RED
        print(f"\n  • {pos['question'][:65]}...")
        print(f"    Entry: ${pos['entry_price']:.3f} | Now: ${current:.3f} | "
              f"Shares: {pos['shares']:.1f} | PnL: {color}{'+'if pnl>=0 else ''}{pnl:.2f}{C.RESET}")

    print(f"\n  Balance:      ${sim['balance']:.2f}")
    color = C.GREEN if total_pnl >= 0 else C.RED
    print(f"  Open PnL:     {color}{'+'if total_pnl>=0 else ''}{total_pnl:.2f}{C.RESET}")
    print(f"  Total trades: {sim['total_trades']} | W/L: {sim['wins']}/{sim['losses']}")

# =============================================================================
# MAIN SCAN
# =============================================================================

def run(dry_run=True):
    print(f"\n{C.BOLD}{C.CYAN}🌤  Weather Trading Bot v3 — Multi-Model + Monte Carlo + Laddering{C.RESET}")
    print("=" * 65)

    sim = load_sim()
    balance = sim["balance"]
    mode = f"{C.YELLOW}PAPER{C.RESET}" if dry_run else f"{C.GREEN}LIVE{C.RESET}"
    ret = (balance - sim["starting_balance"]) / sim["starting_balance"] * 100
    ret_str = f"{C.GREEN}+{ret:.1f}%{C.RESET}" if ret >= 0 else f"{C.RED}{ret:.1f}%{C.RESET}"

    print(f"\n  Mode:       {mode}")
    print(f"  Balance:    {C.BOLD}${balance:.2f}{C.RESET} ({ret_str})")
    print(f"  Models:     ECMWF(35%) + GFS(25%) + NWS(20%) + Ensemble(20%)")
    print(f"  MC sims:    {MC_SIMS:,}")
    print(f"  Ladder:     up to {MAX_LADDER_RUNGS} rungs, {LADDER_BUDGET:.0%} budget")
    print(f"  Kelly:      quarter-Kelly, ${MIN_BET:.0f}-${MAX_BET:.0f} per rung")
    print(f"  Cities:     {len(ACTIVE_LOCATIONS)}")

    # --- CHECK EXITS on open positions ---
    n_open = len(sim["positions"])
    if n_open > 0 and not dry_run:
        print(f"\n{C.BOLD}📤 Checking {n_open} open position{'s' if n_open != 1 else ''}...{C.RESET}")
        sim, n_closed = check_exits(sim)
        balance = sim["balance"]
        save_sim(sim)
        if n_closed:
            info(f"Closed {n_closed} position{'s' if n_closed != 1 else ''} | Balance: ${balance:.2f}")
        else:
            skip("No exits triggered")
    elif n_open > 0:
        print(f"\n  {C.GRAY}  {n_open} open position{'s' if n_open != 1 else ''} (exits only checked in --live mode){C.RESET}")

    trades_executed = 0

    for city_slug in ACTIVE_LOCATIONS:
        if city_slug not in LOCATIONS:
            warn(f"Unknown: {city_slug}")
            continue
        loc = LOCATIONS[city_slug]

        dates = [(datetime.now() + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(4)]

        # Fetch all forecasts
        ecmwf_data = fetch_ecmwf(loc, dates)
        gfs_data = fetch_gfs(loc, dates)
        icon_data = fetch_icon(loc, dates)
        nws_data = fetch_nws(loc, dates) if "nws" in loc else {}
        ensemble_data = fetch_ensemble(loc, dates)

        for day_offset, date_str in enumerate(dates):
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            month = MONTHS[dt.month - 1]

            # Get forecasts for this date
            ecmwf_temp = ecmwf_data.get(date_str)
            gfs_temp = gfs_data.get(date_str)
            icon_temp = icon_data.get(date_str)
            nws_temp = nws_data.get(date_str)
            ens = ensemble_data.get(date_str)

            consensus, sigma, model_spread, sources = compute_consensus(
                ecmwf_temp, gfs_temp, nws_temp, icon_temp, ens, city_slug)
            if consensus is None:
                continue

            sigma = apply_sigma_floor(sigma, day_offset)
            unit_sym = "°F" if loc["unit"] == "fahrenheit" else "°C"

            # Find Polymarket event
            event = get_polymarket_event(city_slug, month, dt.day, dt.year)
            if not event:
                continue

            hours = hours_until_resolution(event)
            if hours < MIN_HOURS or hours > MAX_HOURS:
                continue

            # Parse all market buckets
            buckets = {}      # bucket_key -> (t_low, t_high)
            market_prices = {}  # bucket_key -> yes price
            market_ids = {}   # bucket_key -> market_id
            market_questions = {}  # bucket_key -> question
            market_slugs = {}  # bucket_key -> slug from event

            event_slug = event.get("slug", "")

            for market in event.get("markets", []):
                question = market.get("question", "")
                rng = parse_temp_range(question)
                if not rng:
                    continue
                try:
                    prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
                    yes_price = float(prices[0])
                except Exception:
                    continue
                bkey = f"{rng[0]}-{rng[1]}"
                buckets[bkey] = rng
                market_prices[bkey] = yes_price
                market_ids[bkey] = str(market.get("id", ""))
                market_questions[bkey] = question
                market_slugs[bkey] = market.get("slug", event_slug)

            if not buckets:
                continue

            # Run Monte Carlo
            bucket_probs = monte_carlo_bucket_probs(
                consensus, sigma, buckets)

            # Thesis-break exits: close any open position on these markets
            # whose original entry thesis no longer holds.
            if not dry_run:
                sim, balance, _ = apply_thesis_break_exits(
                    sim, balance, sources, bucket_probs, market_prices, market_ids)

            # Build ladder
            ladder = build_ladder(
                buckets, bucket_probs, market_prices,
                consensus, balance, model_spread or 0)

            if not ladder:
                continue

            # Filter: only trade HIGH and MEDIUM confidence
            tradeable = [r for r in ladder if r["confidence"] in ("HIGH", "MEDIUM")]
            if not tradeable:
                continue

            # Display
            combined_prob = tradeable[0]["combined_hit_prob"]
            src_str = "+".join(s.upper() for s in sources)

            print(f"\n{C.BOLD}{'─'*65}")
            print(f"  LADDER: {loc['name']} — {date_str} (D+{day_offset}, {hours:.0f}h){C.RESET}")
            print(f"  Consensus: {consensus}{unit_sym} | Spread: {model_spread:.1f}{unit_sym} | "
                  f"Sigma: {sigma:.2f} | Sources: {src_str}")
            print(f"  Hit prob: {C.GREEN}{combined_prob:.1%}{C.RESET} (any rung wins)")
            print(f"  ┌{'─'*61}┐")
            print(f"  │ {'Bucket':<12} {'Prob':>6} {'Price':>7} {'Edge':>7} {'Bet':>8} {'EV/$':>7} {'Conf':<6} │")
            print(f"  ├{'─'*61}┤")

            for rung in tradeable:
                t_low, t_high = rung["range"]
                if t_low == -999:
                    bucket_label = f"≤{t_high}{unit_sym}"
                elif t_high == 999:
                    bucket_label = f"≥{t_low}{unit_sym}"
                else:
                    bucket_label = f"{t_low}-{t_high}{unit_sym}"

                conf_color = C.GREEN if rung["confidence"] == "HIGH" else C.YELLOW
                print(f"  │ {bucket_label:<12} {rung['model_prob']:>5.1%} "
                      f"  ${rung['market_price']:.2f} {rung['edge']:>+6.1%} "
                      f"  ${rung['bet_size']:>5.2f} {rung['ev_per_dollar']:>+6.2f} "
                      f"{conf_color}{rung['confidence']:<6}{C.RESET} │")

            print(f"  └{'─'*61}┘")

            # Log each signal
            for rung in tradeable:
                bkey = rung["bucket_key"]
                log_signal({
                    "type": "ladder" if len(tradeable) > 1 else "edge",
                    "city": city_slug,
                    "city_name": loc["name"],
                    "date": date_str,
                    "bucket": rung["bucket_key"],
                    "bucket_range": rung["range"],
                    "model_probability": rung["model_prob"],
                    "market_price": rung["market_price"],
                    "edge_pct": rung["edge"],
                    "kelly_fraction": rung["kelly_raw"],
                    "bet_size_usd": rung["bet_size"],
                    "confidence": rung["confidence"],
                    "consensus_temp": consensus,
                    "model_spread": model_spread,
                    "ensemble_sigma": ens["std"] if ens else None,
                    "sigma_used": sigma,
                    "sources": sources,
                    "combined_hit_prob": combined_prob,
                    "market_id": market_ids.get(bkey, ""),
                    "market_slug": market_slugs.get(bkey, ""),
                    "ev_per_dollar": rung["ev_per_dollar"],
                })

            # Execute paper trades
            if not dry_run:
                for rung in tradeable:
                    bkey = rung["bucket_key"]
                    mid = market_ids.get(bkey, "")
                    if not mid or mid in sim["positions"]:
                        continue
                    if rung["bet_size"] > balance:
                        continue

                    cost = rung["bet_size"]
                    shares = cost / rung["market_price"]
                    balance -= cost
                    sim["positions"][mid] = {
                        "question": market_questions.get(bkey, ""),
                        "entry_price": rung["market_price"],
                        "shares": round(shares, 2),
                        "cost": cost,
                        "date": date_str,
                        "city": city_slug,
                        "consensus_temp": consensus,
                        "model_prob": rung["model_prob"],
                        "model_spread": model_spread,
                        "sigma_used": sigma,
                        "confidence": rung["confidence"],
                        "edge": rung["edge"],
                        "horizon": day_offset,
                        "bucket": bkey,
                        "entry_sources": list(sources.keys()),
                        "opened_at": datetime.now().isoformat(),
                    }
                    sim["total_trades"] += 1
                    sim["trades"].append({
                        "type": "entry",
                        "question": market_questions.get(bkey, ""),
                        "entry_price": rung["market_price"],
                        "shares": round(shares, 2),
                        "cost": cost,
                        "date": date_str,
                        "city": city_slug,
                        "opened_at": datetime.now().isoformat(),
                    })
                    trades_executed += 1
                    ok(f"Position opened: {market_questions.get(bkey, '')[:50]}... ${cost:.2f}")

    # Save state
    if not dry_run:
        sim["balance"] = round(balance, 2)
        sim["peak_balance"] = max(sim.get("peak_balance", balance), balance)
        save_sim(sim)

    # Summary
    print(f"\n{'='*65}")
    print(f"{C.BOLD}📊 Summary:{C.RESET}")
    info(f"Balance:     ${balance:.2f}")
    info(f"Signals:     logged to {LOG_FILE}")
    if dry_run:
        print(f"\n  {C.YELLOW}[PAPER MODE — use --live to simulate trades]{C.RESET}")
    else:
        info(f"Trades:      {trades_executed} this run")

# =============================================================================
# CONTINUOUS LOOP
# =============================================================================

def run_loop(dry_run=True):
    """Run continuously: scan every SCAN_INTERVAL seconds. Ctrl+C to stop."""
    interval = SCAN_INTERVAL
    mode = "PAPER" if dry_run else "LIVE"

    print(f"\n{C.BOLD}{C.CYAN}🌤  Weather Bot v3 — Continuous Mode ({mode}){C.RESET}")
    print(f"  Scan interval: {interval // 60} min | Ctrl+C to stop\n")

    while True:
        try:
            run(dry_run=dry_run)
        except requests.exceptions.ConnectionError:
            warn("Connection lost — retrying in 60s")
            import time; time.sleep(60)
            continue
        except Exception as e:
            warn(f"Scan error: {e} — retrying in 60s")
            import time; time.sleep(60)
            continue

        next_scan = datetime.now() + timedelta(seconds=interval)
        print(f"\n  {C.GRAY}Next scan: {next_scan.strftime('%H:%M:%S')} "
              f"({interval // 60} min) — Ctrl+C to stop{C.RESET}")
        try:
            import time; time.sleep(interval)
        except KeyboardInterrupt:
            print(f"\n{C.BOLD}  Stopped. Bye!{C.RESET}")
            break

# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Weather Trading Bot v3")
    parser.add_argument("--live", action="store_true", help="Execute paper trades")
    parser.add_argument("--loop", action="store_true", help="Run continuously (scan every hour)")
    parser.add_argument("--positions", action="store_true", help="Show positions")
    parser.add_argument("--reset", action="store_true", help="Reset simulation")
    args = parser.parse_args()

    if args.reset:
        if os.path.exists(SIM_FILE):
            os.remove(SIM_FILE)
        ok(f"Simulation reset — balance back to ${BANKROLL:.2f}")
    elif args.positions:
        show_positions()
    elif args.loop:
        run_loop(dry_run=not args.live)
    else:
        run(dry_run=not args.live)
