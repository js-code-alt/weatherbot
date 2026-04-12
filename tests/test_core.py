"""Unit tests for bot_v3.py core logic — pure functions with no network calls."""

import sys
import os
import math
import json
import random
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add parent dir to path so we can import bot_v3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot_v3


class TestComputeConsensus(unittest.TestCase):
    """Test weighted consensus calculation across models."""

    def test_all_four_models(self):
        ens = {"mean": 72.0, "std": 1.5}
        consensus, sigma, spread, sources = bot_v3.compute_consensus(70.0, 74.0, 71.0, ens)
        # ECMWF=70*0.35 + GFS=74*0.25 + NWS=71*0.20 + ENS=72*0.20 = 71.6
        self.assertAlmostEqual(consensus, 71.6, places=1)
        self.assertEqual(len(sources), 4)
        self.assertAlmostEqual(spread, 4.0)  # 74-70
        self.assertAlmostEqual(sigma, 1.5)   # uses ensemble std

    def test_two_models_only(self):
        consensus, sigma, spread, sources = bot_v3.compute_consensus(70.0, 74.0, None, None)
        # Weights: ECMWF=0.35, GFS=0.25, total=0.60
        # (70*0.35 + 74*0.25) / 0.60 = (24.5+18.5)/0.60 = 71.67
        expected = (70.0 * 0.35 + 74.0 * 0.25) / 0.60
        self.assertAlmostEqual(consensus, round(expected, 1), places=1)
        self.assertEqual(len(sources), 2)
        self.assertAlmostEqual(spread, 4.0)
        # No ensemble -> sigma = spread/2
        self.assertAlmostEqual(sigma, 2.0)

    def test_single_model(self):
        consensus, sigma, spread, sources = bot_v3.compute_consensus(75.0, None, None, None)
        self.assertAlmostEqual(consensus, 75.0)
        self.assertEqual(spread, 0)
        self.assertEqual(sigma, 2.0)  # default fallback
        self.assertEqual(len(sources), 1)

    def test_no_models(self):
        consensus, sigma, spread, sources = bot_v3.compute_consensus(None, None, None, None)
        self.assertIsNone(consensus)
        self.assertIsNone(sigma)
        self.assertEqual(sources, {})

    def test_ensemble_only(self):
        ens = {"mean": 68.0, "std": 2.3}
        consensus, sigma, spread, sources = bot_v3.compute_consensus(None, None, None, ens)
        self.assertAlmostEqual(consensus, 68.0)
        self.assertAlmostEqual(sigma, 2.3)
        self.assertEqual(len(sources), 1)

    def test_weight_redistribution(self):
        """When NWS is missing, its 20% should be redistributed proportionally."""
        ens = {"mean": 72.0, "std": 1.0}
        consensus, _, _, sources = bot_v3.compute_consensus(70.0, 74.0, None, ens)
        # Available: ECMWF(0.35) + GFS(0.25) + ENS(0.20) = 0.80
        expected = (70.0*0.35 + 74.0*0.25 + 72.0*0.20) / 0.80
        self.assertAlmostEqual(consensus, round(expected, 1), places=1)
        self.assertNotIn("nws", sources)


class TestSigmaFloor(unittest.TestCase):
    """Test sigma floor application by forecast horizon."""

    def test_today_floor(self):
        self.assertEqual(bot_v3.apply_sigma_floor(0.5, 0), 0.8)

    def test_tomorrow_floor(self):
        self.assertEqual(bot_v3.apply_sigma_floor(0.5, 1), 1.2)

    def test_sigma_above_floor(self):
        self.assertEqual(bot_v3.apply_sigma_floor(3.0, 0), 3.0)

    def test_day3_floor(self):
        self.assertEqual(bot_v3.apply_sigma_floor(1.0, 3), 2.5)

    def test_unknown_horizon(self):
        # days_out > 4 falls back to 3.0
        self.assertEqual(bot_v3.apply_sigma_floor(1.0, 7), 3.0)


