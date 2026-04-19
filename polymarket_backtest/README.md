# Polymarket Backtest (WIP)

Historical backtesting of the weather bot against real Polymarket prices.

## Status

- [x] Phase 1: Survey coverage — done
- [x] Phase 2: Download full price histories — done (1.6M price points, 327 MB SQLite)
- [ ] Phase 3: Historical forecast reconstruction (via Open-Meteo archive API)
- [ ] Phase 4: Simulate bot decisions against real prices + compute P&L

## Phase 1 results (30-day survey)

- **316 weather events** across 12 cities (29-30 per city except Phoenix 0, Denver 25)
- **3,476 individual markets** (temperature buckets)
- **$58M total volume** — markets are liquid
- **Phoenix missing** — slug pattern likely differs; worth investigating
- Phase 1 used `/events?slug=highest-temperature-in-{city}-on-{month}-{day}-{year}`

## Data sources

| Source | Use |
|---|---|
| `https://gamma-api.polymarket.com/events?slug=...` | Event metadata, markets, buckets |
| `https://clob.polymarket.com/prices-history?market={token}&interval=max` | Historical price trajectories per market token |
| Open-Meteo historical forecast archive | What forecast the bot would have seen (approximate) |
| Open-Meteo archive (actuals) | Actual daily high temp for resolution |

## Limitations (known upfront)

1. **Forecast hindsight bias** — Open-Meteo's historical forecast API is roughly the model's final call for that day, not necessarily what was live at scan time.
2. **No ensemble std** — archive API may not expose ensemble spread; sigma must be estimated from model spread alone.
3. **Liquidity simulation** — historical trades give clearing prices, not bid/ask at bot's decision moment.
4. **Slug gaps** — Phoenix and some Denver days don't match the standard slug pattern.

## Files

- `fetch_weather_markets.py` — Phase 1 event fetcher
- `download_prices.py` — Phase 2 price history downloader
- `events.jsonl` — 316 events with metadata + markets (gitignored)
- `prices.db` — SQLite: markets + 1.6M price points (gitignored, 327 MB)

## Phase 2 results

- **3,476 YES tokens** downloaded (NO tokens skipped — complementary)
- **1,645,880 price points** (avg ~470 per market)
- **100% bucket parsing** success from question text
- **316 winning buckets** (exactly 1 per event, as expected)
- Zero fetch errors

## Schema

```sql
markets (
    token_id TEXT PRIMARY KEY,
    event_slug, event_date, city,
    question, condition_id,
    outcome,                    -- "Yes" or "No"
    bucket_low, bucket_high,    -- parsed temp range (°F)
    final_price,                -- 0 or 1 (resolution)
    volume, closed
)

prices (
    token_id, ts, p,            -- unix ts, price
    PRIMARY KEY (token_id, ts)
)
```
