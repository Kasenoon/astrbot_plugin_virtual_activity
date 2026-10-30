"""B 站视频抓取：用公开接口（综合热门 / 分区最新），可选 SESSDATA。"""

from __future__ import annotations

import asyncio
import json
import random
import urllib.request
from dataclasses import dataclass, field
from typing import Any

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

POPULAR_URL = "https://api.bilibili.com/x/web-interface/popular?ps=30&pn=1"
NEWLIST_URL = "https://api.bilibili.com/x/web-interface/newlist?rid={rid}&pn=1&ps=30"
REGION_URL = "https://api.bilibili.com/x/web-interface/ranking/region?rid={rid}&day=3"

# 常用分区名 → rid，配置里也可以直接写数字
PARTITIONS: dict[str, int] = {
    "动画": 1,
    "番剧": 13,
    "游戏": 4,
    "单机游戏": 17,
    "知识": 36,
    "科技": 188,
    "鬼畜": 119,
    "娱乐": 5,
    "影视": 181,
    "音乐": 3,
    "生活": 160,
    "美食": 211,
    "舞蹈": 129,
    "时尚": 155,
    "纪录片": 177,
}

SPAM_PATTERNS = ("发布站", "公益服", "私服", "开服", "代练", "充值")


def resolve_rids(spec: Any) -> list[int]:
    """把 "动画,游戏,36" 这样的配置解析成 rid 列表。"""
    rids: list[int] = []
    if isinstance(spec, str):
        parts = [item.strip() for item in spec.replace("，", ",").split(",")]
    elif isinstance(spec, (list, tuple)):
        parts = [str(item).strip() for item in spec]
    else:
        parts = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            rid = int(part)
        else:
            rid = PARTITIONS.get(part, 0)
        if rid and rid not in rids:
            rids.append(rid)
    return rids


def _http_json(url: str, cookie: str, timeout: float) -> dict[str, Any]:
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json",
    }
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


async def _get_json(url: str, cookie: str, timeout: float) -> dict[str, Any]:
    return await asyncio.to_thread(_http_json, url, cookie, timeout)


def parse_duration(value: Any) -> int:
    """把 ``"04:20"`` / ``"1:02:03"`` / 秒数统一成秒。"""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return 0
    if ":" not in text:
        try:
            return int(text)
        except ValueError:
            return 0
    seconds = 0
    for part in text.split(":"):
        try:
            seconds = seconds * 60 + int(part)
        except ValueError:
            return 0
    return seconds


def normalize_region_video(item: Any) -> dict[str, Any] | None:
    """分区排行榜的条目字段和 popular 不一样。"""
    if not isinstance(item, dict):
        return None
    bvid = str(item.get("bvid") or "").strip()
    title = str(item.get("title") or "").strip()
    if not bvid or not title:
        return None
    if any(pattern in title for pattern in SPAM_PATTERNS):
        return None
    try:
        views = int(item.get("play") or 0)
    except (TypeError, ValueError):
        views = 0
    return {
        "bvid": bvid,
        "title": title,
        "owner": str(item.get("author") or ""),
        "views": views,
        "duration": parse_duration(item.get("duration")),
        "tname": str(item.get("typename") or ""),
        "tid": 0,
        "desc": str(item.get("description") or "").strip()[:200],
        "url": f"https://www.bilibili.com/video/{bvid}",
    }


def extract_region(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict):
        data = data.get("archives") or data.get("list")
    if not isinstance(data, list):
        return []
    videos = []
    for item in data:
        normalized = normalize_region_video(item)
        if normalized:
            videos.append(normalized)
    return videos