class TestMonteCarlo(unittest.TestCase):
    """Test Monte Carlo probability engine."""

    def test_consensus_centered_normal(self):
        """Without ensemble, MC uses normal dist. Most mass should be near consensus."""
        buckets = {
            "70-75": (70, 75),
            "75-80": (75, 80),
            "65-70": (65, 70),
        }
        random.seed(42)
        probs = bot_v3.monte_carlo_bucket_probs(72.5, 2.0, buckets, n_sims=50000)
        # 70-75 bucket should have highest prob (consensus=72.5 is inside)
        self.assertGreater(probs["70-75"], 0.5)
        # Total prob across these 3 should be < 1 (some sims land outside)
        total = sum(probs.values())
        self.assertLess(total, 1.0)

    def test_consensus_centered(self):
        """MC should center on consensus regardless of ensemble availability."""
        buckets = {"70-75": (70, 75), "75-80": (75, 80)}
        random.seed(42)
        probs = bot_v3.monte_carlo_bucket_probs(72.5, 1.5, buckets, n_sims=10000)
        # Consensus=72.5 is inside 70-75, so that bucket should dominate
        self.assertGreater(probs["70-75"], 0.7)

    def test_edge_buckets(self):
        """Test 'or below' and 'or higher' edge buckets."""
        buckets = {
            "-999-60": (-999, 60),
            "60-65": (60, 65),
            "80-999": (80, 999),
        }
        random.seed(42)
        probs = bot_v3.monte_carlo_bucket_probs(62.0, 2.0, buckets, n_sims=50000)
        # "60-65" should have high prob since consensus is 62
        self.assertGreater(probs["60-65"], 0.4)
        # "or below 60" should have some probability
        self.assertGreater(probs["-999-60"], 0.01)
        # "80 or higher" should be near zero
        self.assertLess(probs["80-999"], 0.001)

    def test_probabilities_sum_reasonable(self):
        """If buckets cover the full range, probs should sum close to 1."""
        buckets = {
            "-999-60": (-999, 60),
            "60-65": (60, 65),
            "65-70": (65, 70),
            "70-75": (70, 75),
            "75-999": (75, 999),
        }
        random.seed(42)
        probs = bot_v3.monte_carlo_bucket_probs(67.0, 3.0, buckets, n_sims=50000)
        total = sum(probs.values())
        self.assertAlmostEqual(total, 1.0, places=1)

    def test_wide_sigma_spreads_probability(self):
        """Larger sigma should spread probability across more buckets."""
        buckets = {"70-75": (70, 75), "65-70": (65, 70), "75-80": (75, 80)}
        random.seed(42)
        narrow = bot_v3.monte_carlo_bucket_probs(72.5, 1.0, buckets, n_sims=20000)
        random.seed(42)
        wide = bot_v3.monte_carlo_bucket_probs(72.5, 5.0, buckets, n_sims=20000)
        # Narrow sigma: center bucket should have more mass
        self.assertGreater(narrow["70-75"], wide["70-75"])


