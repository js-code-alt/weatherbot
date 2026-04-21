#!/usr/bin/env python3
"""Phase 4: Simulate bot trading against real Polymarket price history.

For each event, walks the price-history chronologically and replays the
bot's decision logic at every observed price tick. Applies:
 - Edge threshold (configurable, default 15%)
 - Quarter-Kelly sizing (bounded $5-$100/bet)
 - Take-profit at 75¢ (hold to resolution if never triggered)
 - One position per (bucket, event) max
 - $10K starting bankroll

Two modes:
  --mode hindsight   Use Phase 3 bot_probs directly (forecasts = perfect)
  --mode noisy       Add Gaussian noise to consensus before MC (realistic)

Limitations:
- Fill price = observed trade price at scan time (assumes liquidity)
- No slippage / bid-ask modeling
- Ladder logic not yet implemented (TODO) — single entry per event/bucket
- "Bot scan cadence" = every price observation (more frequent than hourly)

Usage:
  python simulate_bot.py                              # Full run, hindsight
  python simulate_bot.py --mode noisy --seed 1
  python simulate_bot.py --city dallas --edge 0.15
"""

import argparse
import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
DB_PATH = HERE / "prices.db"

# Match bot_v3 config
KELLY_FRACTION = 0.25     # quarter-Kelly
MIN_BET = 5.0
MAX_BET = 100.0
TAKE_PROFIT = 0.75
START_BANKROLL = 10_000.0
# Real-time forecast error std (from bot's observed D+0 data: 2.98°)
# Used in --mode noisy to degrade the hindsight forecast
REAL_FORECAST_NOISE_STD = 2.0  # conservative: real error minus hindsight error


def compute_kelly(model_prob, market_price):
    if model_prob <= market_price or market_price <= 0 or market_price >= 1:
        return 0.0
    p = model_prob
    q = 1.0 - p
    b = (1.0 - market_price) / market_price
    kelly = (p * b - q) / b
    return max(kelly, 0.0)


def kelly_bet_size(kelly_frac, bankroll):
    adjusted = kelly_frac * KELLY_FRACTION
    raw = adjusted * bankroll
    return round(min(max(raw, MIN_BET), MAX_BET), 2) if raw >= MIN_BET else 0


def monte_carlo_bucket_prob(consensus, sigma, bucket_low, bucket_high, n_sims=5000, df=5):
    count = 0
    for _ in range(n_sims):
        z = random.gauss(0, 1)
        v = random.gammavariate(df / 2.0, 2.0)
        s = consensus + sigma * (z / math.sqrt(v / df))
        if bucket_low <= -900:
            if s <= bucket_high: count += 1
        elif bucket_high >= 900:
            if s >= bucket_low: count += 1
        else:
            if bucket_low <= s <= bucket_high: count += 1
    return count / n_sims


