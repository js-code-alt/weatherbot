"""Unit tests for analytics summaries."""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import analytics


class TestAnalyticsBucketTypes(unittest.TestCase):
    def test_calibration_groups_by_bucket_type_from_entry_signal(self):
        signals = [
            {
                "type": "entry",
                "market_id": "M1",
                "bucket": "71.5-72.5",
                "bucket_type": "exact",
                "model_probability": 0.60,
            },
            {
                "type": "resolution",
                "market_id": "M1",
                "bucket": "71.5-72.5",
                "actual_temp": 72.0,
            },
        ]

        out = io.StringIO()
        with redirect_stdout(out):
            analytics.run_calibration(signals)

        text = out.getvalue()
        self.assertIn("By Bucket Type", text)
        self.assertIn("exact", text)

    def test_pnl_includes_thesis_break_and_bucket_type(self):
        signals = [
            {
                "type": "entry",
                "market_id": "M1",
                "bucket": "72.0-999.0",
                "bucket_type": "or_higher",
                "confidence": "MEDIUM",
                "horizon": 1,
            },
            {
                "type": "thesis_break",
                "market_id": "M1",
                "pnl": 25.0,
            },
        ]

        out = io.StringIO()
        with redirect_stdout(out):
            analytics.run_pnl(signals)

        text = out.getvalue()
        self.assertIn("Thesis Break", text)
        self.assertIn("or_higher", text)


if __name__ == "__main__":
    unittest.main()
