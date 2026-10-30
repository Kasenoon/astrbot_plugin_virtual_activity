"""把房间状态拼成一段注入文本（纯函数，便于测试）。"""

from __future__ import annotations

from typing import Any

MARKER = "[自身状态 · 虚拟房间]"

CLOSING = "被问到你人在哪、在做什么、刚才干了什么时，按上面的状态自然地回答；不要提这段说明本身。"

# 只放「明确在问状态」的词，全部 ≥2 字；
# 单字词（吃/冷/热/睡）和单字家具名（床）太容易误命中（临床、热更新、吃灰…），一律不收。
STATE_KEYWORDS = (
    "在干嘛",
    "干嘛呢",
    "在做什么",
    "做什么呢",
    "干什么",
    "忙什么",
    "忙吗",
    "在哪",
    "哪里",
    "哪儿",
    "哪个屋",
    "房间",
    "屋里",
    "屋子",
    "家里",
    "沙发",
    "床上",
    "躺着",
    "睡着",
    "累不累",
    "精力",
    "心情",
    "无聊",
    "陪我",
    "想我",
    "天气",
    "气温",
    "外面",
    "下雨",
    "下雪",
    "好热",
    "好冷",
    "冷不冷",
    "热不热",
    "吃饭",
    "吃了",
    "饿了",
    "电视",
    "看什么",
    "视频",
    "谷时",
    "看的书",
    "读到",
    "小说",
    "书里",
    "剧情",
)

MIN_KEYWORD_LEN = 2


def should_inject(text: str, extra_keywords: list[str] | None = None) -> bool:
    """按需注入：消息里明确提到状态相关话题时才把自身状态塞进上下文。"""
    content = str(text or "").strip()
    if not content:
        return False
    lowered = content.lower()
    for keyword in STATE_KEYWORDS:
        if keyword in content:
            return True
    for keyword in extra_keywords or []:
        keyword = str(keyword or "").strip()
        # 单个字的家具名（比如「床」）会把「临床」「床位」也命中，直接跳过
        if len(keyword) < MIN_KEYWORD_LEN:
            continue
        if keyword.lower() in lowered:
            return True
    return False


def _fmt_number(value: Any) -> str:
    try:
        return f"{float(value):.0f}"
    except (TypeError, ValueError):
        return "?"


def build_state_block(
    *,
    room_name: str,
    now_text: str,
    furniture_name: str | None = None,
    effect_name: str | None = None,
    remaining_minutes: int | None = None,
    position: str | None = None,
    energy: Any = None,
    social: Any = None,
    recent: list[str] | None = None,
    last_text: str | None = None,
    last_text_at: str | None = None,
    social_threshold: int = 70,
    weather: str | None = None,
    reading: str | None = None,
) -> str:
    lines = [
        MARKER,
        f"你此刻待在一间叫「{room_name or '我的房间'}」的屋子里，这是你真实的生活状态，不是扮演。",
    ]
    if now_text:
        lines.append(f"- 现在时间：{now_text}")

    if furniture_name and effect_name:
        tail = f"，大约还有 {remaining_minutes} 分钟" if remaining_minutes else ""
        lines.append(f"- 你正在：「{furniture_name}」{effect_name}{tail}")
    else:
        lines.append("- 你正在：闲着，在屋里待着")

    if position:
        lines.append(f"- 位置：{position}")

    if weather:
        lines.append(f"- 窗外：{weather}")

    if reading:
        lines.append(f"- 在读：{reading}")

    if energy is not None or social is not None:
        lines.append(f"- 身体：精力 {_fmt_number(energy)}/100，想社交 {_fmt_number(social)}/100")

    try:
        social_value = float(social)
    except (TypeError, ValueError):
        social_value = 0.0
    if social_value >= social_threshold:
        lines.append("- 心情：挺久没和主人说话了，有点想他")

    if recent:
        tail_items = [item for item in recent[-4:] if item]
        if tail_items:
            lines.append("- 最近：" + " → ".join(tail_items))

    if last_text:
        suffix = f"（{last_text_at}）" if last_text_at else ""
        lines.append(f"- 你最近一次说：「{last_text}」{suffix}")

    lines.append(CLOSING)
    return "\n".join(lines)


def merge_into_system_prompt(system_prompt: str | None, block: str) -> str:
    """幂等追加：已经注入过（同一轮 agent 多次请求）就不重复。"""
    current = system_prompt or ""
    if not block:
        return current
    if MARKER in current:
        return current
    if not current.strip():
        return block + "\n"
    return f"{current}\n\n{block}\n"