class TestKellyCriterion(unittest.TestCase):
    """Test Kelly sizing calculations."""

    def test_no_edge(self):
        self.assertEqual(bot_v3.compute_kelly(0.10, 0.15), 0.0)

    def test_equal_prob_price(self):
        self.assertEqual(bot_v3.compute_kelly(0.50, 0.50), 0.0)

    def test_positive_edge(self):
        k = bot_v3.compute_kelly(0.60, 0.10)
        # p=0.6, b=9, kelly = (0.6*9 - 0.4)/9 = (5.4-0.4)/9 = 0.556
        self.assertAlmostEqual(k, 0.556, places=2)
        self.assertGreater(k, 0)

    def test_zero_price(self):
        self.assertEqual(bot_v3.compute_kelly(0.50, 0.0), 0.0)

    def test_price_one(self):
        self.assertEqual(bot_v3.compute_kelly(0.50, 1.0), 0.0)

    def test_high_edge(self):
        k = bot_v3.compute_kelly(0.90, 0.05)
        self.assertGreater(k, 0.5)

    def test_bet_size_clamping(self):
        # Tiny Kelly -> $1 min
        size = bot_v3.kelly_bet_size(0.001, 1000)
        self.assertEqual(size, 1.0)

        # Huge Kelly -> $10 max
        size = bot_v3.kelly_bet_size(0.90, 100000)
        self.assertEqual(size, 10.0)

    def test_bet_size_normal(self):
        # kelly_frac=0.20, bankroll=1000 -> adjusted=0.20*0.25=0.05, raw=50 -> clamped to 10
        size = bot_v3.kelly_bet_size(0.20, 1000)
        self.assertEqual(size, 10.0)

        # kelly_frac=0.10, bankroll=200 -> adjusted=0.025, raw=5
        size = bot_v3.kelly_bet_size(0.10, 200)
        self.assertEqual(size, 5.0)


class TestConfidenceLevels(unittest.TestCase):
    """Test confidence classification."""

    def test_high_confidence(self):
        self.assertEqual(bot_v3.classify_confidence(0.45, 2.0), "HIGH")

    def test_high_requires_low_spread(self):
        # Edge >= 40% but spread >= 3 -> MEDIUM not HIGH
        self.assertEqual(bot_v3.classify_confidence(0.45, 4.0), "MEDIUM")

    def test_medium_confidence(self):
        self.assertEqual(bot_v3.classify_confidence(0.30, 5.0), "MEDIUM")

    def test_low_confidence(self):
        self.assertEqual(bot_v3.classify_confidence(0.16, 5.0), "LOW")

    def test_below_threshold(self):
        self.assertIsNone(bot_v3.classify_confidence(0.10, 5.0))

    def test_edge_at_boundary(self):
        self.assertEqual(bot_v3.classify_confidence(0.25, 5.0), "MEDIUM")
        self.assertEqual(bot_v3.classify_confidence(0.40, 2.9), "HIGH")
        self.assertEqual(bot_v3.classify_confidence(0.15, 1.0), "LOW")


class TestParseTempRange(unittest.TestCase):
    """Test Polymarket question parsing."""

    def test_between_fahrenheit(self):
        q = "Will the highest temperature be between 70-75°F on April 12?"
        self.assertEqual(bot_v3.parse_temp_range(q), (70.0, 75.0))

    def test_or_below(self):
        q = "Will it be 60°F or below?"
        self.assertEqual(bot_v3.parse_temp_range(q), (-999.0, 60.0))

    def test_or_higher(self):
        q = "Will it be 85°F or higher?"
        self.assertEqual(bot_v3.parse_temp_range(q), (85.0, 999.0))

    def test_celsius(self):
        q = "Will the highest temperature be between 20-25°C on April 12?"
        self.assertEqual(bot_v3.parse_temp_range(q), (20.0, 25.0))

    def test_exact_temp(self):
        q = "Will the highest temp be 72°F on April 12?"
        self.assertEqual(bot_v3.parse_temp_range(q), (72.0, 72.0))

    def test_none_input(self):
        self.assertIsNone(bot_v3.parse_temp_range(None))

    def test_no_match(self):
        self.assertIsNone(bot_v3.parse_temp_range("What is the weather?"))

    def test_negative_celsius(self):
        q = "Will the highest temperature be -5°C or below?"
        self.assertEqual(bot_v3.parse_temp_range(q), (-999.0, -5.0))


