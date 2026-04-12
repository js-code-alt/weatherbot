#!/usr/bin/env python3
"""
Weather Bot Dashboard — Clean terminal view
Usage: python dashboard.py
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

SIM_FILE = "simulation_v3.json"
LOG_FILE = Path("logs/weather-signals.ndjson")

# ── Colors ──────────────────────────────────────────────────────────────────

class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    GREEN   = "\033[92m"
    RED     = "\033[91m"
    YELLOW  = "\033[93m"
    CYAN    = "\033[96m"
    MAGENTA = "\033[95m"
    WHITE   = "\033[97m"
    BLUE    = "\033[94m"
    BG_DARK = "\033[48;5;235m"

def colored(text, color):
    return f"{color}{text}{C.RESET}"

# ── Data Loading ────────────────────────────────────────────────────────────

def load_sim():
    try:
        with open(SIM_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return None

def load_recent_signals(n=10):
    if not LOG_FILE.exists():
        return []
    lines = LOG_FILE.read_text().strip().split("\n")
    signals = []
    for line in lines[-n:]:
        try:
            signals.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return signals

# ── Formatting Helpers ──────────────────────────────────────────────────────

def fmt_money(val):
    if val >= 0:
        return colored(f"${val:,.2f}", C.GREEN)
    return colored(f"-${abs(val):,.2f}", C.RED)

def fmt_pct(val):
    sign = "+" if val >= 0 else ""
    color = C.GREEN if val >= 0 else C.RED
    return colored(f"{sign}{val:.1f}%", color)

def fmt_edge(edge):
    pct = edge * 100
    if pct >= 30:
        return colored(f"+{pct:.1f}%", C.GREEN)
    elif pct >= 15:
        return colored(f"+{pct:.1f}%", C.YELLOW)
    return colored(f"+{pct:.1f}%", C.WHITE)

def fmt_confidence(conf):
    colors = {"HIGH": C.GREEN, "MEDIUM": C.YELLOW, "LOW": C.RED}
    return colored(conf, colors.get(conf, C.WHITE))

def time_ago(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        hours = delta.total_seconds() / 3600
        if hours < 1:
            return f"{int(delta.total_seconds() / 60)}m ago"
        elif hours < 24:
            return f"{int(hours)}h ago"
        else:
            return f"{int(hours / 24)}d ago"
    except Exception:
        return "?"

def parse_bet_description(question, city):
    """Extract a human-readable bet description from the market slug or question."""
    q = question.replace("-", " ").replace("highest temperature in ", "")
    # Extract temperature target
    # Patterns: "13c", "58forhigher", "76 77f", "14corbelow"
    import re

    # "23corhigher" -> "23°C or higher"
    m = re.search(r"(\d+)corhigher", question)
    if m:
        return f"High ≥ {m.group(1)}°C"

    m = re.search(r"(\d+)forhigher", question)
    if m:
        return f"High ≥ {m.group(1)}°F"

    # "14corbelow" -> "14°C or below"
    m = re.search(r"(\d+)corbelow", question)
    if m:
        return f"High ≤ {m.group(1)}°C"

    m = re.search(r"(\d+)forbelow", question)
    if m:
        return f"High ≤ {m.group(1)}°F"

    # "13c" -> "High = 13°C"
    m = re.search(r"(\d+)c$", question)
    if m:
        return f"High = {m.group(1)}°C"

    # "76-77f" -> "High = 76-77°F"
    m = re.search(r"(\d+)\D*(\d+)f$", question)
    if m:
        return f"High = {m.group(1)}-{m.group(2)}°F"

    # "44-45f" pattern
    m = re.search(r"(\d+)\D+(\d+)f", question)
    if m:
        return f"High = {m.group(1)}-{m.group(2)}°F"

    # Fallback: try to extract from "Will the highest temperature..." style questions
    if "be " in question and " on " in question:
        target = question.split("be ")[-1].split(" on ")[0]
        return f"High = {target}"

    return question[:40]

def market_status(date_str):
    """Return market status: LIVE, AWAITING RESOLUTION, or time remaining."""
    try:
        # Market date = the weather day. Resolution happens AFTER this day ends.
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = dt - now
        hours_until_date = delta.total_seconds() / 3600

        if hours_until_date < -24:
            # More than 24h past the date — should be resolved soon or already
            return colored("⏳ AWAITING RESOLUTION", C.YELLOW), "awaiting"
        elif hours_until_date < 0:
            # Date has passed but within 24h — weather data being finalized
            hours_ago = abs(hours_until_date)
            return colored(f"⏳ Day ended {int(hours_ago)}h ago · resolves soon", C.YELLOW), "awaiting"
        elif hours_until_date < 24:
            h = int(hours_until_date)
            return colored(f"🟢 LIVE · {h}h until day ends", C.GREEN), "live"
        else:
            d = int(hours_until_date / 24)
            h = int(hours_until_date % 24)
            return colored(f"🟢 LIVE · {d}d {h}h until day ends", C.GREEN), "live"
    except Exception:
        return colored("?", C.DIM), "unknown"

# ── Display Sections ────────────────────────────────────────────────────────

WIDTH = 66
SEP = "─" * WIDTH

def header():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print()
    print(colored("┌" + SEP + "┐", C.DIM))
    title = "WEATHER BOT DASHBOARD"
    pad = (WIDTH - len(title)) // 2
    print(colored("│", C.DIM) + " " * pad + colored(title, C.BOLD + C.CYAN) + " " * (WIDTH - pad - len(title)) + colored("│", C.DIM))
    pad2 = (WIDTH - len(now)) // 2
    print(colored("│", C.DIM) + " " * pad2 + colored(now, C.DIM) + " " * (WIDTH - pad2 - len(now)) + colored("│", C.DIM))
    print(colored("└" + SEP + "┘", C.DIM))

def balance_section(sim):
    balance = sim["balance"]
    starting = sim["starting_balance"]
    pnl = balance - starting
    pnl_pct = (pnl / starting) * 100 if starting > 0 else 0
    peak = sim.get("peak_balance", starting)
    drawdown = ((peak - balance) / peak) * 100 if peak > 0 else 0
    wins = sim.get("wins", 0)
    losses = sim.get("losses", 0)
    total = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0
    cost_in_play = sum(p.get("cost", 0) for p in sim.get("positions", {}).values())

    print()
    print(colored("  ACCOUNT", C.BOLD + C.WHITE))
    print(colored("  " + "─" * 40, C.DIM))
    print(f"  Balance:       {fmt_money(balance)}")
    print(f"  P&L:           {fmt_money(pnl)}  ({fmt_pct(pnl_pct)})")
    print(f"  Peak:          ${peak:,.2f}  (drawdown {fmt_pct(-drawdown)})")
    print(f"  At risk:       ${cost_in_play:,.2f}  ({len(sim.get('positions', {}))} open)")
    print(f"  Record:        {colored(str(wins), C.GREEN)}W / {colored(str(losses), C.RED)}L" +
          (f"  ({win_rate:.0f}% win rate)" if total > 0 else ""))

def positions_section(sim):
    positions = sim.get("positions", {})
    print()
    if not positions:
        print(colored("  NO OPEN POSITIONS", C.BOLD + C.WHITE))
        return

    # Sort: live first (by time remaining), then awaiting resolution
    def sort_key(item):
        mid, pos = item
        date = pos.get("date", "9999-99-99")
        _, status = market_status(date)
        order = {"live": 0, "awaiting": 1, "unknown": 2}
        return (order.get(status, 2), date)

    sorted_positions = sorted(positions.items(), key=sort_key)

    # Group by status
    live_positions = []
    awaiting_positions = []
    for mid, pos in sorted_positions:
        _, status = market_status(pos.get("date", ""))
        if status == "live":
            live_positions.append((mid, pos))
        else:
            awaiting_positions.append((mid, pos))

    total = len(positions)

    if live_positions:
        print(colored(f"  🟢 LIVE BETS ({len(live_positions)})", C.BOLD + C.GREEN))
        print(colored("  " + "─" * 62, C.DIM))
        for mid, pos in live_positions:
            _render_position(mid, pos)

    if awaiting_positions:
        if live_positions:
            print()
        print(colored(f"  ⏳ AWAITING RESOLUTION ({len(awaiting_positions)})", C.BOLD + C.YELLOW))
        print(colored("  " + "─" * 62, C.DIM))
        for mid, pos in awaiting_positions:
            _render_position(mid, pos)

def _render_position(mid, pos):
    city = pos.get("city", "?").upper()
    date = pos.get("date", "?")
    entry = pos.get("entry_price", 0)
    cost = pos.get("cost", 0)
    shares = pos.get("shares", 0)
    edge = pos.get("edge", 0)
    conf = pos.get("confidence", "?")
    opened = pos.get("opened_at", "")
    question = pos.get("question", "")

    bet_desc = parse_bet_description(question, city)
    status_str, _ = market_status(date)
    potential_win = shares * 1.0 - cost
    odds = f"1:{int(1/entry)}" if entry > 0 and entry < 1 else "?"

    print()
    # Line 1: City, date, bet description
    print(f"  {colored(city, C.BOLD + C.CYAN)}  {colored(date, C.DIM)}  ·  {colored(bet_desc, C.BOLD + C.WHITE)}")
    # Line 2: Status
    print(f"  {status_str}")
    # Line 3: Entry, odds, cost
    print(f"  Entry: ${entry:.3f}  ({odds} odds)  ·  Cost: ${cost:.2f}  ·  {shares:.0f} shares")
    # Line 4: Edge, confidence, opened
    print(f"  Edge: {fmt_edge(edge)}  ·  Conf: {fmt_confidence(conf)}  ·  Opened: {time_ago(opened)}")
    # Line 5: Payoff
    print(f"  Win: {colored(f'+${potential_win:,.2f}', C.GREEN)}  ·  Lose: {colored(f'-${cost:.2f}', C.RED)}")

def signals_section():
    signals = load_recent_signals(8)
    print()
    print(colored("  RECENT ACTIVITY", C.BOLD + C.WHITE))
    print(colored("  " + "─" * 62, C.DIM))

    if not signals:
        print(colored("  No activity logged yet", C.DIM))
        return

    for sig in reversed(signals):
        sig_type = sig.get("type", "scan")
        city = sig.get("city", "?").upper()
        ts = sig.get("timestamp", "")
        ago = time_ago(ts) if ts else "?"

        if sig_type == "resolution":
            result = sig.get("result", "?")
            pnl = sig.get("pnl", 0)
            icon = "✅" if result == "win" else "❌"
            label = "WON" if result == "win" else "LOST"
            pnl_str = fmt_money(pnl)
            print(f"  {icon} {colored(ago, C.DIM):>12s}  {city:<10s}  {label}  {pnl_str}")

        elif sig_type == "take_profit":
            pnl = sig.get("pnl", 0)
            pnl_str = fmt_money(pnl)
            print(f"  💰 {colored(ago, C.DIM):>12s}  {city:<10s}  TAKE PROFIT  {pnl_str}")

        elif sig_type == "stop_loss":
            pnl = sig.get("pnl", 0)
            pnl_str = fmt_money(pnl)
            print(f"  🛑 {colored(ago, C.DIM):>12s}  {city:<10s}  STOP LOSS  {pnl_str}")

        elif sig_type == "entry":
            cost = sig.get("cost", 0)
            edge = sig.get("edge", 0)
            print(f"  📥 {colored(ago, C.DIM):>12s}  {city:<10s}  NEW BET  ${cost:.2f}  edge {fmt_edge(edge)}")

        else:
            edge = sig.get("edge", sig.get("best_edge", 0))
            prob = sig.get("model_prob", sig.get("hit_prob", 0))
            if isinstance(prob, (int, float)) and prob > 0:
                print(f"  🔍 {colored(ago, C.DIM):>12s}  {city:<10s}  SCAN  prob {prob:.0%}  edge {fmt_edge(edge)}")

def next_scan_str():
    """Calculate time until next hourly cron scan (runs at :00)."""
    now = datetime.now(timezone.utc)
    # Next full hour
    next_hour = now.replace(minute=0, second=0, microsecond=0)
    if next_hour <= now:
        next_hour = next_hour.replace(hour=next_hour.hour + 1)
    delta = next_hour - now
    mins = int(delta.total_seconds() / 60)
    if mins <= 1:
        return colored("⚡ scanning now", C.GREEN)
    return f"in {mins}m ({next_hour.strftime('%H:%M')} UTC)"

def footer(sim):
    total_trades = sim.get("total_trades", 0)
    print()
    print(colored("  " + "─" * 62, C.DIM))
    print(f"  Next scan: {next_scan_str()}")
    print(colored(f"  {total_trades} total trades  ·  Scans hourly  ·  Quarter-Kelly sizing  ·  12 cities", C.DIM))
    print()

# ── Main ────────────────────────────────────────────────────────────────────

def main():
    os.system("clear" if os.name != "nt" else "cls")
    sim = load_sim()
    if sim is None:
        print(colored("  No simulation data found. Run bot_v3.py first.", C.RED))
        return

    header()
    balance_section(sim)
    positions_section(sim)
    signals_section()
    footer(sim)

if __name__ == "__main__":
    main()
