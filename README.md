# 🌤 WeatherBet — Polymarket Weather Trading Bot

Automated weather market trading bot for Polymarket. Finds mispriced temperature outcomes using real forecast data from multiple sources across 20 cities worldwide.

No SDK. No black box. Pure Python.

---

## Current version: `bot_v3.py`

The active bot. Features:
- **11 cities** (US + international, Seoul disabled pending bias retune)
- **5 forecast sources** — ECMWF, GFS, ICON, NWS (US), GFS Ensemble (mean + std)
- **Weighted consensus** — per-city bias correction applied before weighting
- **Monte Carlo probability engine** — Student's t sampling, sigma-floored by horizon
- **Quarter-Kelly sizing** — $5–$100 per rung, ladder up to 5 rungs
- **Edge gates** — SINGLE_MIN_EDGE (default 0.25) + MIN_ENTRY_PRICE (default 0.05)
- **Take-profit at 75¢** — no stop-loss (removed; binary markets cap downside at stake)
- **Full data storage** — forecast snapshots, trades, and resolutions logged to NDJSON

---

## How It Works

Polymarket runs markets like "Will the highest temperature in Chicago be between 46–47°F on March 7?" These markets are often mispriced — the forecast says 78% likely but the market is trading at 8 cents.

The bot:
1. Fetches forecasts from ECMWF and HRRR via Open-Meteo (free, no key required)
2. Gets real-time observations from METAR airport stations
3. Finds the matching temperature bucket on Polymarket
4. Calculates Expected Value — only enters if the math is positive
5. Sizes the position using fractional Kelly Criterion
6. Monitors stops every 10 minutes, full scan every hour
7. Auto-resolves markets by querying Polymarket API directly

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
| London | EGLC | London City |
| Tokyo | RJTT | Haneda |
| ... | ... | ... |

---

## Installation
```bash
git clone https://github.com/alteregoeth-ai/weatherbot
cd weatherbot
pip install requests
```

Create `config.json` in the project folder:
```json
{
  "balance": 10000.0,
  "max_bet": 20.0,
  "min_ev": 0.05,
  "max_price": 0.45,
  "min_volume": 2000,
  "min_hours": 2.0,
  "max_hours": 72.0,
  "kelly_fraction": 0.25,
  "max_slippage": 0.03,
  "scan_interval": 3600,
  "calibration_min": 30,
  "vc_key": "YOUR_VISUAL_CROSSING_KEY"
}
```

Get a free Visual Crossing API key at visualcrossing.com — used to fetch actual temperatures after market resolution.

---

## Usage
```bash
python bot_v3.py           # paper-trading scan (no trades executed)
python bot_v3.py --live    # scan and execute trades against virtual balance
python dashboard.py        # show balance, open positions, recent trades
python analytics.py all    # calibration + P&L + sigma floor analysis
python backtest.py         # 30-day historical calibration backtest
```

Polymarket backtest framework (optional, heavier):
```bash
cd polymarket_backtest
python fetch_weather_markets.py --days 30
python download_prices.py
python reconstruct_forecasts.py
python simulate_bot.py --mode noisy --edge 0.25
```

---

## Data Storage

All data is saved to `data/markets/` — one JSON file per market. Each file contains:
- Hourly forecast snapshots (ECMWF, HRRR, METAR)
- Market price history
- Position details (entry, stop, PnL)
- Final resolution outcome

This data is used for self-calibration — the bot learns forecast accuracy per city over time and adjusts position sizing accordingly.

---

## APIs Used

| API | Auth | Purpose |
|-----|------|---------|
| Open-Meteo | None | ECMWF + HRRR forecasts |
| Aviation Weather (METAR) | None | Real-time station observations |
| Polymarket Gamma | None | Market data |
| Visual Crossing | Free key | Historical temps for resolution |

---

## Disclaimer

This is not financial advice. Prediction markets carry real risk. Run the simulation thoroughly before committing real capital.