class TestBuildLadder(unittest.TestCase):
    """Test temperature ladder construction."""

    def setUp(self):
        self.buckets = {
            "65-70": (65, 70),
            "70-75": (70, 75),
            "75-80": (75, 80),
            "60-65": (60, 65),
            "80-999": (80, 999),
        }

    def test_basic_ladder(self):
        probs = {"65-70": 0.15, "70-75": 0.55, "75-80": 0.20, "60-65": 0.05, "80-999": 0.05}
        prices = {"65-70": 0.05, "70-75": 0.08, "75-80": 0.10, "60-65": 0.04, "80-999": 0.50}
        ladder = bot_v3.build_ladder(self.buckets, probs, prices, 72.5, 10000, 2.0)
        self.assertGreater(len(ladder), 0)
        # All rungs should have positive edge
        for rung in ladder:
            self.assertGreater(rung["edge"], 0)

    def test_no_edge_no_ladder(self):
        """If all prices are above model probs, no ladder."""
        probs = {"65-70": 0.05, "70-75": 0.10, "75-80": 0.05, "60-65": 0.02, "80-999": 0.01}
        prices = {"65-70": 0.50, "70-75": 0.50, "75-80": 0.50, "60-65": 0.50, "80-999": 0.50}
        ladder = bot_v3.build_ladder(self.buckets, probs, prices, 72.5, 10000, 2.0)
        self.assertEqual(len(ladder), 0)

    def test_ladder_max_rungs(self):
        """Ladder should not exceed MAX_LADDER_RUNGS."""
        probs = {k: 0.40 for k in self.buckets}
        prices = {k: 0.05 for k in self.buckets}
        ladder = bot_v3.build_ladder(self.buckets, probs, prices, 72.5, 10000, 1.0)
        self.assertLessEqual(len(ladder), bot_v3.MAX_LADDER_RUNGS)

    def test_ladder_sorted_by_proximity(self):
        """Rungs should be sorted by distance from consensus."""
        probs = {"65-70": 0.30, "70-75": 0.50, "75-80": 0.30, "60-65": 0.20, "80-999": 0.15}
        prices = {"65-70": 0.05, "70-75": 0.05, "75-80": 0.05, "60-65": 0.05, "80-999": 0.05}
        ladder = bot_v3.build_ladder(self.buckets, probs, prices, 72.5, 10000, 1.0)
        if len(ladder) >= 2:
            for i in range(len(ladder) - 1):
                self.assertLessEqual(ladder[i]["distance"], ladder[i+1]["distance"])

    def test_combined_hit_probability(self):
        """Combined prob should be 1 - product(1-p_i)."""
        probs = {"70-75": 0.50, "75-80": 0.30}
        prices = {"70-75": 0.05, "75-80": 0.05}
        buckets = {"70-75": (70, 75), "75-80": (75, 80)}
        ladder = bot_v3.build_ladder(buckets, probs, prices, 72.5, 10000, 1.0)
        if ladder:
            expected = 1.0 - (1.0 - 0.50) * (1.0 - 0.30)
            self.assertAlmostEqual(ladder[0]["combined_hit_prob"], round(expected, 4), places=3)

    def test_bet_allocation_proportional_to_edge(self):
        """Larger edge should get larger bet allocation."""
        probs = {"70-75": 0.60, "75-80": 0.30}
        prices = {"70-75": 0.05, "75-80": 0.05}
        buckets = {"70-75": (70, 75), "75-80": (75, 80)}
        ladder = bot_v3.build_ladder(buckets, probs, prices, 72.5, 10000, 1.0)
        if len(ladder) == 2:
            # 70-75 has edge=0.55, 75-80 has edge=0.25 -> 70-75 should get more
            self.assertGreaterEqual(ladder[0]["bet_size"], ladder[1]["bet_size"])