class Simulation:
    def __init__(self, db_path, mode="hindsight", edge_threshold=0.15,
                 min_entry_price=0.0, seed=42, noise_std=REAL_FORECAST_NOISE_STD):
        self.con = sqlite3.connect(db_path)
        self.mode = mode
        self.edge_threshold = edge_threshold
        self.min_entry_price = min_entry_price
        self.noise_std = noise_std
        random.seed(seed)
        self.bankroll = START_BANKROLL
        self.open_positions = {}   # token_id -> {entry_price, shares, cost, event_date, city}
        self.trades = []           # closed trades for P&L tracking

    def get_event_data(self, city_filter=None):
        """Get all events ordered by date, with markets + prices."""
        where = "WHERE city=?" if city_filter else ""
        params = (city_filter,) if city_filter else ()
        events = self.con.execute(
            f"SELECT DISTINCT city, event_date FROM markets {where} ORDER BY event_date, city",
            params
        ).fetchall()
        return events

    def get_markets_for_event(self, city, date):
        """Return list of markets with forecast + bucket info."""
        rows = self.con.execute("""
            SELECT m.token_id, m.question, m.bucket_low, m.bucket_high, m.final_price,
                   f.consensus, f.sigma_floored
            FROM markets m
            LEFT JOIN forecasts f ON f.city=m.city AND f.event_date=m.event_date
            WHERE m.city=? AND m.event_date=?
            ORDER BY m.bucket_low
        """, (city, date)).fetchall()
        return rows

    def get_price_series(self, token_id):
        """All (ts, p) tuples sorted ascending."""
        return self.con.execute(
            "SELECT ts, p FROM prices WHERE token_id=? ORDER BY ts ASC",
            (token_id,)
        ).fetchall()

    def bot_prob_for_bucket(self, consensus, sigma, bucket_low, bucket_high):
        """Compute bot's probability estimate for a bucket, optionally with noise."""
        if consensus is None or sigma is None:
            return None
        if self.mode == "hindsight":
            return monte_carlo_bucket_prob(consensus, sigma, bucket_low, bucket_high)
        else:  # noisy
            # Add a single realization of forecast noise to consensus
            noisy_consensus = consensus + random.gauss(0, self.noise_std)
            # Widen sigma to reflect our uncertainty about the noisy forecast
            effective_sigma = math.sqrt(sigma * sigma + self.noise_std * self.noise_std)
            return monte_carlo_bucket_prob(noisy_consensus, effective_sigma, bucket_low, bucket_high)

    def simulate_event(self, city, date):
        """Process a single event: scan chronologically, open/close positions."""
        markets = self.get_markets_for_event(city, date)
        if not markets:
            return

        # Pre-compute bot probs per bucket (fixed per event in hindsight; resampled in noisy)
        bot_probs = {}
        for tok, q, bl, bh, fin, cons, sig in markets:
            if bl is None or cons is None:
                continue
            bot_probs[tok] = self.bot_prob_for_bucket(cons, sig, bl, bh)

        # Merge all price series into a single timeline
        # timeline: list of (ts, token_id, price)
        timeline = []
        for tok, q, bl, bh, fin, cons, sig in markets:
            for ts, p in self.get_price_series(tok):
                timeline.append((ts, tok, p, bl, bh, q, fin, cons, sig))
        timeline.sort(key=lambda x: x[0])

        # Track latest price per token
        last_price = {}
        # Tokens we've already held and closed within this event — don't re-enter
        exited_tokens = set()

        for ts, tok, price, bl, bh, q, fin, cons, sig in timeline:
            last_price[tok] = price

            # Check take-profit on open positions
            for otok in list(self.open_positions):
                pos = self.open_positions[otok]
                # Only consider positions from this event
                if pos["city"] != city or pos["event_date"] != date:
                    continue
                # Don't TP on the same tick we entered (prevents 0-PnL flips)
                if ts == pos["entry_ts"]:
                    continue
                cur_p = last_price.get(otok, pos["entry_price"])
                if cur_p >= TAKE_PROFIT and cur_p > pos["entry_price"]:
                    payout = pos["shares"] * cur_p
                    pnl = payout - pos["cost"]
                    self.bankroll += payout
                    self.trades.append({
                        "city": pos["city"], "date": pos["event_date"],
                        "token": otok, "entry_price": pos["entry_price"],
                        "exit_price": cur_p, "cost": pos["cost"],
                        "shares": pos["shares"], "pnl": pnl,
                        "exit_type": "take_profit", "final_price": None,
                    })
                    del self.open_positions[otok]
                    exited_tokens.add(otok)

            # Check for new entry opportunity on THIS token
            if tok in self.open_positions:
                continue  # already have a position in this bucket
            if tok in exited_tokens:
                continue  # already held and closed this bucket this event
            # Don't enter at prices already at/above TP — no room to profit
            if price >= TAKE_PROFIT:
                continue
            # Apply configured MIN_ENTRY_PRICE floor (penny-longshot filter)
            if price < self.min_entry_price:
                continue

            bp = bot_probs.get(tok)
            if bp is None or bp <= price:
                continue
            edge = bp - price
            if edge < self.edge_threshold:
                continue

            # Compute Kelly stake
            kelly = compute_kelly(bp, price)
            stake = kelly_bet_size(kelly, self.bankroll)
            if stake <= 0 or stake > self.bankroll:
                continue

            shares = stake / price
            self.bankroll -= stake
            self.open_positions[tok] = {
                "entry_price": price, "shares": shares, "cost": stake,
                "event_date": date, "city": city, "entry_ts": ts,
                "bot_prob": bp, "edge": edge,
            }

        # End of event: resolve any remaining positions at final_price (0 or 1)
        fin_by_tok = {m[0]: m[4] for m in markets}
        for otok in list(self.open_positions):
            pos = self.open_positions[otok]
            if pos["city"] != city or pos["event_date"] != date:
                continue
            final = fin_by_tok.get(otok)
            if final is None:
                # Unresolved; close at last seen price
                cur_p = last_price.get(otok, pos["entry_price"])
                payout = pos["shares"] * cur_p
            else:
                payout = pos["shares"] * final  # 0 or 1 per share
            pnl = payout - pos["cost"]
            self.bankroll += payout
            self.trades.append({
                "city": pos["city"], "date": pos["event_date"],
                "token": otok, "entry_price": pos["entry_price"],
                "exit_price": final if final is not None else last_price.get(otok),
                "cost": pos["cost"], "shares": pos["shares"], "pnl": pnl,
                "exit_type": "resolution", "final_price": final,
            })
            del self.open_positions[otok]

    def run(self, city_filter=None):
        events = self.get_event_data(city_filter)
        print(f"Events to simulate: {len(events)}", file=sys.stderr)
        for i, (city, date) in enumerate(events):
            self.simulate_event(city, date)
            if (i + 1) % 50 == 0:
                print(f"  {i+1}/{len(events)} events | bankroll=${self.bankroll:,.0f} | {len(self.trades)} trades",
                      file=sys.stderr)
        print(f"  Final: bankroll=${self.bankroll:,.0f} | {len(self.trades)} trades", file=sys.stderr)

    def report(self):
        t = self.trades
        wins = [x for x in t if x["pnl"] > 0]
        losses = [x for x in t if x["pnl"] < 0]
        breakeven = [x for x in t if x["pnl"] == 0]
        total_pnl = sum(x["pnl"] for x in t)
        tp_trades = [x for x in t if x["exit_type"] == "take_profit"]
        res_trades = [x for x in t if x["exit_type"] == "resolution"]

        print(f"\n=== SIMULATION RESULTS ({self.mode}, edge≥{self.edge_threshold:.0%}) ===")
        print(f"Starting bankroll: ${START_BANKROLL:,.0f}")
        print(f"Ending bankroll:   ${self.bankroll:,.2f}")
        print(f"Total P&L:         ${total_pnl:+,.2f} ({total_pnl/START_BANKROLL*100:+.1f}%)")
        print(f"\nTrade count: {len(t)}")
        print(f"  Winners: {len(wins)} (avg +${sum(x['pnl'] for x in wins)/max(len(wins),1):.2f})")
        print(f"  Losers:  {len(losses)} (avg ${sum(x['pnl'] for x in losses)/max(len(losses),1):.2f})")
        print(f"  Break-even: {len(breakeven)}")
        print(f"  Win rate: {len(wins)/len(t)*100:.1f}%")
        print(f"\nExit type:")
        print(f"  Take-profit: {len(tp_trades)} (P&L ${sum(x['pnl'] for x in tp_trades):+,.0f})")
        print(f"  Resolution:  {len(res_trades)} (P&L ${sum(x['pnl'] for x in res_trades):+,.0f})")

        # By city
        by_city = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0})
        for x in t:
            by_city[x["city"]]["n"] += 1
            by_city[x["city"]]["pnl"] += x["pnl"]
            if x["pnl"] > 0:
                by_city[x["city"]]["w"] += 1
        print(f"\nPer-city:")
        print(f"  {'City':<10} {'Trades':>7} {'Wins':>5} {'WR%':>5} {'P&L':>12}")
        for city in sorted(by_city, key=lambda c: -by_city[c]["pnl"]):
            s = by_city[city]
            wr = s["w"] / s["n"] * 100 if s["n"] else 0
            print(f"  {city:<10} {s['n']:>7} {s['w']:>5} {wr:>5.1f} ${s['pnl']:>+10,.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["hindsight", "noisy"], default="hindsight")
    ap.add_argument("--edge", type=float, default=0.15)
    ap.add_argument("--min-entry-price", type=float, default=0.0,
                    help="Skip markets below this price (penny-longshot filter)")
    ap.add_argument("--city", type=str, default=None)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--noise-std", type=float, default=REAL_FORECAST_NOISE_STD,
                    help="Gaussian noise std added to consensus in noisy mode (°)")
    args = ap.parse_args()

    sim = Simulation(DB_PATH, mode=args.mode, edge_threshold=args.edge,
                     min_entry_price=args.min_entry_price,
                     seed=args.seed, noise_std=args.noise_std)
    sim.run(city_filter=args.city)
    sim.report()


if __name__ == "__main__":
    main()
