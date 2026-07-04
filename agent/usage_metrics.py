"""Token usage and prompt-cache metrics persistence.

EventBus is intentionally a live stream, not an event store. This recorder
keeps a small JSONL ledger for TokenUsage events so UI commands can answer
"today's cache hit rate" without scraping runtime logs.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from agent.event_bus import EventBus
from agent.events import TokenUsage


def _local_datetime(ts: Optional[float] = None) -> datetime:
    if ts is None:
        return datetime.now().astimezone()
    return datetime.fromtimestamp(float(ts)).astimezone()


def _date_key(day: Optional[date] = None) -> str:
    return (day or _local_datetime().date()).isoformat()


def _metric_tokens(event: TokenUsage | Dict[str, Any]) -> tuple[Optional[int], Optional[int], str]:
    """Return (hit_tokens, denominator_tokens, source)."""
    if isinstance(event, TokenUsage):
        prompt_tokens = int(event.prompt_tokens or 0)
        cached = event.cached_prompt_tokens
        hit = event.prompt_cache_hit_tokens
        miss = event.prompt_cache_miss_tokens
    else:
        prompt_tokens = int(event.get("prompt_tokens") or 0)
        cached = event.get("cached_prompt_tokens")
        hit = event.get("prompt_cache_hit_tokens")
        miss = event.get("prompt_cache_miss_tokens")

    if hit is not None and miss is not None:
        hit_i = max(0, int(hit))
        miss_i = max(0, int(miss))
        return hit_i, hit_i + miss_i, "hit_miss"
    if cached is not None and prompt_tokens > 0:
        return max(0, int(cached)), prompt_tokens, "cached_over_prompt"
    return None, None, "unsupported"


def _empty_bucket(model: Optional[str] = None) -> Dict[str, Any]:
    return {
        "model": model,
        "requests": 0,
        "supported_requests": 0,
        "unsupported_requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_denominator_tokens": 0,
        "cache_hit_rate": None,
    }


def _add_row(bucket: Dict[str, Any], row: Dict[str, Any]) -> None:
    bucket["requests"] += 1
    bucket["prompt_tokens"] += int(row.get("prompt_tokens") or 0)
    bucket["completion_tokens"] += int(row.get("completion_tokens") or 0)
    bucket["total_tokens"] += int(row.get("total_tokens") or 0)

    hit, denominator, _source = _metric_tokens(row)
    if hit is None or denominator is None or denominator <= 0:
        bucket["unsupported_requests"] += 1
        return
    bucket["supported_requests"] += 1
    bucket["cache_hit_tokens"] += hit
    bucket["cache_denominator_tokens"] += denominator


def _finish_bucket(bucket: Dict[str, Any]) -> Dict[str, Any]:
    denominator = int(bucket.get("cache_denominator_tokens") or 0)
    if denominator > 0:
        bucket["cache_hit_rate"] = round(int(bucket.get("cache_hit_tokens") or 0) / denominator, 4)
    else:
        bucket["cache_hit_rate"] = None
    return bucket


class UsageMetricsRecorder:
    def __init__(self, metrics_dir: Path):
        self.metrics_dir = Path(metrics_dir)
        self.metrics_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def attach(self, bus: EventBus) -> None:
        bus.subscribe(self.record, TokenUsage)

    def path_for_date(self, day: Optional[date] = None) -> Path:
        return self.metrics_dir / f"token-usage-{_date_key(day)}.jsonl"

    def record(self, event: TokenUsage) -> None:
        dt = _local_datetime(event.timestamp)
        hit, denominator, source = _metric_tokens(event)
        payload = dataclasses.asdict(event)
        payload.update({
            "datetime": dt.isoformat(timespec="milliseconds"),
            "date": dt.date().isoformat(),
            "cache_hit_tokens": hit,
            "cache_denominator_tokens": denominator,
            "cache_metric_source": source,
        })
        path = self.path_for_date(dt.date())
        line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(line)

    def summarize_today(self) -> Dict[str, Any]:
        return self.summarize_date(_local_datetime().date())

    def summarize_date(self, day: date) -> Dict[str, Any]:
        path = self.path_for_date(day)
        rows = list(_read_rows(path))
        total = _empty_bucket(model=None)
        by_model: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            _add_row(total, row)
            model = str(row.get("model") or "unknown")
            bucket = by_model.setdefault(model, _empty_bucket(model=model))
            _add_row(bucket, row)
        return {
            "date": day.isoformat(),
            "path": str(path),
            "total": _finish_bucket(total),
            "models": sorted(
                (_finish_bucket(bucket) for bucket in by_model.values()),
                key=lambda item: (-int(item.get("requests") or 0), str(item.get("model") or "")),
            ),
        }


def _read_rows(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


__all__ = ["UsageMetricsRecorder"]