def normalize_video(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    bvid = str(item.get("bvid") or "").strip()
    title = str(item.get("title") or "").strip()
    if not bvid or not title:
        return None
    if any(pattern in title for pattern in SPAM_PATTERNS):
        return None
    owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
    stat = item.get("stat") if isinstance(item.get("stat"), dict) else {}
    try:
        views = int(stat.get("view") or 0)
    except (TypeError, ValueError):
        views = 0
    try:
        duration = int(item.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    return {
        "bvid": bvid,
        "title": title,
        "owner": str(owner.get("name") or ""),
        "views": views,
        "duration": duration,
        "tname": str(item.get("tname") or ""),
        "tid": int(item.get("tid") or 0),
        "desc": str(item.get("desc") or "").strip()[:200],
        "url": f"https://www.bilibili.com/video/{bvid}",
    }


def extract_pool(payload: Any) -> list[dict[str, Any]]:
    """从 popular / newlist 的响应里取出视频列表。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    raw = data.get("list") or data.get("archives") or []
    videos = []
    for item in raw if isinstance(raw, list) else []:
        video = normalize_video(item)
        if video:
            videos.append(video)
    return videos


async def _safe_get(url: str, cookie: str, timeout: float, logger: Any) -> dict[str, Any]:
    try:
        payload = await _get_json(url, cookie, timeout)
    except Exception as exc:  # noqa: BLE001 - 网络问题不该影响主流程
        if logger is not None:
            logger.warning(f"[virtual_activity] 拉取 B 站视频失败 {url}: {exc}")
        return {}
    if payload.get("code") not in (0, None):
        if logger is not None:
            logger.warning(f"[virtual_activity] B 站接口返回 {payload.get('code')}: {payload.get('message')}")
        return {}
    return payload


async def fetch_pool(
    rids: list[int],
    cookie: str = "",
    timeout: float = 10.0,
    logger: Any = None,
) -> list[dict[str, Any]]:
    """抓候选视频：有分区时用分区排行榜（质量高），空则退回分区最新；没分区用综合热门。"""
    if not rids:
        payload = await _safe_get(POPULAR_URL, cookie, timeout, logger)
        return extract_pool(payload)

    pool: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rid in rids[:3]:
        payload = await _safe_get(REGION_URL.format(rid=rid), cookie, timeout, logger)
        videos = extract_region(payload)
        if not videos:
            fallback = await _safe_get(NEWLIST_URL.format(rid=rid), cookie, timeout, logger)
            videos = extract_pool(fallback)
        for video in videos:
            if video["bvid"] in seen:
                continue
            seen.add(video["bvid"])
            pool.append(video)
    return pool


@dataclass
class Category:
    """一条偏好：名字 + 分区 + 关键词 + 权重。"""

    name: str
    partitions: list[int] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    weight: float = 1.0


def _split_terms(text: str) -> list[str]:
    return [item.strip() for item in str(text or "").replace("，", ",").replace("、", ",").split(",") if item.strip()]


def parse_categories(spec: Any, weight: float = 1.0) -> list[Category]:
    """解析多行偏好配置，每行 ``名称 | 分区,分区 | 关键词,关键词``。

    后两段可省，例如只写 ``读书书评 | | 读书,书评``。
    """
    text = str(spec or "") if not isinstance(spec, (list, tuple)) else "\n".join(str(item) for item in spec)
    categories: list[Category] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        name = parts[0] if parts else ""
        partitions = resolve_rids(parts[1]) if len(parts) > 1 else []
        keywords = _split_terms(parts[2]) if len(parts) > 2 else []
        if not partitions and not keywords:
            continue
        categories.append(
            Category(
                name=name or (parts[1] if len(parts) > 1 else "偏好"),
                partitions=partitions,
                keywords=keywords,
                weight=max(0.1, weight),
            )
        )
    return categories


def pick_category(categories: list[Category], rng: random.Random | None = None) -> Category | None:
    if not categories:
        return None
    chooser = rng or random
    weights = [max(0.1, category.weight) for category in categories]
    return chooser.choices(categories, weights=weights, k=1)[0]


def _in_duration(video: dict[str, Any], low: int, high: int) -> bool:
    duration = int(video.get("duration") or 0)
    if low > 0 and duration < low:
        return False
    if high > 0 and duration > high:
        return False
    return True


def _duration_distance(video: dict[str, Any], low: int, high: int) -> int:
    duration = int(video.get("duration") or 0)
    if low > 0 and duration < low:
        return low - duration
    if high > 0 and duration > high:
        return duration - high
    return 0


def pick_videos(
    pool: list[dict[str, Any]],
    *,
    keywords: list[str] | None = None,
    limit: int = 2,
    rng: random.Random | None = None,
    watched: list[str] | None = None,
    min_duration: int = 0,
    max_duration: int = 0,
    min_views: int = 0,
    require_keyword: bool = False,
) -> list[dict[str, Any]]:
    """按偏好挑几条：先过滤看过的、关键词、时长，再按播放量取头部随机抽。"""
    chooser = rng or random
    watched_set = {str(item) for item in (watched or [])}
    fresh = [video for video in pool if video["bvid"] not in watched_set]
    if not fresh:
        return []

    ranged = [video for video in fresh if _in_duration(video, min_duration, max_duration)]
    if ranged:
        fresh = ranged
    elif min_duration or max_duration:
        # 一条都不在区间里时，退而求其次挑最接近的
        fresh = sorted(fresh, key=lambda video: _duration_distance(video, min_duration, max_duration))[:15]

    matched = fresh
    keywords = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if keywords:
        lowered = [k.lower() for k in keywords]
        hit = [
            video
            for video in fresh
            if any(word in video["title"].lower() for word in lowered)
        ]
        if hit:
            matched = hit
        elif require_keyword:
            # 这条偏好明确写了关键词，一条都没命中就换别的偏好，不硬凑
            return []

    if min_views > 0:
        popular = [video for video in matched if int(video.get("views") or 0) >= min_views]
        if popular:
            matched = popular

    matched = [video for video in matched if video.get("duration", 0) >= 30] or matched
    matched.sort(key=lambda video: video.get("views", 0), reverse=True)
    candidates = matched[:20]
    if not candidates:
        return []
    count = min(limit, len(candidates))
    picked = chooser.sample(candidates, count) if count < len(candidates) else list(candidates)
    return picked[:limit]
