"""时间窗工具：解析 ``08:00-20:00`` 这类区间，支持跨天区间。"""

from __future__ import annotations

from datetime import datetime, timedelta

MINUTES_PER_DAY = 24 * 60

Window = tuple[int, int]


def parse_hhmm(text: str) -> int | None:
    """把 ``HH:MM`` 解析成当天第几分钟；非法返回 None。"""
    raw = str(text or "").strip()
    if ":" not in raw:
        return None
    hour_text, minute_text = raw.split(":", 1)
    try:
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError):
        return None
    if hour == 24 and minute == 0:
        return 0  # 24:00 折回 0，配合 in_window 的 start==end “全天”语义
    if not (0 <= hour < 24) or not (0 <= minute < 60):
        return None
    return hour * 60 + minute


def parse_windows(spec: str | None) -> list[Window]:
    """解析 ``"08:00-20:00"`` / ``"08:00-12:00, 14:00-18:00"``。"""
    windows: list[Window] = []
    text = str(spec or "").replace("，", ",").replace("；", ",").replace("~", "-")
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk or "-" not in chunk:
            continue
        start_text, end_text = chunk.split("-", 1)
        start = parse_hhmm(start_text)
        end = parse_hhmm(end_text)
        if start is None or end is None:
            continue
        windows.append((start, end))
    return windows


def minute_of_day(moment: datetime) -> int:
    return moment.hour * 60 + moment.minute


def in_window(minute: int, window: Window) -> bool:
    start, end = window
    if start == end:
        # 00:00-24:00 这类写法按“全天”处理
        return True
    if start < end:
        return start <= minute < end
    # 跨天，例如 23:00-08:00
    return minute >= start or minute < end


def in_windows(minute: int, windows: list[Window]) -> bool:
    return any(in_window(minute, window) for window in windows)


def is_allowed(
    moment: datetime,
    active_windows: list[Window] | None,
    quiet_windows: list[Window] | None,
) -> bool:
    """是否落在允许活动的时间点：在活动时段内、且不在静默时段。"""
    minute = minute_of_day(moment)
    if active_windows and not in_windows(minute, active_windows):
        return False
    if quiet_windows and in_windows(minute, quiet_windows):
        return False
    return True


def next_allowed(
    moment: datetime,
    active_windows: list[Window] | None,
    quiet_windows: list[Window] | None,
    horizon_minutes: int = MINUTES_PER_DAY * 3,
) -> datetime:
    """返回不早于 ``moment`` 的第一个允许时间点。"""
    if is_allowed(moment, active_windows, quiet_windows):
        return moment
    probe = moment.replace(second=0, microsecond=0)
    for _ in range(horizon_minutes):
        probe += timedelta(minutes=1)
        if is_allowed(probe, active_windows, quiet_windows):
            return probe
    return moment


def next_allowed_seconds(
    now: datetime,
    active_windows: list[Window] | None,
    quiet_windows: list[Window] | None,
) -> float:
    """距离下一个允许时间点还有多少秒。"""
    return (next_allowed(now, active_windows, quiet_windows) - now).total_seconds()


def describe_windows(spec: str | None) -> str:
    windows = parse_windows(spec)
    if not windows:
        return "全天"
    parts = []
    for start, end in windows:
        parts.append(f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}")
    return "、".join(parts)
