# 🌤 Weather Trading Bot — Polymarket

Automated weather market trading bot for Polymarket. Finds mispriced temperature outcomes using real station data from NWS and Visual Crossing.

No SDK. No black box. Pure Python.

---

## Versions

### `bot_v1.py` — Base Bot

The foundation. Scans 6 US cities, fetches forecasts from NWS, finds matching temperature buckets on Polymarket, and enters trades when the market price is below the entry threshold.

No math, no complexity. Just the core logic — good for understanding how the system works.

### `bot_v2.py` — Kelly + EV Edition

Everything in v1, plus:

- **Expected Value** — skips trades where the math doesn't work
- **Kelly Criterion** — sizes positions based on edge strength, not a flat %
- **Auto-exit** — closes positions when price hits the exit threshold
- **Live dashboard** — updates `simulation.json` so the dashboard stays current

~400 lines of pure Python.

### `bot_v3.py` — Auto-Cycle + Forecast Monitoring (current)

Everything in v2, plus:

- **Auto-cycle** — scans every hour, synchronized to the clock (:00, :01, :02...)
- **Forecast monitor** — checks every 60 seconds, closes positions if forecast changes or EV goes negative
- **Real station data** — NWS hourly observations + Visual Crossing for today's actual readings
- **Correct airport coordinates** — each city mapped to the exact station Polymarket resolves on
- **Liquidity-aware sizing** — reduces position size for low-volume markets, hard cap at $20 for markets under $1k volume
- **Slippage simulation** — entry price reflects real market impact
- **16 cities** — 6 US cities via NWS, 10 international via Open-Meteo

---

## How It Works

Polymarket runs markets like "Will the highest temperature in Chicago be between 46–47°F on March 7?" These markets are often mispriced — the forecast says 78% likely but the market is trading at 8 cents.

The bot:

1. Fetches forecasts from NWS (US cities) and Open-Meteo (international) using airport coordinates
2. Combines real station observations with hourly forecast to get the true daily maximum
3. Finds the matching temperature bucket on Polymarket
4. Calculates Expected Value — skips the trade if EV is below threshold
5. Calculates Kelly Criterion — sizes position based on edge strength
6. Adjusts position size for market liquidity and simulates slippage
7. Monitors all open positions every 60 seconds — closes if forecast shifts

---

## Why Airport Coordinates Matter

Most bots use city center coordinates. That's wrong.

Every Polymarket weather market resolves on a specific airport station. NYC resolves on LaGuardia (KLGA), Dallas on Love Field (KDAL) — not DFW. The difference between city center and airport can be 3–8°F. On markets with 1–2°F buckets, that's the difference between the right trade and a guaranteed loss.

| City | Station | Airport |
|------|---------|---------|
| NYC | KLGA | LaGuardia |
| Chicago | KORD | O'Hare |
| Miami | KMIA | Miami Intl |
| Dallas | KDAL | Love Field |
| Seattle | KSEA | Sea-Tac |
| Atlanta | KATL | Hartsfield |

---

## Kelly + EV Logic

**Expected Value** — is this trade mathematically profitable?

```
EV = (our_probability × net_payout) − (1 − our_probability)
```

**Kelly Criterion** — how much of the balance to bet?

```
Kelly % = (p × b − q) / b
```

We use fractional Kelly (25%) and cap each position at 5% of balance.

---

## Installation

```bash
git clone https://github.com/alteregoeth-ai/weatherbot
cd weatherbot
pip install requests pytz
```

Add your settings to `config.json`:

```json
{
  "entry_threshold": 0.15,
  "exit_threshold": 0.45,
  "max_position_pct": 0.05,
  "locations": "nyc,chicago,miami,dallas,seattle,atlanta",
  "max_trades_per_run": 5,
  "min_hours_to_resolution": 2
}
```

---

## Usage

### bot_v1.py

```bash
python bot_v1.py          # paper mode — shows signals, no trades
python bot_v1.py --live   # simulates trades with $1,000 balance
python bot_v1.py --reset  # reset balance back to $1,000
```

### bot_v3.py

```bash
python bot_v3.py --live       # run bot (entry scanner + forecast monitor)
python bot_v3.py --positions  # show open positions and PnL
python bot_v3.py --reset      # reset simulation back to $1,000
```

---

## Dashboard

Run a local server in the bot folder:

```bash
python -m http.server 8000
```

Then open `http://localhost:8000/sim_dashboard_repost.html` in your browser.

- Balance chart with history
- Open positions with Kelly %, EV, and current PnL
- Full trade history with ENTRY/SELL labels
- Refreshes automatically every 10 seconds

---

## Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `entry_threshold` | `0.15` | Buy below this price |
| `exit_threshold` | `0.45` | Sell above this price |
| `max_position_pct` | `0.05` | Max position size as % of balance |
| `locations` | `nyc,...` | Cities to scan (comma separated) |
| `max_trades_per_run` | `5` | Max new trades per scan |
| `min_hours_to_resolution` | `2` | Skip if resolves too soon |

---

## APIs Used

| API | Auth | Purpose |
|-----|------|---------|
| NWS (api.weather.gov) | None | US city forecasts + station observations |
| Open-Meteo | None | International city forecasts |
| Visual Crossing | Free key | Today's actual hourly readings by station |
| Polymarket Gamma | None | Market data |
| Polymarket CLOB | Wallet key | Live trading (optional) |

---

## Live Trading

The bot runs in simulation mode by default. To execute real trades, add Polymarket CLOB integration:

```bash
pip install py-clob-client
```

Then replace the paper mode block in `bot_v3.py` with your CLOB buy function. Full guide in the article linked below.

---

## Disclaimer

This is not financial advice. Prediction markets carry real risk. Run the simulation thoroughly before committing real capital.
