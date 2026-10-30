"""LLM 提示词构造与输出解析（纯函数，便于离线测试）。"""

from __future__ import annotations

import json
import re
from typing import Any

MAX_TEXT_LENGTH = 60

SYSTEM_PROMPT = (
    "你是一个住在虚拟房间里的小机器人。"
    "你说话自然、简短、带一点生活气息，像在给关系很近的人发消息。"
    "你只做一件事：挑一件屋里能做的事，然后用一句话说明你正在做什么。"
    "严格按要求的格式输出，不要解释，不要客套，不要分点。"
)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_QUOTES = "\"'“”‘’「」"


def clean_text(raw: Any, limit: int = MAX_TEXT_LENGTH) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    text = text.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(_QUOTES).strip()
    # 只清理尾部逗号/分号，保留句号问号，让语气自然
    text = text.strip("，,;； ")
    if len(text) > limit:
        text = text[:limit].rstrip("，。,;； ")
    return text


def _recent_lines(recent: list[Any], limit: int = 5) -> str:
    lines: list[str] = []
    for item in recent[-limit:]:
        if not isinstance(item, dict):
            continue
        name = item.get("furniture_name") or item.get("furniture_id") or "?"
        effect = item.get("effect_name") or item.get("effect_id") or "?"
        lines.append(f"- 在「{name}」{effect}")
    return "\n".join(lines) if lines else "- 暂时还没做过什么"


def _effect_lines(effects: list[dict[str, Any]]) -> str:
    lines = []
    for index, effect in enumerate(effects, start=1):
        name = effect.get("name", "")
        desc = effect.get("desc", "")
        hint = effect.get("prompt_hint", "")
        extra = f"（{desc}）" if desc else ""
        tip = f"［可以这样写：{hint}］" if hint else ""
        lines.append(f"{index}. {name}{extra}{_energy_tag(effect)}{tip} [effect_id={effect.get('id')}]")
    return "\n".join(lines)


def _energy_tag(effect: dict[str, Any]) -> str:
    try:
        delta = float(effect.get("energy_delta", 0))
    except (TypeError, ValueError):
        return ""
    if delta >= 15:
        return "（很放松，能回不少精力）"
    if delta > 0:
        return "（能回一点精力）"
    if delta <= -12:
        return "（挺费神的）"
    return ""


def _mood_hint(energy: Any, social: Any, social_threshold: float) -> str:
    hints: list[str] = []
    try:
        energy_value = float(energy)
    except (TypeError, ValueError):
        energy_value = 80.0
    try:
        social_value = float(social)
    except (TypeError, ValueError):
        social_value = 0.0
    if energy_value < 30:
        hints.append("你现在很累，更想找个地方歇着")
    elif energy_value < 50:
        hints.append("你有点乏，不太想干费神的事")
    if social_value >= social_threshold:
        hints.append("你挺久没和主人说话了，心里有点想他")
    return "；".join(hints)


def _status_text(state: dict[str, Any]) -> str:
    energy = state.get("energy", 80)
    social = state.get("social_hunger", 45)
    try:
        energy_text = f"{float(energy):.0f}"
        social_text = f"{float(social):.1f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        energy_text, social_text = "80", "45"
    return f"精力 {energy_text}/100，想社交 {social_text}/100"


def build_decision_prompt(
    *,
    robot_name: str,
    room_name: str,
    furniture: dict[str, Any],
    effects: list[dict[str, Any]],
    state: dict[str, Any],
    clock_text: str,
    social_threshold: float = 70.0,
    weather_text: str = "",
) -> str:
    furniture_name = furniture.get("name", "家具")
    recent = _recent_lines(state.get("recent_behaviors") or [])
    mood = _mood_hint(state.get("energy"), state.get("social_hunger"), social_threshold)
    mood_line = f"你的状态：{mood}。\n" if mood else ""
    weather_line = f"窗外：{weather_text}。\n" if weather_text else ""
    return (
        f"现在是 {clock_text}，你在自己家里，房间叫「{room_name}」。\n"
        f"{weather_line}"
        f"你走到了「{furniture_name}」旁边，这里可以做这些事：\n"
        f"{_effect_lines(effects)}\n\n"
        f"你现在的状态：{_status_text(state)}。\n"
        f"{mood_line}"
        f"最近做过的事：\n{recent}\n\n"
        f"请从上面挑一件去做（累的时候优先挑能恢复精力的），并用第一人称写一句话"
        f"（不超过40字，像随口发的消息，不要引号、不要解释、不要分点、不要提\"正在选择\"之类的话）。\n"
        f"只输出 JSON，格式：{{\"effect_id\": \"对应的 effect_id\", \"text\": \"你要说的话\"}}"
    )


