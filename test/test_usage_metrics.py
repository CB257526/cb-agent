from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.events import TokenUsage
from agent.usage_metrics import UsageMetricsRecorder


class TestUsageMetricsRecorder(unittest.TestCase):
    def test_records_and_summarizes_by_model(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            recorder = UsageMetricsRecorder(Path(td))
            recorder.record(TokenUsage(
                prompt_tokens=100,
                completion_tokens=10,
                total_tokens=110,
                prompt_cache_hit_tokens=80,
                prompt_cache_miss_tokens=20,
                model="model-a",
                timestamp=1898611200.0,
            ))
            recorder.record(TokenUsage(
                prompt_tokens=50,
                completion_tokens=5,
                total_tokens=55,
                cached_prompt_tokens=25,
                model="model-b",
                timestamp=1898611200.0,
            ))
            recorder.record(TokenUsage(
                prompt_tokens=10,
                completion_tokens=1,
                total_tokens=11,
                model="model-b",
                timestamp=1898611200.0,
            ))

            day = datetime.fromtimestamp(1898611200.0).astimezone().date()
            summary = recorder.summarize_date(day)

        self.assertEqual(summary["total"]["requests"], 3)
        self.assertEqual(summary["total"]["supported_requests"], 2)
        self.assertEqual(summary["total"]["unsupported_requests"], 1)
        self.assertEqual(summary["total"]["cache_hit_tokens"], 105)
        self.assertEqual(summary["total"]["cache_denominator_tokens"], 150)
        self.assertEqual(summary["total"]["cache_hit_rate"], 0.7)
        by_model = {item["model"]: item for item in summary["models"]}
        self.assertEqual(by_model["model-a"]["cache_hit_rate"], 0.8)
        self.assertEqual(by_model["model-b"]["requests"], 2)
        self.assertEqual(by_model["model-b"]["cache_hit_rate"], 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
