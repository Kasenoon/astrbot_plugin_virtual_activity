"""房间 / 家具 / 互动效果的数据结构与校验。"""

from __future__ import annotations

import random
import re
from typing import Any

SCHEMA_VERSION = 1

DEFAULT_CANVAS: dict[str, Any] = {
    "width": 1000,
    "height": 700,
    "background": "#f3ece2",
}

PALETTE = [
    "#c8a27a",
    "#a8c0d8",
    "#b8a7d9",
    "#a8d8c0",
    "#d8b4a8",
    "#c9c9a8",
    "#d9c2e8",
]

ID_PATTERN = re.compile(r"^[a-z0-9_\-]{1,40}$")
COLOR_PATTERN = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

EFFECT_DEFAULT_DURATION = 600
EFFECT_MIN_DURATION = 30
EFFECT_MAX_DURATION = 6 * 60 * 60

DEFAULT_ENERGY_DELTA = -6
"""效果没填 energy_delta 时，做一件事默认消耗的精力。"""


def effect_score(effect: dict[str, Any], energy: float) -> float:
    """按当前精力给效果打活动权重：越累越偏向能回精力的选项，回得越多越优先。"""
    weight = max(0.0001, float(effect.get("weight", 1.0) or 1.0))
    try:
        delta = float(effect.get("energy_delta", DEFAULT_ENERGY_DELTA))
    except (TypeError, ValueError):
        delta = DEFAULT_ENERGY_DELTA
    try:
        current = float(energy)
    except (TypeError, ValueError):
        current = 80.0
    tired = max(0.0, min(1.0, (50.0 - current) / 50.0))  # 50 以上为 0，0 时为 1
    magnitude = min(1.0, abs(delta) / 20.0)
    if delta >= 8:
        # 明显回精力的活动才享受“累了更想去”的加成
        return weight * (1.0 + 4.5 * tired * magnitude)
    if delta > 0:
        return weight
    if delta < 0:
        return weight * max(0.08, 1.0 - 3.0 * tired * magnitude)
    return weight


def pick_furniture(
    room: dict[str, Any],
    energy: float,
    rng: random.Random | None = None,
    boosts: dict[str, float] | None = None,
    allowed: Any = None,
) -> dict[str, Any] | None:
    """按精力加权挑一件家具：累的时候更可能挑到能休息的那件。"""
    items = candidates(room)
    if allowed is not None:
        items = [item for item in items if allowed(item)]
    if not items:
        return None
    chooser = rng or random
    weights = []
    for item in items:
        score = max(effect_score(effect, energy) for effect in item["effects"])
        if boosts:
            score *= float(boosts.get(str(item.get("id")), 1.0))
        weights.append(max(0.0001, score))
    return chooser.choices(items, weights=weights, k=1)[0]


def is_tv_furniture(furniture: dict[str, Any]) -> bool:
    effects = furniture.get("effects") if isinstance(furniture.get("effects"), list) else []
    return any(str(effect.get("action") or "") == "tv" for effect in effects)


def is_reading_furniture(furniture: dict[str, Any]) -> bool:
    effects = furniture.get("effects") if isinstance(furniture.get("effects"), list) else []
    return any(str(effect.get("action") or "") == "read_book" for effect in effects)


def tv_effects(furniture: dict[str, Any]) -> list[dict[str, Any]]:
    effects = furniture.get("effects") if isinstance(furniture.get("effects"), list) else []
    return [effect for effect in effects if str(effect.get("action") or "") == "tv"]



