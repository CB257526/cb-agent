"""
Token用量和提示缓存（prompt cache）指标的持久化模块。

EventBus 本质上是实时流，而不是事件存储。本记录器会为 TokenUsage 事件
维护一个小型的 JSONL 日志文件，使得 UI 命令可以回答"今天的缓存命中率是多少？"
这类问题，而无需去抓取运行时的日志。
"""

from __future__ import annotations  # 允许在类型注解中使用类自身的名字（PEP 563）

import dataclasses  # 用于将 dataclass 实例转为字典（asdict）
import json  # JSON 序列化/反序列化
import threading  # 线程锁，保证并发写入安全
from datetime import date, datetime  # 日期时间处理
from pathlib import Path  # 跨平台路径操作
from typing import Any, Dict, Iterable, Optional  # 类型注解

from agent.event_bus import EventBus  # 事件总线，用于订阅/发布事件
from agent.events import TokenUsage  # Token 用量事件的数据结构


def _local_datetime(ts: Optional[float] = None) -> datetime:
    """
    将时间戳（可选）转换为带时区的本地 datetime 对象。

    参数:
        ts: 可选，Unix 时间戳（秒）。如果为 None，则返回当前本地时间。

    返回:
        带本地时区信息的 datetime 对象。
    """
    if ts is None:
        return datetime.now().astimezone()  # 当前时刻，带时区
    return datetime.fromtimestamp(float(ts)).astimezone()  # 从时间戳转换并附加时区


def _date_key(day: Optional[date] = None) -> str:
    """
    生成日期键字符串，格式为 ISO 8601（YYYY-MM-DD）。

    参数:
        day: 可选，日期对象。如果为 None，则使用当前本地日期。

    返回:
        形如 "2026-07-07" 的日期字符串。
    """
    return (day or _local_datetime().date()).isoformat()


def _metric_tokens(event: TokenUsage | Dict[str, Any]) -> tuple[Optional[int], Optional[int], str]:
    """
    从 TokenUsage 事件中提取缓存命中 tokens 和分母 tokens。

    支持两种输入格式：
      1. TokenUsage dataclass 实例
      2. 字典（用于从 JSONL 文件反序列化的行）

    返回:
        (hit_tokens, denominator_tokens, source) 三元组。
        - hit_tokens: 缓存命中的 token 数量
        - denominator_tokens: 计算命中率时的分母（总可缓存 token 数）
        - source: 指标来源标识（"hit_miss" / "cached_over_prompt" / "unsupported"）
    """
    if isinstance(event, TokenUsage):
        # 从 TokenUsage dataclass 提取字段
        prompt_tokens = int(event.prompt_tokens or 0)
        cached = event.cached_prompt_tokens
        hit = event.prompt_cache_hit_tokens
        miss = event.prompt_cache_miss_tokens
    else:
        # 从字典（JSON 反序列化结果）提取字段
        prompt_tokens = int(event.get("prompt_tokens") or 0)
        cached = event.get("cached_prompt_tokens")
        hit = event.get("prompt_cache_hit_tokens")
        miss = event.get("prompt_cache_miss_tokens")

    # 情况 1: 同时有 hit 和 miss 字段 —— 最精确的方式
    if hit is not None and miss is not None:
        hit_i = max(0, int(hit))       # 防止负值
        miss_i = max(0, int(miss))     # 防止负值
        return hit_i, hit_i + miss_i, "hit_miss"  # 分母 = 命中 + 未命中

    # 情况 2: 只有 cached 字段，但 prompt_tokens > 0 —— 用 cached/prompt 近似
    if cached is not None and prompt_tokens > 0:
        return max(0, int(cached)), prompt_tokens, "cached_over_prompt"

    # 情况 3: 字段不足，无法计算命中率
    return None, None, "unsupported"