class TestHoursUntilResolution(unittest.TestCase):

    def test_future_event(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        hours = bot_v3.hours_until_resolution({"endDate": future})
        self.assertAlmostEqual(hours, 24.0, delta=0.1)

    def test_past_event(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        hours = bot_v3.hours_until_resolution({"endDate": past})
        self.assertEqual(hours, 0)

    def test_no_end_date(self):
        self.assertEqual(bot_v3.hours_until_resolution({}), 999)

    def test_z_suffix(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        hours = bot_v3.hours_until_resolution({"endDate": future})
        self.assertAlmostEqual(hours, 10.0, delta=0.1)


class TestLocations(unittest.TestCase):
    """Validate location configuration."""

    def test_all_us_stations_have_nws(self):
        for slug, loc in bot_v3.LOCATIONS.items():
            if loc["unit"] == "fahrenheit":
                self.assertIn("nws", loc, f"{slug} is US but missing NWS grid")

    def test_international_stations_no_nws(self):
        for slug, loc in bot_v3.LOCATIONS.items():
            if loc["unit"] == "celsius":
                self.assertNotIn("nws", loc, f"{slug} is intl but has NWS grid")

    def test_required_fields(self):
        for slug, loc in bot_v3.LOCATIONS.items():
            self.assertIn("lat", loc, f"{slug} missing lat")
            self.assertIn("lon", loc, f"{slug} missing lon")
            self.assertIn("name", loc, f"{slug} missing name")
            self.assertIn("station", loc, f"{slug} missing station")
            self.assertIn("unit", loc, f"{slug} missing unit")

    def test_expected_cities_present(self):
        expected = ["nyc", "chicago", "miami", "dallas", "seattle", "atlanta",
                    "denver", "phoenix", "london", "tokyo", "seoul", "paris"]
        for city in expected:
            self.assertIn(city, bot_v3.LOCATIONS, f"Missing city: {city}")

    def test_station_codes(self):
        checks = {
            "nyc": "KLGA", "denver": "KDEN", "phoenix": "KPHX",
            "london": "EGLL", "tokyo": "RJTT", "seoul": "RKSI", "paris": "LFPG",
        }
        for slug, expected_station in checks.items():
            self.assertEqual(bot_v3.LOCATIONS[slug]["station"], expected_station,
                           f"{slug} wrong station")


class TestSimulation(unittest.TestCase):
    """Test simulation state management."""

    def setUp(self):
        self.test_file = "test_simulation_tmp.json"
        bot_v3.SIM_FILE = self.test_file

    def tearDown(self):
        if os.path.exists(self.test_file):
            os.remove(self.test_file)

    def test_load_default(self):
        sim = bot_v3.load_sim()
        self.assertEqual(sim["balance"], bot_v3.BANKROLL)
        self.assertEqual(sim["positions"], {})

    def test_save_and_load(self):
        sim = bot_v3.load_sim()
        sim["balance"] = 9500.0
        sim["positions"]["test_id"] = {"question": "test", "cost": 50.0}
        bot_v3.save_sim(sim)

        loaded = bot_v3.load_sim()
        self.assertEqual(loaded["balance"], 9500.0)
        self.assertIn("test_id", loaded["positions"])


class TestLogSignal(unittest.TestCase):
    """Test NDJSON logging."""

    def setUp(self):
        self.test_log = Path("test_signals_tmp.ndjson")
        bot_v3.LOG_FILE = self.test_log

    def tearDown(self):
        if self.test_log.exists():
            self.test_log.unlink()

    def test_log_writes_ndjson(self):
        bot_v3.log_signal({"type": "edge", "city": "nyc", "edge": 0.25})
        bot_v3.log_signal({"type": "ladder", "city": "chicago", "edge": 0.30})

        lines = self.test_log.read_text().strip().split("\n")
        self.assertEqual(len(lines), 2)

        entry1 = json.loads(lines[0])
        self.assertEqual(entry1["type"], "edge")
        self.assertEqual(entry1["city"], "nyc")
        self.assertIn("timestamp", entry1)

        entry2 = json.loads(lines[1])
        self.assertEqual(entry2["type"], "ladder")


if __name__ == "__main__":
    unittest.main()