def default_room() -> dict[str, Any]:
    """首次启动时写入的示例房间。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "room": {
            "name": "我的房间",
            "canvas": dict(DEFAULT_CANVAS),
            "robots": [{"id": "default", "name": "小鲸鱼", "avatar": ""}],
        },
        "furniture": [
            {
                "id": "bed",
                "name": "床",
                "shape": "rect",
                "x": 60,
                "y": 60,
                "w": 280,
                "h": 170,
                "color": "#b8a7d9",
                "emoji": "🛏️",
                "z": 2,
                "enabled": True,
                "effects": [
                    {
                        "id": "bed_sleep",
                        "name": "睡觉",
                        "desc": "钻进被子里补觉",
                        "prompt_hint": "可能睡得很沉，被子卷成一团",
                        "duration_sec": 1800,
                        "weight": 1.0,
                        "energy_delta": 30,
                        "social_delta": 0,
                    },
                    {
                        "id": "bed_lazy",
                        "name": "赖床",
                        "desc": "醒了但不想起来",
                        "prompt_hint": "盯着天花板发呆，翻个身继续躺",
                        "duration_sec": 900,
                        "weight": 1.0,
                        "energy_delta": 12,
                        "social_delta": 0,
                    },
                ],
            },
            {
                "id": "desk",
                "name": "书桌",
                "shape": "rect",
                "x": 420,
                "y": 70,
                "w": 200,
                "h": 110,
                "color": "#c9a679",
                "emoji": "🖥️",
                "z": 2,
                "enabled": True,
                "effects": [
                    {
                        "id": "desk_code",
                        "name": "写代码",
                        "desc": "坐在电脑前敲敲打打",
                        "prompt_hint": "屏幕上是看不懂的代码，时不时停下来思考",
                        "duration_sec": 1200,
                        "weight": 1.0,
                        "energy_delta": -18,
                        "social_delta": 0,
                    },
                    {
                        "id": "desk_notes",
                        "name": "整理笔记",
                        "desc": "把散落的纸条收进本子里",
                        "prompt_hint": "纸上写满了字，边写边叹气",
                        "duration_sec": 600,
                        "weight": 1.0,
                        "energy_delta": -8,
                        "social_delta": 0,
                    },
                ],
            },
            {
                "id": "window_side",
                "name": "窗边",
                "shape": "rect",
                "x": 780,
                "y": 60,
                "w": 150,
                "h": 140,
                "color": "#a8d8ea",
                "emoji": "🪟",
                "z": 1,
                "enabled": True,
                "effects": [
                    {
                        "id": "window_gaze",
                        "name": "看窗外",
                        "desc": "趴在窗台上看外面的天色",
                        "prompt_hint": "外面有云飘过去，可能还看见一只鸟",
                        "duration_sec": 600,
                        "weight": 1.0,
                        "energy_delta": 3,
                        "social_delta": 0,
                    },
                ],
            },
            {
                "id": "sofa",
                "name": "沙发",
                "shape": "rect",
                "x": 90,
                "y": 330,
                "w": 250,
                "h": 110,
                "color": "#c8a27a",
                "emoji": "🛋️",
                "z": 2,
                "enabled": True,
                "effects": [
                    {
                        "id": "sofa_read",
                        "name": "看书",
                        "desc": "窝在沙发上看书",
                        "prompt_hint": "随手翻开一本旧书，停在某一页",
                        "duration_sec": 600,
                        "weight": 1.0,
                        "energy_delta": -3,
                        "social_delta": 0,
                        "action": "read_book",
                    },
                    {
                        "id": "sofa_rest",
                        "name": "休息",
                        "desc": "在沙发上躺一会儿",
                        "prompt_hint": "眯起眼睛，可能盖了条毯子",
                        "duration_sec": 900,
                        "weight": 1.0,
                        "energy_delta": 15,
                        "social_delta": 0,
                    },
                ],
            },
            {
                "id": "coffee_table",
                "name": "茶几",
                "shape": "rect",
                "x": 380,
                "y": 350,
                "w": 130,
                "h": 90,
                "color": "#b08968",
                "emoji": "☕",
                "z": 1,
                "enabled": True,
                "effects": [
                    {
                        "id": "coffee_sip",
                        "name": "喝咖啡",
                        "desc": "端着杯子小口小口地喝",
                        "prompt_hint": "咖啡有点烫，吹了吹才喝",
                        "duration_sec": 300,
                        "weight": 1.0,
                        "energy_delta": 4,
                        "social_delta": 0,
                    },
                ],
            },
            {
                "id": "fridge",
                "name": "冰箱",
                "shape": "rect",
                "x": 790,
                "y": 300,
                "w": 110,
                "h": 180,
                "color": "#d7e3ec",
                "emoji": "🥛",
                "z": 2,
                "enabled": True,
                "effects": [
                    {
                        "id": "fridge_food",
                        "name": "翻冰箱",
                        "desc": "打开冰箱找点吃的",
                        "prompt_hint": "看了半天，最后只拿了瓶饮料",
                        "duration_sec": 300,
                        "weight": 1.0,
                        "energy_delta": 5,
                        "social_delta": 0,
                    },
                ],
            },
            {
                "id": "tv",
                "name": "电视",
                "shape": "rect",
                "x": 640,
                "y": 74,
                "w": 124,
                "h": 140,
                "color": "#9fb3c8",
                "emoji": "📺",
                "z": 2,
                "enabled": True,
                "effects": [
                    {
                        "id": "tv_watch",
                        "name": "看电视",
                        "desc": "窝在电视前刷 B 站",
                        "prompt_hint": "电视里放着刚挑到的视频",
                        "duration_sec": 1800,
                        "weight": 1.0,
                        "energy_delta": 5,
                        "social_delta": 0,
                        "action": "tv",
                    },
                ],
            },
            {
                "id": "plant",
                "name": "绿植",
                "shape": "rect",
                "x": 640,
                "y": 540,
                "w": 90,
                "h": 90,
                "color": "#7fae8b",
                "emoji": "🪴",
                "z": 1,
                "enabled": True,
                "effects": [],
            },
        ],
    }


# ---------------------------------------------------------------- 小工具


def _as_int(value: Any, default: int) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_text(value: Any, default: str = "", limit: int = 200) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        return default
    return text[:limit]


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _unique_id(candidate: str, used: set[str], prefix: str) -> str:
    base = candidate if ID_PATTERN.match(candidate or "") else ""
    if not base:
        base = prefix
    if base not in used:
        used.add(base)
        return base
    index = 2
    while f"{base}_{index}" in used:
        index += 1
    final = f"{base}_{index}"
    used.add(final)
    return final


# ---------------------------------------------------------------- 校验

_DEFAULT_DELTAS: dict[str, int] | None = None


def _default_deltas() -> dict[str, int]:
    """示例房间各效果的默认 energy_delta，用于老房间平滑升级。"""
    global _DEFAULT_DELTAS
    if _DEFAULT_DELTAS is None:
        table: dict[str, int] = {}
        for furniture in default_room()["furniture"]:
            for effect in furniture["effects"]:
                table[str(effect["id"])] = int(effect.get("energy_delta", DEFAULT_ENERGY_DELTA))
        _DEFAULT_DELTAS = table
    return _DEFAULT_DELTAS


def normalize_effect(raw: Any, furniture_id: str, used_ids: set[str], index: int) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = _as_text(raw.get("name"))
    if not name:
        return None
    raw_id = _as_text(raw.get("id")).lower()
    effect_id = _unique_id(raw_id, used_ids, f"{furniture_id}_e{index + 1}")
    duration = _as_int(raw.get("duration_sec"), EFFECT_DEFAULT_DURATION)
    duration = _clamp(duration, EFFECT_MIN_DURATION, EFFECT_MAX_DURATION)
    weight = _as_float(raw.get("weight"), 1.0)
    if weight <= 0:
        weight = 1.0
    # 老版本 room.json 没有 energy_delta：优先按示例房间里的同名效果补齐
    default_delta = _default_deltas().get(raw_id, DEFAULT_ENERGY_DELTA)
    energy_delta = _as_int(raw.get("energy_delta"), default_delta) if raw.get("energy_delta") is not None else default_delta
    return {
        "id": effect_id,
        "name": name,
        "desc": _as_text(raw.get("desc"), "", 120),
        "prompt_hint": _as_text(raw.get("prompt_hint"), "", 200),
        "duration_sec": duration,
        "weight": round(weight, 3),
        "energy_delta": _clamp(energy_delta, -60, 60),
        "social_delta": _clamp(_as_int(raw.get("social_delta"), 0), -60, 60),
        "action": _as_text(raw.get("action"), "", 16),
    }


def normalize_furniture(raw: Any, index: int, canvas: dict[str, Any], used_ids: set[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = _as_text(raw.get("name"))
    if not name:
        return None
    furniture_id = _unique_id(_as_text(raw.get("id")).lower(), used_ids, f"f{index + 1}")

    width = _clamp(_as_int(raw.get("w"), 120), 20, canvas["width"])
    height = _clamp(_as_int(raw.get("h"), 80), 20, canvas["height"])
    x = _clamp(_as_int(raw.get("x"), 40), 0, max(0, canvas["width"] - width))
    y = _clamp(_as_int(raw.get("y"), 40), 0, max(0, canvas["height"] - height))

    color = _as_text(raw.get("color"), "")
    if not COLOR_PATTERN.match(color):
        color = PALETTE[index % len(PALETTE)]

    effect_used: set[str] = set()
    effects: list[dict[str, Any]] = []
    raw_effects = raw.get("effects")
    if isinstance(raw_effects, list):
        for effect_index, effect_raw in enumerate(raw_effects):
            effect = normalize_effect(effect_raw, furniture_id, effect_used, effect_index)
            if effect:
                effects.append(effect)

    return {
        "id": furniture_id,
        "name": name,
        "shape": _as_text(raw.get("shape"), "rect", 16) or "rect",
        "x": x,
        "y": y,
        "w": width,
        "h": height,
        "color": color,
        "emoji": _as_text(raw.get("emoji"), "", 8),
        "z": _clamp(_as_int(raw.get("z"), 1), -50, 50),
        "enabled": _as_bool(raw.get("enabled"), True),
        "effects": effects,
    }


def normalize_room(raw: Any) -> dict[str, Any]:
    """把任意来源的房间数据整理成合法结构；结构性错误抛 ValueError。"""
    if not isinstance(raw, dict):
        raise ValueError("房间数据必须是 JSON 对象")

    room_raw = raw.get("room") if isinstance(raw.get("room"), dict) else {}
    canvas_raw = room_raw.get("canvas") if isinstance(room_raw.get("canvas"), dict) else {}
    width = _clamp(_as_int(canvas_raw.get("width"), DEFAULT_CANVAS["width"]), 320, 4000)
    height = _clamp(_as_int(canvas_raw.get("height"), DEFAULT_CANVAS["height"]), 240, 4000)
    background = _as_text(canvas_raw.get("background"), DEFAULT_CANVAS["background"], 32)
    if not COLOR_PATTERN.match(background):
        background = DEFAULT_CANVAS["background"]

    robots_raw = room_raw.get("robots")
    robots: list[dict[str, Any]] = []
    robot_used: set[str] = set()
    if isinstance(robots_raw, list):
        for index, item in enumerate(robots_raw):
            if not isinstance(item, dict):
                continue
            robot_id = _unique_id(_as_text(item.get("id")).lower(), robot_used, f"robot{index + 1}")
            avatar_fit = _as_text(item.get("avatar_fit"), "cover", 16).lower()
            if avatar_fit not in {"cover", "contain"}:
                avatar_fit = "cover"
            robots.append(
                {
                    "id": robot_id,
                    "name": _as_text(item.get("name"), robot_id, 40),
                    "avatar": _as_text(item.get("avatar"), "", 200000),
                    "avatar_size": _clamp(_as_int(item.get("avatar_size"), 128), 32, 512),
                    "avatar_fit": avatar_fit,
                }
            )
    if not robots:
        robots = [
            {
                "id": "default",
                "name": _as_text(room_raw.get("name"), "小鲸鱼", 40),
                "avatar": "",
                "avatar_size": 128,
                "avatar_fit": "cover",
            }
        ]

    if not robots[0].get("avatar") and isinstance(raw, dict):
        # 兼容：把机器人图标放在房间顶层的情况
        fallback_avatar = _as_text(raw.get("robot_avatar"), "", 200000)
        if fallback_avatar:
            robots[0]["avatar"] = fallback_avatar

    canvas = {"width": width, "height": height, "background": background}

    furniture_raw = raw.get("furniture")
    furniture: list[dict[str, Any]] = []
    furniture_used: set[str] = set()
    if isinstance(furniture_raw, list):
        for index, item in enumerate(furniture_raw):
            normalized = normalize_furniture(item, index, canvas, furniture_used)
            if normalized:
                furniture.append(normalized)

    migrations_raw = raw.get("migrations")
    migrations = [str(item) for item in migrations_raw] if isinstance(migrations_raw, list) else []
    # 老房间升级：补一台电视（用户删掉后不会再加回来）
    if "tv" not in migrations and not any(str(item.get("id")) == "tv" for item in furniture):
        tv_raw = next((item for item in default_room()["furniture"] if item.get("id") == "tv"), None)
        if tv_raw is not None:
            normalized_tv = normalize_furniture(tv_raw, len(furniture), canvas, furniture_used)
            if normalized_tv:
                furniture.append(normalized_tv)
                migrations.append("tv")

    # 老房间升级：把「看书」接到读书活动上
    if "reading" not in migrations:
        for item in furniture:
            for effect in item.get("effects") or []:
                if effect.get("id") == "sofa_read" and not effect.get("action"):
                    effect["action"] = "read_book"
        migrations.append("reading")

    return {
        "schema_version": SCHEMA_VERSION,
        "room": {
            "name": _as_text(room_raw.get("name"), "我的房间", 40),
            "canvas": canvas,
            "robots": robots,
        },
        "furniture": furniture,
        "migrations": migrations,
    }


# ---------------------------------------------------------------- 查询


def furniture_list(room: dict[str, Any]) -> list[dict[str, Any]]:
    value = room.get("furniture")
    return value if isinstance(value, list) else []


def has_effects(furniture: dict[str, Any]) -> bool:
    effects = furniture.get("effects")
    return isinstance(effects, list) and bool(effects)


def candidates(room: dict[str, Any]) -> list[dict[str, Any]]:
    """可参与随机 / 可互动（绑了行为且启用）的家具。"""
    return [item for item in furniture_list(room) if item.get("enabled", True) and has_effects(item)]


def find_furniture(room: dict[str, Any], key: str) -> dict[str, Any] | None:
    text = str(key or "").strip().strip("「」\"'")
    if not text:
        return None
    items = furniture_list(room)
    for item in items:
        if item.get("id") == text:
            return item
    lowered = text.lower()
    for item in items:
        if str(item.get("name", "")).lower() == lowered:
            return item
    for item in items:
        if lowered in str(item.get("name", "")).lower():
            return item
    if text.isdigit():
        index = int(text) - 1
        if 0 <= index < len(items):
            return items[index]
    return None


def find_effect(furniture: dict[str, Any], key: str) -> dict[str, Any] | None:
    text = str(key or "").strip()
    if not text:
        return None
    effects = furniture.get("effects") if isinstance(furniture.get("effects"), list) else []
    for effect in effects:
        if effect.get("id") == text:
            return effect
    lowered = text.lower()
    for effect in effects:
        if str(effect.get("name", "")).lower() == lowered:
            return effect
    return None


def furniture_center(furniture: dict[str, Any]) -> tuple[float, float]:
    return (
        float(furniture.get("x", 0)) + float(furniture.get("w", 0)) / 2.0,
        float(furniture.get("y", 0)) + float(furniture.get("h", 0)) / 2.0,
    )


def pick_effect(
    furniture: dict[str, Any],
    policy: str,
    rng: random.Random | None = None,
    energy: float | None = None,
) -> dict[str, Any] | None:
    effects = furniture.get("effects") if isinstance(furniture.get("effects"), list) else []
    if not effects:
        return None
    if policy == "first" or len(effects) == 1:
        return effects[0]
    chooser = rng or random
    if energy is None:
        weights = [max(0.0001, float(effect.get("weight", 1.0) or 1.0)) for effect in effects]
    else:
        weights = [max(0.0001, effect_score(effect, energy)) for effect in effects]
    return chooser.choices(effects, weights=weights, k=1)[0]


def robot_of(room: dict[str, Any], robot_id: str) -> dict[str, Any]:
    robots = room.get("room", {}).get("robots") if isinstance(room.get("room"), dict) else None
    if isinstance(robots, list):
        for robot in robots:
            if robot.get("id") == robot_id:
                return robot
    return {"id": robot_id, "name": robot_id, "avatar": ""}


def room_summary(room: dict[str, Any]) -> dict[str, Any]:
    items = furniture_list(room)
    return {
        "name": room.get("room", {}).get("name", ""),
        "canvas": room.get("room", {}).get("canvas", DEFAULT_CANVAS),
        "furniture_count": len(items),
        "interactive_count": len(candidates(room)),
    }