def _empty_bucket(model: Optional[str] = None) -> Dict[str, Any]:
    """
    创建一个空的统计数据桶（bucket），用于按模型或全局汇总。

    参数:
        model: 模型名称。为 None 时表示全局汇总桶。

    返回:
        初始值为 0 的统计字典。
    """
    return {
        "model": model,                    # 模型名称
        "requests": 0,                     # 总请求数
        "supported_requests": 0,           # 支持缓存统计的请求数
        "unsupported_requests": 0,         # 不支持缓存统计的请求数
        "prompt_tokens": 0,                # 提示 token 总数
        "completion_tokens": 0,            # 补全 token 总数
        "total_tokens": 0,                 # 总 token 数
        "cache_hit_tokens": 0,             # 缓存命中的 token 总数
        "cache_denominator_tokens": 0,     # 缓存命中率计算的分母
        "cache_hit_rate": None,            # 缓存命中率（计算后填充）
    }


def _add_row(bucket: Dict[str, Any], row: Dict[str, Any]) -> None:
    """
    将一行 TokenUsage 数据累加到指定的统计桶中。

    参数:
        bucket: 目标统计桶（会被就地修改）。
        row:    一行 TokenUsage 数据（字典格式）。
    """
    bucket["requests"] += 1  # 总请求数 +1
    bucket["prompt_tokens"] += int(row.get("prompt_tokens") or 0)
    bucket["completion_tokens"] += int(row.get("completion_tokens") or 0)
    bucket["total_tokens"] += int(row.get("total_tokens") or 0)

    # 提取缓存相关的指标
    hit, denominator, _source = _metric_tokens(row)
    if hit is None or denominator is None or denominator <= 0:
        # 无法计算缓存命中率，归类为"不支持"
        bucket["unsupported_requests"] += 1
        return
    # 有可用的缓存指标
    bucket["supported_requests"] += 1
    bucket["cache_hit_tokens"] += hit
    bucket["cache_denominator_tokens"] += denominator


def _finish_bucket(bucket: Dict[str, Any]) -> Dict[str, Any]:
    """
    完成统计桶的计算：计算最终的缓存命中率。

    参数:
        bucket: 已累加完数据的统计桶。

    返回:
        填充了 cache_hit_rate 字段后的统计字典。
    """
    denominator = int(bucket.get("cache_denominator_tokens") or 0)
    if denominator > 0:
        # 命中率 = 命中 tokens / 分母 tokens，保留 4 位小数
        bucket["cache_hit_rate"] = round(int(bucket.get("cache_hit_tokens") or 0) / denominator, 4)
    else:
        bucket["cache_hit_rate"] = None  # 没有可缓存请求，命中率为空
    return bucket


