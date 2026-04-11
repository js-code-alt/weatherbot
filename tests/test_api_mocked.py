"""Integration tests for API fetchers using mocked HTTP responses."""

import sys
import os
import json
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot_v3


def mock_response(json_data, status=200):
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.status_code = status
    return resp


class TestFetchECMWF(unittest.TestCase):

    @patch.object(bot_v3, '_get')
    def test_parse_response(self, mock_get):
        mock_get.return_value = mock_response({
            "daily": {
                "time": ["2026-04-11", "2026-04-12", "2026-04-13"],
                "temperature_2m_max": [62.0, 55.0, 70.0],
            }
        })
        loc = bot_v3.LOCATIONS["nyc"]
        result = bot_v3.fetch_ecmwf(loc, ["2026-04-11", "2026-04-12"])
        self.assertEqual(result["2026-04-11"], 62)
        self.assertEqual(result["2026-04-12"], 55)
        self.assertNotIn("2026-04-13", result)  # not in requested dates

    @patch.object(bot_v3, '_get')
    def test_handles_error_response(self, mock_get):
        mock_get.return_value = mock_response({"error": True, "reason": "Not found"})
        loc = bot_v3.LOCATIONS["nyc"]
        result = bot_v3.fetch_ecmwf(loc, ["2026-04-11"])
        self.assertEqual(result, {})

    @patch.object(bot_v3, '_get')
    def test_handles_exception(self, mock_get):
        mock_get.side_effect = Exception("timeout")
        loc = bot_v3.LOCATIONS["nyc"]
        result = bot_v3.fetch_ecmwf(loc, ["2026-04-11"])
        self.assertEqual(result, {})

    @patch.object(bot_v3, '_get')
    def test_celsius_rounding(self, mock_get):
        mock_get.return_value = mock_response({
            "daily": {
                "time": ["2026-04-11"],
                "temperature_2m_max": [22.37],
            }
        })
        loc = bot_v3.LOCATIONS["london"]  # celsius
        result = bot_v3.fetch_ecmwf(loc, ["2026-04-11"])
        self.assertEqual(result["2026-04-11"], 22.4)  # rounded to 1 decimal


class TestFetchGFS(unittest.TestCase):

    @patch.object(bot_v3, '_get')
    def test_parse_response(self, mock_get):
        mock_get.return_value = mock_response({
            "daily": {
                "time": ["2026-04-11", "2026-04-12"],
                "temperature_2m_max": [63.0, 56.0],
            }
        })
        loc = bot_v3.LOCATIONS["nyc"]
        result = bot_v3.fetch_gfs(loc, ["2026-04-11"])
        self.assertEqual(result["2026-04-11"], 63)

    @patch.object(bot_v3, '_get')
    def test_skips_none_values(self, mock_get):
        mock_get.return_value = mock_response({
            "daily": {
                "time": ["2026-04-11", "2026-04-12"],
                "temperature_2m_max": [None, 56.0],
            }
        })
        loc = bot_v3.LOCATIONS["nyc"]
        result = bot_v3.fetch_gfs(loc, ["2026-04-11", "2026-04-12"])
        self.assertNotIn("2026-04-11", result)
        self.assertIn("2026-04-12", result)


class TestFetchNWS(unittest.TestCase):

    @patch.object(bot_v3, '_get')
    def test_parse_hourly(self, mock_get):
        mock_get.return_value = mock_response({
            "properties": {
                "periods": [
                    {"startTime": "2026-04-11T06:00:00-04:00", "temperature": 55, "temperatureUnit": "F"},
                    {"startTime": "2026-04-11T14:00:00-04:00", "temperature": 72, "temperatureUnit": "F"},
                    {"startTime": "2026-04-11T18:00:00-04:00", "temperature": 68, "temperatureUnit": "F"},
                    {"startTime": "2026-04-12T10:00:00-04:00", "temperature": 60, "temperatureUnit": "F"},
                ]
            }
        })
        loc = bot_v3.LOCATIONS["nyc"]
        result = bot_v3.fetch_nws(loc, ["2026-04-11", "2026-04-12"])
        self.assertEqual(result["2026-04-11"], 72)  # max of 55, 72, 68
        self.assertEqual(result["2026-04-12"], 60)

    def test_no_nws_for_international(self):
        loc = bot_v3.LOCATIONS["london"]
        result = bot_v3.fetch_nws(loc, ["2026-04-11"])
        self.assertEqual(result, {})

    @patch.object(bot_v3, '_get')
    def test_celsius_conversion(self, mock_get):
        mock_get.return_value = mock_response({
            "properties": {
                "periods": [
                    {"startTime": "2026-04-11T14:00:00-04:00", "temperature": 20, "temperatureUnit": "C"},
                ]
            }
        })
        loc = bot_v3.LOCATIONS["nyc"]
        result = bot_v3.fetch_nws(loc, ["2026-04-11"])
        self.assertEqual(result["2026-04-11"], 68)  # 20C = 68F


class TestFetchEnsemble(unittest.TestCase):

    @patch.object(bot_v3, '_get')
    def test_computes_daily_max_per_member(self, mock_get):
        # 2 members, 3 hourly timesteps for one day
        mock_get.return_value = mock_response({
            "hourly": {
                "time": ["2026-04-11T00:00", "2026-04-11T06:00", "2026-04-11T12:00"],
                "temperature_2m_member00": [60, 65, 70],
                "temperature_2m_member01": [58, 63, 72],
            }
        })
        loc = bot_v3.LOCATIONS["nyc"]
        result = bot_v3.fetch_ensemble(loc, ["2026-04-11"])
        self.assertIn("2026-04-11", result)
        data = result["2026-04-11"]
        self.assertEqual(data["members"], [70, 72])  # max per member
        self.assertAlmostEqual(data["mean"], 71.0)
        self.assertGreater(data["std"], 0)

    @patch.object(bot_v3, '_get')
    def test_handles_error(self, mock_get):
        mock_get.return_value = mock_response({"error": True})
        loc = bot_v3.LOCATIONS["nyc"]
        result = bot_v3.fetch_ensemble(loc, ["2026-04-11"])
        self.assertEqual(result, {})


class TestPolymarketEvent(unittest.TestCase):

    @patch.object(bot_v3, '_get')
    def test_parse_event(self, mock_get):
        mock_get.return_value = mock_response([{
            "slug": "highest-temperature-in-nyc-on-april-12-2026",
            "endDate": "2026-04-13T00:00:00Z",
            "markets": [
                {
                    "id": "abc123",
                    "question": "Will the highest temperature be between 70-75°F on April 12?",
                    "outcomePrices": "[0.08, 0.92]",
                    "volume": 1000,
                }
            ]
        }])
        event = bot_v3.get_polymarket_event("nyc", "april", 12, 2026)
        self.assertIsNotNone(event)
        self.assertEqual(len(event["markets"]), 1)

    @patch.object(bot_v3, '_get')
    def test_empty_response(self, mock_get):
        mock_get.return_value = mock_response([])
        event = bot_v3.get_polymarket_event("nyc", "april", 12, 2026)
        self.assertIsNone(event)

    @patch.object(bot_v3, '_get')
    def test_handles_error(self, mock_get):
        mock_get.side_effect = Exception("network error")
        event = bot_v3.get_polymarket_event("nyc", "april", 12, 2026)
        self.assertIsNone(event)


if __name__ == "__main__":
    unittest.main()