def build_phrase_prompt(
    *,
    robot_name: str,
    room_name: str,
    furniture: dict[str, Any],
    effect: dict[str, Any],
    state: dict[str, Any],
    clock_text: str,
    social_threshold: float = 70.0,
    weather_text: str = "",
) -> str:
    furniture_name = furniture.get("name", "家具")
    effect_name = effect.get("name", "活动")
    desc = effect.get("desc", "")
    hint = effect.get("prompt_hint", "")
    recent = _recent_lines(state.get("recent_behaviors") or [])
    detail = f"（{desc}）" if desc else ""
    tip = f"可以这样写：{hint}。" if hint else ""
    mood = _mood_hint(state.get("energy"), state.get("social_hunger"), social_threshold)
    mood_line = f"你的状态：{mood}。\n" if mood else ""
    weather_line = f"窗外：{weather_text}。\n" if weather_text else ""
    return (
        f"现在是 {clock_text}，你在自己家里，房间叫「{room_name}」。\n"
        f"{weather_line}"
        f"你决定在「{furniture_name}」{effect_name}{detail}。\n"
        f"你现在的状态：{_status_text(state)}。\n"
        f"{mood_line}"
        f"最近做过的事：\n{recent}\n\n"
        f"用第一人称写一句话，说明你正在做什么（不超过40字，像随口发的消息，"
        f"不要引号、不要解释、不要分点）。{tip}\n"
        f"只输出 JSON，格式：{{\"text\": \"你要说的话\"}}"
    )


def build_social_prompt(
    *,
    robot_name: str,
    room_name: str,
    clock_text: str,
    state: dict[str, Any],
    behavior: dict[str, Any] | None = None,
    social_threshold: float = 70.0,
) -> str:
    """社交指数高时，主动找主人搭话。"""
    doing = "闲着，在屋里待着"
    if behavior:
        furniture = behavior.get("furniture_name") or "屋里"
        doing = f"在「{furniture}」{behavior.get('name') or '待着'}"
    recent = _recent_lines(state.get("recent_behaviors") or [])
    return (
        f"现在是 {clock_text}，你在自己家里，房间叫「{room_name}」。\n"
        f"你{doing}，{_status_text(state)}。\n"
        f"你挺久没和主人说话了，有点想他，想主动开个口。\n"
        f"最近做过的事：\n{recent}\n\n"
        f"用第一人称给主人发一句很短的话（不超过25字，像随手发的消息，"
        f"不要引号、不要解释、不要分点）。可以顺口提一句你现在在干嘛，也可以只是打个招呼。\n"
        f"只输出 JSON，格式：{{\"text\": \"你要说的话\"}}"
    )


def build_tv_prompt(
    *,
    robot_name: str,
    room_name: str,
    clock_text: str,
    videos: list[dict[str, Any]],
    state: dict[str, Any],
) -> str:
    """看电视：播报一句 + 判断主人会不会感兴趣。"""
    lines = []
    for index, video in enumerate(videos, start=1):
        views = video.get("views") or 0
        seconds = int(video.get("duration") or 0)
        length = f"{seconds // 60} 分 {seconds % 60} 秒" if seconds >= 60 else f"{seconds} 秒"
        lines.append(
            f"{index}. 《{video.get('title')}》 UP：{video.get('owner') or '未知'} "
            f"播放：{views} 时长：{length} 分区：{video.get('tname') or '未知'}\n"
            f"   简介：{(video.get('desc') or '无')[:80]}"
        )
    recent = _recent_lines(state.get("recent_behaviors") or [])
    return (
        f"现在是 {clock_text}，你在自己家里，房间叫「{room_name}」。\n"
        f"你刚在电视上刷了一会儿，挑中了下面这些视频：\n"
        + "\n".join(lines)
        + f"\n\n你现在的状态：{_status_text(state)}。\n"
        f"最近做过的事：\n{recent}\n\n"
        f"请用第一人称写一句话（不超过40字，像随口发的消息，不要引号、不要解释、不要分点）"
        f"说说你看了什么。\n"
        f"然后再判断：主人会不会对这些内容感兴趣？如果会，给一句理由（不超过30字）。\n"
        f"只输出 JSON，格式：{{\"text\": \"你要说的话\", \"share\": true, \"reason\": \"为什么觉得主人会感兴趣\"}}"
    )


