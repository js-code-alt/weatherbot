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

def time_until(date_str):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        delta = dt - datetime.now(timezone.utc)
        hours = delta.total_seconds() / 3600
        if hours < 0:
            return colored("EXPIRED", C.RED)
        elif hours < 24:
            return colored(f"{int(hours)}h left", C.YELLOW)
        else:
            return f"{int(hours / 24)}d {int(hours % 24)}h left"
    except Exception:
        return "?"

# ── Display Sections ────────────────────────────────────────────────────────

WIDTH = 66

def header():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print()
    print(colored("┌" + "─" * WIDTH + "┐", C.DIM))
    title = "WEATHER BOT DASHBOARD"
    pad = (WIDTH - len(title)) // 2
    print(colored("│", C.DIM) + " " * pad + colored(title, C.BOLD + C.CYAN) + " " * (WIDTH - pad - len(title)) + colored("│", C.DIM))
    pad2 = (WIDTH - len(now)) // 2
    print(colored("│", C.DIM) + " " * pad2 + colored(now, C.DIM) + " " * (WIDTH - pad2 - len(now)) + colored("│", C.DIM))
    print(colored("└" + "─" * WIDTH + "┘", C.DIM))

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

    print()
    print(colored("  ACCOUNT", C.BOLD + C.WHITE))
    print(colored("  " + "─" * 40, C.DIM))
    print(f"  Balance:    {fmt_money(balance)}")
    print(f"  P&L:        {fmt_money(pnl)}  ({fmt_pct(pnl_pct)})")
    print(f"  Peak:       ${peak:,.2f}  (drawdown {fmt_pct(-drawdown)})")
    print(f"  Record:     {colored(str(wins), C.GREEN)}W / {colored(str(losses), C.RED)}L" +
          (f"  ({win_rate:.0f}% win rate)" if total > 0 else ""))

def positions_section(sim):
    positions = sim.get("positions", {})
    print()
    if not positions:
        print(colored("  OPEN POSITIONS", C.BOLD + C.WHITE))
        print(colored("  " + "─" * 40, C.DIM))
        print(colored("  No open positions", C.DIM))
        return

    print(colored(f"  OPEN POSITIONS ({len(positions)})", C.BOLD + C.WHITE))
    print(colored("  " + "─" * 62, C.DIM))

    for mid, pos in positions.items():
        city = pos.get("city", "?").upper()
        date = pos.get("date", "?")
        entry = pos.get("entry_price", 0)
        cost = pos.get("cost", 0)
        shares = pos.get("shares", 0)
        edge = pos.get("edge", 0)
        conf = pos.get("confidence", "?")
        opened = pos.get("opened_at", "")
        question = pos.get("question", "")

        # Extract the temp target from question
        target = question.split("be ")[-1].split(" on ")[0] if "be " in question else "?"

        potential_win = shares * (1.0 - entry) - cost
        potential_loss = cost

        print()
        print(f"  {colored(city, C.BOLD + C.CYAN)}  {date}  {time_until(date)}")
        print(f"  {colored(question, C.DIM)}")
        print(f"  Entry: ${entry:.3f}  |  Cost: ${cost:.2f}  |  Shares: {shares:.0f}")
        print(f"  Edge: {fmt_edge(edge)}  |  Confidence: {fmt_confidence(conf)}")
        print(f"  If YES:  {colored(f'+${potential_win:.2f}', C.GREEN)}  |  If NO:  {colored(f'-${potential_loss:.2f}', C.RED)}")
        print(f"  Opened: {time_ago(opened)}")

def signals_section():
    signals = load_recent_signals(8)
    print()
    print(colored("  RECENT SIGNALS", C.BOLD + C.WHITE))
    print(colored("  " + "─" * 62, C.DIM))

    if not signals:
        print(colored("  No signals logged yet", C.DIM))
        return

    for sig in reversed(signals):
        sig_type = sig.get("type", "scan")
        city = sig.get("city", "?").upper()
        ts = sig.get("timestamp", "")
        ago = time_ago(ts) if ts else "?"

        if sig_type == "resolution":
            result = sig.get("result", "?")
            pnl = sig.get("pnl", 0)
            icon = "🟢" if result == "win" else "🔴"
            pnl_str = fmt_money(pnl)
            print(f"  {icon} {colored(ago, C.DIM):>12s}  {city:<10s}  RESOLVED {result.upper()}  {pnl_str}")

        elif sig_type in ("take_profit", "stop_loss"):
            pnl = sig.get("pnl", 0)
            icon = "🟡"
            label = "TAKE PROFIT" if sig_type == "take_profit" else "STOP LOSS"
            pnl_str = fmt_money(pnl)
            print(f"  {icon} {colored(ago, C.DIM):>12s}  {city:<10s}  {label}  {pnl_str}")

        elif sig_type == "entry":
            cost = sig.get("cost", 0)
            edge = sig.get("edge", 0)
            print(f"  🔵 {colored(ago, C.DIM):>12s}  {city:<10s}  ENTRY  ${cost:.2f}  edge {fmt_edge(edge)}")

        else:
            # scan/signal
            edge = sig.get("edge", sig.get("best_edge", 0))
            prob = sig.get("model_prob", sig.get("hit_prob", 0))
            if isinstance(prob, (int, float)) and prob > 0:
                print(f"  ⚪ {colored(ago, C.DIM):>12s}  {city:<10s}  SCAN  prob {prob:.0%}  edge {fmt_edge(edge)}")

def footer(sim):
    total_trades = sim.get("total_trades", 0)
    n_positions = len(sim.get("positions", {}))
    cost_in_play = sum(p.get("cost", 0) for p in sim.get("positions", {}).values())

    print()
    print(colored("  " + "─" * 62, C.DIM))
    print(f"  Total trades: {total_trades}  |  Open: {n_positions}  |  Capital at risk: ${cost_in_play:,.2f}")
    print(colored(f"  Scans hourly via cron  |  Quarter-Kelly sizing  |  12 cities", C.DIM))
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
