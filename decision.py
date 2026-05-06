"""Pure ladder/decision logic shared by the live bot and backtests.

This module is deliberately I/O-free: no network calls, no file writes, no
prints. Its only dependency is the standard library. Callers build a
``LadderConfig`` from their own constants/config and call ``evaluate_ladder``;
the result describes which rungs to enter and why.

The shape and numerical output of ``evaluate_ladder`` exactly matches what
``bot_v3.build_ladder`` previously produced — this is a lift-and-extract, not
a redesign. See ``claudedocs/decision_core_audit.md`` for the divergences
between this canonical version and the older copies in ``backtest.py`` /
``polymarket_backtest/simulate_bot.py``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple


# =============================================================================
# CONFIG
# =============================================================================


@dataclass(frozen=True)
class LadderConfig:
    """Tunables for ``evaluate_ladder``. Callers pass their own values.

    Defaults mirror ``bot_v3.py`` constants as of 2026-05-07 so a caller that
    wants "live behavior" can use ``LadderConfig()`` directly.
    """

    min_edge: float = 0.05
    single_min_edge: float = 0.15
    max_price: float = 0.45
    min_entry_price: float = 0.05
    market_extremity_price: float = 0.10
    market_extremity_edge_gap: float = 0.20
    max_ladder_rungs: int = 5
    ladder_budget: float = 0.25  # fraction of bankroll per ladder
    kelly_fraction: float = 0.25  # quarter-Kelly
    min_bet: float = 5.0
    max_bet: float = 100.0
    allowed_confidences: Optional[Tuple[str, ...]] = None  # e.g. ("HIGH","MEDIUM")


# =============================================================================
# PURE HELPERS
# =============================================================================


def compute_kelly(model_prob: float, market_price: float) -> float:
    """Raw (un-fractional) Kelly stake fraction. Returns 0 when there's no edge."""
    if model_prob <= market_price or market_price <= 0 or market_price >= 1:
        return 0.0
    p = model_prob
    q = 1.0 - p
    b = (1.0 - market_price) / market_price
    kelly = (p * b - q) / b
    return max(kelly, 0.0)


def kelly_bet_size(kelly_frac: float, bankroll: float, config: LadderConfig) -> float:
    """Quarter-Kelly bet size (or ``config.kelly_fraction``), clamped to bounds."""
    adjusted = kelly_frac * config.kelly_fraction
    raw = adjusted * bankroll
    return round(min(max(raw, config.min_bet), config.max_bet), 2)


def classify_confidence(edge: float, model_spread: float) -> Optional[str]:
    """Bucket an edge into HIGH/MEDIUM/LOW based on edge size and model agreement."""
    if edge >= 0.40 and model_spread < 3.0:
        return "HIGH"
    elif edge >= 0.25:
        return "MEDIUM"
    elif edge >= 0.15:
        return "LOW"
    return None  # below threshold


def classify_bucket_type(question: Optional[str], rng: Optional[Tuple[float, float]] = None) -> str:
    """Classify a Polymarket temperature bucket by shape (exact / range / or_higher / or_below)."""
    if question:
        if re.search(r'or below', question, re.IGNORECASE):
            return "or_below"
        if re.search(r'or higher', question, re.IGNORECASE):
            return "or_higher"
        if re.search(r'between ', question, re.IGNORECASE):
            return "range"
        if re.search(r'be ' + r'(-?\d+(?:\.\d+)?)' + r'[°]?[FC] on', question, re.IGNORECASE):
            return "exact"

    if rng:
        t_low, t_high = rng
        if t_low <= -900:
            return "or_below"
        if t_high >= 900:
            return "or_higher"
        if abs((t_high - t_low) - 1.0) < 1e-9:
            return "exact"
    return "range"


# =============================================================================
# LADDER EVALUATION
# =============================================================================


def evaluate_ladder(
    consensus: float,
    buckets: Dict[str, Tuple[float, float]],
    bucket_probs: Dict[str, float],
    market_prices: Dict[str, float],
    bankroll: float,
    model_spread: float,
    config: LadderConfig,
    bucket_types: Optional[Dict[str, str]] = None,
) -> List[dict]:
    """Build a ladder of 2-5 adjacent underpriced buckets around consensus.

    Returns a list of ladder rungs sorted by proximity to consensus, or an
    empty list. Each rung is a dict with keys::

        bucket_key, range, bucket_type, model_prob, market_price, edge,
        kelly_raw, ev_per_dollar, distance, confidence, bet_size,
        combined_hit_prob

    This is a pure function: no I/O, no globals. ``consensus`` is the weighted
    forecast temperature; ``buckets`` maps a bucket key to ``(t_low, t_high)``;
    ``bucket_probs`` is the Monte Carlo result; ``market_prices`` is the YES
    price per bucket; ``model_spread`` is max(temps)-min(temps) across models.
    """
    candidates: List[dict] = []
    for bkey, prob in bucket_probs.items():
        price = market_prices.get(bkey, 1.0)
        if price <= 0 or price >= 1:
            continue
        if price < config.min_entry_price:
            continue  # penny-priced longshots: high variance, small forecast errors dominate
        if price > config.max_price:
            continue  # market already overpriced relative to our tolerance
        edge = prob - price
        if edge < config.min_edge:
            continue
        if edge < config.single_min_edge:
            continue  # config-driven primary edge gate
        if price <= config.market_extremity_price and edge >= config.market_extremity_edge_gap:
            continue  # extreme market consensus — don't fight a 10¢ market with a 30% prediction

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
        if config.allowed_confidences and confidence not in config.allowed_confidences:
            continue

        ev_per_dollar = (prob * (1.0 / price - 1.0) - (1.0 - prob))

        candidates.append({
            "bucket_key": bkey,
            "range": buckets[bkey],
            "bucket_type": (bucket_types or {}).get(bkey, classify_bucket_type(None, buckets[bkey])),
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
    ladder = candidates[: config.max_ladder_rungs]

    # Allocate capital proportional to edge strength
    total_edge = sum(r["edge"] for r in ladder)
    budget = bankroll * config.ladder_budget
    for rung in ladder:
        frac = rung["edge"] / total_edge
        raw_bet = frac * budget
        rung["bet_size"] = round(min(max(raw_bet, config.min_bet), config.max_bet), 2)

    # Combined hit probability: 1 - product(1 - prob_i)
    combined_prob = 1.0 - math.prod(1.0 - r["model_prob"] for r in ladder)
    for rung in ladder:
        rung["combined_hit_prob"] = round(combined_prob, 4)

    return ladder
