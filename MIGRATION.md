# Migration: backtest.py & polymarket_backtest/simulate_bot.py → decision.py

This branch extracts the live bot's ladder/Kelly/confidence logic into a pure
module, `decision.py`. `bot_v3.py` is wired up; the two backtest paths still
have their own inline copies. This document is the recipe for adopting the
shared module in a follow-up PR.

Read `claudedocs/decision_core_audit.md` first — it lists the concrete
divergences each migration has to handle on purpose, not by accident.

## Why migrate in a follow-up, not now

Three different ladder/decision flows already disagree on:

- σ floor at D+0 (1.3 in `backtest.py` vs 3.0 in `bot_v3.py`)
- whether NWS contributes to consensus (live yes, `backtest.py` no)
- whether market-extremity / SINGLE_MIN_EDGE / MAX_PRICE gates exist
  (`simulate_bot.py` has none of them — it predates PR #15 and earlier work)

Forcing them onto a single shared function in one branch *changes historical
backtest numbers*. That's a deliberate decision, not a refactor — it belongs
in its own PR with its own review.

## Migration A — `polymarket_backtest/simulate_bot.py`

This is the higher-value migration: it actually simulates entries, so it
benefits from the shared ladder.

### Step 1: import + delete duplicates

```python
# Top of simulate_bot.py
from decision import (
    LadderConfig,
    evaluate_ladder,
    compute_kelly as _compute_kelly,    # if you want the canonical helper
    kelly_bet_size as _kelly_bet_size,
)
```

Delete:
- `compute_kelly()` (lines 52–59)
- `kelly_bet_size()` (lines 62–65)
- The local `KELLY_FRACTION / MIN_BET / MAX_BET` constants (push them into a
  `LadderConfig` instead)

### Step 2: build the per-event LadderConfig

```python
self.config = LadderConfig(
    min_edge=args.edge,                  # was self.edge_threshold
    single_min_edge=args.edge,           # historically simulate_bot only had ONE edge gate
    max_price=0.99,                      # simulate_bot had no max-price filter
    min_entry_price=args.min_entry_price,
    market_extremity_price=0.0,          # disabled in old simulator (set 0.10 to enable)
    market_extremity_edge_gap=999.0,     # disabled
    max_ladder_rungs=1,                  # simulate_bot is single-rung; bump to 5 to enable ladder
    ladder_budget=0.25,
    kelly_fraction=0.25,
    min_bet=5.0,
    max_bet=100.0,
    allowed_confidences=None,            # simulator had no confidence filter
)
```

The numbers above reproduce *current* `simulate_bot.py` behavior. Tightening
gates to match live (`max_price=0.45`, extremity guard on, allowed
confidences = HIGH/MEDIUM) is a separate behavior change to land in its own
commit so the diff in P&L is attributable.

### Step 3: replace the inline entry decision

In `simulate_event()`, the block that does

```python
if bp is None or bp <= price: continue
edge = bp - price
if edge < self.edge_threshold: continue
kelly = compute_kelly(bp, price)
stake = kelly_bet_size(kelly, self.bankroll)
```

becomes a call to `evaluate_ladder(...)` against a one-bucket dict. Note
`evaluate_ladder` runs the full filter chain — edge, price bounds, extremity,
confidence — so the easiest port is:

```python
# Per-tick, per-token:
buckets    = {tok: (bl, bh)}
probs      = {tok: bp}
prices     = {tok: price}
ladder = evaluate_ladder(
    consensus=cons, buckets=buckets, bucket_probs=probs, market_prices=prices,
    bankroll=self.bankroll, model_spread=0.0,  # spread unknown in this sim
    config=self.config,
)
if not ladder: continue
rung = ladder[0]
stake = rung["bet_size"]
```

### Step 4: keep the per-tick loop and the take-profit logic

`evaluate_ladder` only handles entries. Take-profit at 75¢, position
bookkeeping, and end-of-event resolution all stay in `simulate_bot.py`.

### Step 5: validate

Run the existing simulator before and after; trade-count and P&L should be
*identical* if the LadderConfig in step 2 is faithful to current behavior.
Diff the per-trade NDJSON output if available.

## Migration B — `backtest.py`

`backtest.py` doesn't actually build a ladder; it only checks calibration.
The migration is much narrower.

### What to share

- `decision.compute_kelly`, `kelly_bet_size`, `classify_confidence` —
  currently absent from `backtest.py`, so this is "future-proofing" if the
  backtest grows to evaluate trade decisions.
- `decision.classify_bucket_type` — `backtest.py` doesn't classify, but if
  it ever wants per-bucket-type calibration breakdowns this is the canonical
  function.

### What NOT to share yet

- `compute_consensus` — `backtest.py`'s version *omits NWS* and has a
  *stale CITY_BIASES table*. Sharing means choosing one canonical version,
  which changes historical numbers. Out of scope for the refactor.
- `monte_carlo_bucket_probs` — `backtest.py` has slightly looser edge-bucket
  detection (`< -900` / `> 900` rather than exactly `-999` / `999`). Fixable
  but still a behavior choice.
- `SIGMA_FLOORS` — backtest's D+0 = 1.3 vs live's D+0 = 3.0. This is the
  biggest divergence and should be reconciled in its own commit with notes.

### Recommended sequence

1. Copy CITY_BIASES from `bot_v3.py` into `backtest.py` and confirm tests +
   `--compute-biases` output don't drift. (1 commit.)
2. Replace `WEIGHTS` with the bot's full dict including NWS. The backtest
   never receives an `nws_temp` so this is a no-op today, but it stops the
   dicts drifting further. (1 commit.)
3. Decide and document the σ floor for D+0 in the backtest. (1 commit, with
   plot of recalibration before/after.)
4. *Then* import `compute_consensus / monte_carlo_bucket_probs` from a
   future shared `forecast.py` (out of scope here — needs its own audit).

## Tests

After each migration, run from repo root:

```
python3 -m unittest discover tests
```

If you add tests for `decision.py` directly (recommended), put them in
`tests/test_decision.py` and import `decision` at the top — no need for the
`bot_v3` import shim.

## Open questions for the follow-up PRs

- Should `LadderConfig` be loadable from `config.json` directly? Today
  `bot_v3._ladder_config()` reads its module globals.
- Should we expose `evaluate_ladder` results via a dataclass instead of a
  dict? Would catch typos at type-check time; would also mean updating every
  call site that indexes into the rung dict.
- Should `monte_carlo_bucket_probs` move into `decision.py`? It's pure and
  already duplicated three places. Held back because the three copies have
  slightly different edge-bucket conventions, which is exactly the kind of
  thing that needs deliberate reconciliation, not silent unification.