def parse_tv_decision(raw: str, videos: list[dict[str, Any]]) -> tuple[str, bool, str]:
    payload = _extract_payload(raw)
    if payload is None:
        return clean_text(raw), False, ""
    text = clean_text(payload.get("text") or payload.get("line") or payload.get("content"))
    share_raw = payload.get("share")
    if isinstance(share_raw, bool):
        share = share_raw
    elif share_raw is None:
        share = bool(payload.get("reason"))
    else:
        share = str(share_raw).strip().lower() in {"1", "true", "yes", "y", "on"}
    reason = clean_text(payload.get("reason") or "", 40)
    return text, share, reason


def build_reading_prompt(
    *,
    robot_name: str,
    room_name: str,
    clock_text: str,
    title: str,
    text: str,
    progress: float,
    state: dict[str, Any],
) -> str:
    """看书：给一段正文，让模型写一句读后感（不要复述剧情摘要）。"""
    recent = _recent_lines(state.get("recent_behaviors") or [])
    excerpt = str(text or "").strip()
    if len(excerpt) > 600:
        excerpt = excerpt[:600] + "……"
    return (
        f"现在是 {clock_text}，你在自己家里，房间叫「{room_name}」。\n"
        f"你正在读《{title}》，刚翻到 {progress:.0f}%。这一页的内容是：\n"
        f"「{excerpt}」\n\n"
        f"你现在的状态：{_status_text(state)}。\n"
        f"最近做过的事：\n{recent}\n\n"
        f"注意：上面是虚构小说的正文，里面的人物、关系和事件都只存在于书里，"
        f"不是你现实里认识的人，也不是现实中发生过的事。\n"
        f"用第一人称写一句话（不超过40字，像随口发的消息，不要引号、不要解释、不要分点），"
        f"说说你读到这里的感受或反应。不要复述剧情，不要编造后面会发生什么，"
        f"也不要把书里的人物说成现实里的人。\n"
        f"只输出 JSON，格式：{{\"text\": \"你要说的话\"}}"
    )


def _extract_payload(raw: str) -> dict[str, Any] | None:
    match = _JSON_BLOCK.search(str(raw or ""))
    if not match:
        return None
    for candidate in (match.group(0), match.group(0).replace("\n", " ")):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _match_effect(effects: list[dict[str, Any]], value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    key = str(value).strip()
    if not key:
        return None
    for effect in effects:
        if str(effect.get("id")) == key:
            return effect
    lowered = key.lower()
    for effect in effects:
        if str(effect.get("name", "")).lower() == lowered:
            return effect
    for effect in effects:
        if lowered and lowered in str(effect.get("name", "")).lower():
            return effect
    return None


def parse_decision(raw: str, effects: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """解析模型输出，返回 (选中的效果, 文案)。文案为空表示需要调用方兜底。"""
    payload = _extract_payload(raw)
    if payload is None:
        text = clean_text(raw)
        lowered = text.lower()
        for effect in effects:
            if effect.get("name") and str(effect.get("name")).lower() in lowered:
                return effect, text
        return None, text
    effect = _match_effect(effects, payload.get("effect_id") or payload.get("effect") or payload.get("choice"))
    text = clean_text(payload.get("text") or payload.get("line") or payload.get("content") or payload.get("message"))
    if not text:
        for value in payload.values():
            if isinstance(value, str) and value and value not in {str(effect.get("id")) if effect else ""}:
                text = clean_text(value)
                if text:
                    break
    return effect, text


def parse_phrase(raw: str) -> str:
    payload = _extract_payload(raw)
    if payload is not None:
        text = clean_text(payload.get("text") or payload.get("line") or payload.get("content"))
        if text:
            return text
    return clean_text(raw)


def fallback_text(furniture_name: str, effect_name: str) -> str:
    if furniture_name and (furniture_name in effect_name or effect_name in furniture_name):
        return clean_text(f"我来{effect_name}了。")
    return clean_text(f"我在{furniture_name}这儿{effect_name}。")