class UsageMetricsRecorder:
    """
    Token 用量指标记录器。

    订阅 EventBus 上的 TokenUsage 事件，将每次调用的 token 用量和
    缓存命中信息追加写入按日期切割的 JSONL 文件，并提供按日期/模型的汇总能力。
    """

    def __init__(self, metrics_dir: Path):
        """
        初始化记录器。

        参数:
            metrics_dir: 存放指标 JSONL 文件的目录路径。目录不存在会自动创建。
        """
        self.metrics_dir = Path(metrics_dir)               # 归一化路径
        self.metrics_dir.mkdir(parents=True, exist_ok=True)  # 确保目录存在
        self._lock = threading.Lock()  # 写文件时用的线程锁

    def attach(self, bus: EventBus) -> None:
        """
        将本记录器挂载到事件总线上，开始监听 TokenUsage 事件。

        参数:
            bus: EventBus 实例，本记录器会订阅其 TokenUsage 事件。
        """
        bus.subscribe(self.record, TokenUsage)  # 订阅 TokenUsage 类型的事件

    def path_for_date(self, day: Optional[date] = None) -> Path:
        """
        获取指定日期对应的 JSONL 文件路径。

        参数:
            day: 日期。默认 None 表示当天。

        返回:
            形如 /path/to/token-usage-2026-07-07.jsonl 的路径。
        """
        return self.metrics_dir / f"token-usage-{_date_key(day)}.jsonl"

    def record(self, event: TokenUsage) -> None:
        """
        记录一条 TokenUsage 事件到 JSONL 文件。

        这是 EventBus 事件的回调方法，每当有 TokenUsage 事件发出时被调用。
        将事件数据序列化为 JSON 行，追加写入对应日期的文件。

        参数:
            event: TokenUsage dataclass 实例，包含 token 用量详情。
        """
        dt = _local_datetime(event.timestamp)              # 事件时间戳 → 本地时间
        hit, denominator, source = _metric_tokens(event)   # 提取缓存指标

        # 将 dataclass 转为字典，并补充额外字段
        payload = dataclasses.asdict(event)
        payload.update({
            "datetime": dt.isoformat(timespec="milliseconds"),  # 精确到毫秒的本地时间
            "date": dt.date().isoformat(),                      # 事件日期
            "cache_hit_tokens": hit,                            # 缓存命中 tokens
            "cache_denominator_tokens": denominator,             # 缓存命中率分母
            "cache_metric_source": source,                      # 指标来源标识
        })

        path = self.path_for_date(dt.date())  # 按事件日期确定文件路径
        line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"

        # 线程安全地追加写入 JSONL 文件
        with self._lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(line)

    def summarize_today(self) -> Dict[str, Any]:
        """
        汇总今天的 Token 用量统计。

        返回:
            当天的汇总字典，包含总览和按模型分组的详细数据。
        """
        return self.summarize_date(_local_datetime().date())

    def summarize_date(self, day: date) -> Dict[str, Any]:
        """
        汇总指定日期的 Token 用量统计。

        读取对应日期的 JSONL 文件，按请求总数累积统计，
        同时按模型分组统计，最后计算各组的缓存命中率。

        参数:
            day: 要汇总的日期。

        返回:
            包含日期、文件路径、总量和按模型分组数据的字典。
        """
        path = self.path_for_date(day)          # 获取文件路径
        rows = list(_read_rows(path))           # 读取所有行
        total = _empty_bucket(model=None)       # 全局汇总桶
        by_model: Dict[str, Dict[str, Any]] = {}  # 按模型分组的字典

        for row in rows:
            _add_row(total, row)  # 累加到全局统计
            model = str(row.get("model") or "unknown")  # 获取模型名称
            bucket = by_model.setdefault(model, _empty_bucket(model=model))  # 获取或创建模型桶
            _add_row(bucket, row)  # 累加到模型统计

        return {
            "date": day.isoformat(),                                # 日期
            "path": str(path),                                      # JSONL 文件路径
            "total": _finish_bucket(total),                         # 全局汇总（含命中率）
            "models": sorted(
                (_finish_bucket(bucket) for bucket in by_model.values()),  # 各模型汇总
                key=lambda item: (
                    -int(item.get("requests") or 0),    # 按请求数降序排列
                    str(item.get("model") or ""),        # 请求数相同时按模型名升序排列
                ),
            ),
        }


def _read_rows(path: Path) -> Iterable[Dict[str, Any]]:
    """
    读取 JSONL 文件中所有有效的行，返回字典列表。

    自动跳过空行和 JSON 解析失败的行，保证健壮性。

    参数:
        path: JSONL 文件路径。

    返回:
        字典的可迭代对象，每个字典代表一行数据。
    """
    if not path.exists():
        return []  # 文件不存在，返回空列表

    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue  # 跳过空行
            try:
                row = json.loads(line)  # 尝试解析 JSON
            except json.JSONDecodeError:
                continue  # 解析失败则跳过（容忍脏数据）
            if isinstance(row, dict):
                rows.append(row)  # 只保留字典类型的行
    return rows


__all__ = ["UsageMetricsRecorder"]  # 模块的公开接口
