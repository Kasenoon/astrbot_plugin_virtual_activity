"""核心判定：手动点击与随机 tick 共用的活动管线。"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Protocol

from . import bilibili, llm, models, store as store_module
from .timewin import in_windows, is_allowed, next_allowed, parse_windows
from .weather import Weather

DEFAULT_TICK_MIN = 20
DEFAULT_TICK_MAX = 45
DEFAULT_DURATION = 600
DEFAULT_RECENT_LIMIT = 20


class TextGenerator(Protocol):
    async def __call__(self, system_prompt: str, prompt: str) -> str: ...


@dataclass
class ActivityResult:
    ok: bool
    reason: str
    message: str
    text: str = ""
    furniture: dict[str, Any] | None = None
    effect: dict[str, Any] | None = None
    state: dict[str, Any] | None = None
    source: str = ""
    bypassed: list[str] = field(default_factory=list)
    next_tick_at: str | None = None
    kind: str = "activity"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "message": self.message,
            "text": self.text,
            "furniture": self.furniture,
            "effect": self.effect,
            "state": self.state,
            "source": self.source,
            "bypassed": self.bypassed,
            "next_tick_at": self.next_tick_at,
            "kind": self.kind,
        }


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class VirtualRoomEngine:
    """房间状态机 + 活动执行。

    与 AstrBot 解耦：LLM 与消息发送都通过注入的回调完成，方便离线测试。
    """

    def __init__(
        self,
        store: store_module.RoomStore,
        config: Callable[[str, Any], Any],
        generate: TextGenerator,
        logger: Any = None,
        rng: random.Random | None = None,
        clock: Callable[[], datetime] | None = None,
        video_source: Callable[[list[int], str, float], Awaitable[list[dict[str, Any]]]] | None = None,
        weather_source: Callable[[], Awaitable[Weather | None]] | None = None,
        books: Any = None,
    ) -> None:
        self.store = store
        self._cfg = config
        self._generate = generate
        self._logger = logger
        self.rng = rng or random.Random()
        self._clock = clock or store_module.now_local
        self._lock = asyncio.Lock()
        self._video_source = video_source or self._default_video_source
        self._weather_source = weather_source
        self._books = books

    async def current_weather(self) -> Weather | None:
        if self._weather_source is None:
            return None
        try:
            return await self._weather_source()
        except Exception as exc:  # noqa: BLE001 - 天气失败不该影响活动
            self._log("warning", f"[virtual_activity] 获取天气失败: {exc}")
            return None

    @staticmethod
    def weather_boost(furniture: dict[str, Any], weather: Weather | None) -> float:
        """天气对家具权重的影响：下雨更想趴窗边，天热更想翻冰箱。"""
        if weather is None:
            return 1.0
        effects = furniture.get("effects") if isinstance(furniture.get("effects"), list) else []
        names = str(furniture.get("name") or "") + "".join(str(effect.get("name") or "") for effect in effects)
        boost = 1.0
        if "窗" in names:
            if weather.is_wet():
                boost *= 1.8
            elif weather.is_clear():
                boost *= 1.5
        if "冰箱" in names and weather.is_hot():
            boost *= 1.6
        return boost

    async def _default_video_source(
        self,
        rids: list[int],
        cookie: str,
        timeout: float,
    ) -> list[dict[str, Any]]:
        return await bilibili.fetch_pool(rids, cookie=cookie, timeout=timeout, logger=self._logger)

    # ------------------------------------------------------------ 基础

    def now(self) -> datetime:
        return self._clock()

    def _log(self, level: str, message: str) -> None:
        if self._logger is None:
            return
        handler = getattr(self._logger, level, None)
        if callable(handler):
            handler(message)

    def _int_cfg(self, key: str, default: int) -> int:
        try:
            return int(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    def _float_cfg(self, key: str, default: float) -> float:
        """配置读成小数：社交增长、阈值、回落量都支持 0.5 这种值。"""
        try:
            return float(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    def announce_enabled(self) -> bool:
        """自动活动要不要发消息“报备”。"""
        value = self._cfg("announce_activity", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def should_speak(self, source: str) -> bool:
        """手动触发的指令需要回一句话，自动 tick 看配置。"""
        if source in {"click", "test"}:
            return True
        return self.announce_enabled()

    def _tick_range(self) -> tuple[int, int]:
        low = max(1, self._int_cfg("tick_min_minutes", DEFAULT_TICK_MIN))
        high = max(low, self._int_cfg("tick_max_minutes", DEFAULT_TICK_MAX))
        return low, high

    def active_windows(self) -> list[tuple[int, int]]:
        return parse_windows(str(self._cfg("active_hours", "08:00-20:00") or ""))

    def quiet_windows(self) -> list[tuple[int, int]]:
        return parse_windows(str(self._cfg("quiet_hours", "23:00-08:00") or ""))

    def auto_tick_enabled(self) -> bool:
        value = self._cfg("enable_auto_tick", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def policy(self) -> str:
        value = str(self._cfg("multi_effect_policy", "llm") or "llm").strip().lower()
        return value if value in {"llm", "random", "first"} else "llm"

    def default_duration(self) -> int:
        return max(30, self._int_cfg("default_duration_sec", DEFAULT_DURATION))

    def recent_limit(self) -> int:
        return max(1, self._int_cfg("recent_limit", DEFAULT_RECENT_LIMIT))

    # ------------------------------------------------------------ 状态

    def _refresh(self, state: dict[str, Any], now: datetime) -> dict[str, Any]:
        """内存里清理已过期的活动，不落盘（等下一次写入时自然持久化）。"""
        ends_at = store_module.parse_iso(state.get("ends_at"))
        if ends_at is not None and now >= ends_at:
            state["behavior"] = None
            state["location"] = None
            state["started_at"] = None
            state["ends_at"] = None
        return state

    def load_state(self, robot_id: str = "default") -> dict[str, Any]:
        return self._refresh(self.store.load_state(robot_id), self.now())

    def is_busy(self, state: dict[str, Any]) -> bool:
        ends_at = store_module.parse_iso(state.get("ends_at"))
        return bool(state.get("behavior")) and ends_at is not None and ends_at > self.now()

    def busy_remaining_sec(self, state: dict[str, Any]) -> int:
        ends_at = store_module.parse_iso(state.get("ends_at"))
        if ends_at is None:
            return 0
        return max(0, int((ends_at - self.now()).total_seconds()))

    def _window_ok(self, moment: datetime) -> bool:
        return is_allowed(moment, self.active_windows(), self.quiet_windows())

    def _next_tick_after(self, base: datetime) -> str | None:
        if not self.auto_tick_enabled():
            return None
        low, high = self._tick_range()
        active = self.active_windows()
        quiet = self.quiet_windows()
        delay = self.rng.uniform(low * 60.0, high * 60.0)
        target = base + timedelta(seconds=delay)
        allowed = next_allowed(target, active, quiet)
        if allowed != target:
            # 被时间窗推到窗口边界时，加一点随机抖动，避免每天都卡在 08:00 整
            jitter = timedelta(seconds=self.rng.uniform(0.0, max(60.0, (high - low) * 60.0)))
            candidate = allowed + jitter
            target = candidate if is_allowed(candidate, active, quiet) else allowed
        else:
            target = allowed
        return store_module.isoformat(target)

    def reschedule(self, robot_id: str = "default", after: datetime | None = None) -> str | None:
        state = self.load_state(robot_id)
        base = self.now()
        if after is not None and after > base:
            base = after
        state["next_tick_at"] = self._next_tick_after(base)
        self.store.save_state(state)
        return state["next_tick_at"]

    def _social_growth(self, minutes: float) -> float:
        """社交指数按分钟随机增长，支持小数配置（比如每分钟 +0.2~+0.5）。"""
        low = max(0.0, self._float_cfg("social_grow_min", 1.0))
        high = max(low, self._float_cfg("social_grow_max", 2.0))
        if minutes <= 0:
            return 0.0
        gain = self.rng.uniform(low, high) * min(minutes, 24 * 60 * 7)
        return gain

    def _apply_body_state(self, state: dict[str, Any], now: datetime, effect: dict[str, Any] | None = None) -> None:
        """精力随时间缓慢恢复、社交随时间随机上涨；做了事再按效果结算精力。"""
        updated = store_module.parse_iso(state.get("updated_at")) or now
        minutes = _clamp((now - updated).total_seconds() / 60.0, 0.0, 60.0 * 24 * 7)
        try:
            energy = float(state.get("energy", 80.0))
        except (TypeError, ValueError):
            energy = 80.0
        try:
            social = float(state.get("social_hunger", 45.0))
        except (TypeError, ValueError):
            social = 45.0

        recover = 0.5
        try:
            recover = float(self._cfg("energy_recover_per_min", 0.5))
        except (TypeError, ValueError):
            pass

        energy = _clamp(energy + max(0.0, recover) * minutes, 0.0, 100.0)
        social = _clamp(social + self._social_growth(minutes), 0.0, 100.0)

        if effect is not None:
            try:
                delta = float(effect.get("energy_delta", models.DEFAULT_ENERGY_DELTA))
            except (TypeError, ValueError):
                delta = models.DEFAULT_ENERGY_DELTA
            energy = _clamp(energy + delta, 0.0, 100.0)

        state["energy"] = round(energy, 1)
        state["social_hunger"] = round(social, 1)

    def _append_recent(self, state: dict[str, Any], furniture: dict[str, Any], effect: dict[str, Any], now: datetime) -> None:
        recent = state.get("recent_behaviors")
        if not isinstance(recent, list):
            recent = []
        recent.append(
            {
                "furniture_id": furniture.get("id"),
                "furniture_name": furniture.get("name"),
                "effect_id": effect.get("id"),
                "effect_name": effect.get("name"),
                "at": store_module.isoformat(now),
            }
        )
        limit = self.recent_limit()
        state["recent_behaviors"] = recent[-limit:]

    def clear_behavior(self, robot_id: str = "default") -> dict[str, Any]:
        state = self.load_state(robot_id)
        state["behavior"] = None
        state["location"] = None
        state["started_at"] = None
        state["ends_at"] = None
        return self.store.save_state(state)

    def record_error(self, robot_id: str, message: str) -> None:
        """把发送/调度层面的错误写进状态，方便 /回家 里直接看到。"""
        state = self.load_state(robot_id)
        state["last_error"] = str(message).strip()[:200] or None
        self.store.save_state(state)

    # ------------------------------------------------------------ 文案

    async def _decide(self, robot_name: str, room_name: str, furniture: dict[str, Any], effects: list[dict[str, Any]], state: dict[str, Any], now: datetime, weather_text: str = "") -> tuple[dict[str, Any] | None, str]:
        clock_text = now.strftime("%H:%M")
        prompt = llm.build_decision_prompt(
            robot_name=robot_name,
            room_name=room_name,
            furniture=furniture,
            effects=effects,
            state=state,
            clock_text=clock_text,
            social_threshold=self._float_cfg("social_talk_threshold", 70.0),
            weather_text=weather_text,
        )
        # 结构化输出不一定一次就对：第一次解析失败时补一句更硬的格式要求重试一次
        for attempt in range(2):
            try:
                raw = await self._generate(llm.SYSTEM_PROMPT, prompt)
            except Exception as exc:  # noqa: BLE001 - 模型层不应影响主流程
                self._log("warning", f"[virtual_activity] 选择效果失败: {exc}")
                return None, ""
            effect, text = llm.parse_decision(raw, effects)
            if effect is not None:
                return effect, text
            if attempt == 0:
                prompt = (
                    prompt
                    + "\n上一次输出格式不对。这一次必须只输出一个 JSON 对象，"
                    '形如 {"effect_id": "上面某个 effect_id", "text": "一句话"}，不要任何其它文字。'
                )
                self._log("info", "[virtual_activity] 效果选择输出不合法，重试一次")
        return None, ""

    async def _phrase(self, robot_name: str, room_name: str, furniture: dict[str, Any], effect: dict[str, Any], state: dict[str, Any], now: datetime, weather_text: str = "") -> str:
        prompt = llm.build_phrase_prompt(
            robot_name=robot_name,
            room_name=room_name,
            furniture=furniture,
            effect=effect,
            state=state,
            clock_text=now.strftime("%H:%M"),
            social_threshold=self._float_cfg("social_talk_threshold", 70.0),
            weather_text=weather_text,
        )
        try:
            raw = await self._generate(llm.SYSTEM_PROMPT, prompt)
        except Exception as exc:  # noqa: BLE001
            self._log("warning", f"[virtual_activity] 生成文案失败: {exc}")
            return ""
        return llm.parse_phrase(raw)

    # ------------------------------------------------------------ 电视

    def tv_windows(self, now: datetime) -> list[tuple[int, int]]:
        if now.weekday() >= 5:
            spec = str(self._cfg("tv_hours_weekend", "00:00-24:00") or "")
        else:
            spec = str(self._cfg("tv_hours_weekday", "12:00-14:00,18:00-22:00") or "")
        return parse_windows(spec)

    def tv_allowed(self, now: datetime) -> bool:
        windows = self.tv_windows(now)
        if not windows:
            return False
        return in_windows(now.hour * 60 + now.minute, windows)

    def tv_hours_text(self, now: datetime) -> str:
        kind = "周末/节假日" if now.weekday() >= 5 else "工作日"
        if now.weekday() >= 5:
            spec = str(self._cfg("tv_hours_weekend", "00:00-24:00") or "")
        else:
            spec = str(self._cfg("tv_hours_weekday", "12:00-14:00,18:00-22:00") or "")
        windows = parse_windows(spec)
        if not windows:
            return f"{kind}不开放"
        parts = [f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}" for start, end in windows]
        return f"{kind} {'、'.join(parts)}"

    @staticmethod
    def _tv_date(now: datetime) -> str:
        return now.strftime("%Y-%m-%d")

    def tv_used_today(self, state: dict[str, Any], now: datetime) -> int:
        if str(state.get("tv_date") or "") != self._tv_date(now):
            return 0
        try:
            return max(0, int(state.get("tv_watched") or 0))
        except (TypeError, ValueError):
            return 0

    def tv_remaining_today(self, state: dict[str, Any], now: datetime) -> int:
        limit = max(0, self._int_cfg("tv_daily_limit", 3))
        return max(0, limit - self.tv_used_today(state, now))

    async def _run_tv(
        self,
        room: dict[str, Any],
        state: dict[str, Any],
        furniture: dict[str, Any],
        effect: dict[str, Any],
        source: str,
        bypass: bool,
        now: datetime,
        bypassed: list[str],
        speak: bool = True,
    ) -> ActivityResult:
        if not bypass:
            if not self.tv_allowed(now):
                return ActivityResult(
                    False,
                    "tv_closed",
                    f"电视只在谷时才能看（{self.tv_hours_text(now)}）。",
                    state=state,
                    furniture=furniture,
                    effect=effect,
                    source=source,
                )
            if self.tv_remaining_today(state, now) <= 0:
                return ActivityResult(
                    False,
                    "tv_limit",
                    "今天电视已经看够了。",
                    state=state,
                    furniture=furniture,
                    effect=effect,
                    source=source,
                )

        per_session = max(1, self._int_cfg("tv_videos_per_session", 2))
        if bypass:
            count = per_session
        else:
            count = min(per_session, self.tv_remaining_today(state, now))

        rids = bilibili.resolve_rids(self._cfg("tv_partitions", ""))
        global_keywords = self._cfg("tv_keywords", []) or []
        if isinstance(global_keywords, str):
            global_keywords = [item.strip() for item in global_keywords.replace("，", ",").split(",") if item.strip()]

        categories = bilibili.parse_categories(
            self._cfg("tv_prefer_lines", ""), weight=2.0
        ) + bilibili.parse_categories(self._cfg("tv_ok_lines", ""), weight=1.0)

        min_duration = max(0, self._int_cfg("tv_duration_min_minutes", 3)) * 60
        max_duration = max(0, self._int_cfg("tv_duration_max_minutes", 8)) * 60
        if max_duration and min_duration > max_duration:
            min_duration, max_duration = max_duration, min_duration

        cookie = str(self._cfg("tv_cookie", "") or "").strip()
        if cookie and "=" not in cookie:
            cookie = f"SESSDATA={cookie}"
        try:
            timeout = float(self._cfg("tv_timeout_sec", 10))
        except (TypeError, ValueError):
            timeout = 10.0

        cache: dict[tuple[int, ...], list[dict[str, Any]]] = {}

        async def pool_for(partition_rids: list[int]) -> list[dict[str, Any]]:
            key = tuple(partition_rids)
            if key not in cache:
                try:
                    cache[key] = await self._video_source(list(key), cookie, timeout)
                except Exception as exc:  # noqa: BLE001
                    self._log("warning", f"[virtual_activity] 拉取电视内容失败: {exc}")
                    cache[key] = []
            return cache[key]

        videos: list[dict[str, Any]] = []
        attempts = 0
        max_attempts = max(5, count * 4)
        while len(videos) < count and attempts < max_attempts:
            attempts += 1
            category = bilibili.pick_category(categories, self.rng) if categories else None
            watched_now = list(state.get("tv_watched_bvids") or []) + [video["bvid"] for video in videos]
            picked: list[dict[str, Any]] = []

            if category is not None:
                keywords_for_pick = list(category.keywords) + list(global_keywords)
                # 优先从综合热门里按分区挑，热门内容质量更稳
                popular = await pool_for([])
                scoped = [video for video in popular if video.get("tid") in category.partitions]
                if scoped:
                    picked = bilibili.pick_videos(
                        scoped,
                        keywords=keywords_for_pick,
                        limit=1,
                        rng=self.rng,
                        watched=watched_now,
                        min_duration=min_duration,
                        max_duration=max_duration,
                        min_views=self._int_cfg("tv_min_views", 1000),
                        require_keyword=False,
                    )
                if not picked:
                    pool = await pool_for(category.partitions)
                    picked = bilibili.pick_videos(
                        pool,
                        keywords=keywords_for_pick,
                        limit=1,
                        rng=self.rng,
                        watched=watched_now,
                        min_duration=min_duration,
                        max_duration=max_duration,
                        min_views=self._int_cfg("tv_min_views", 1000),
                        require_keyword=False,
                    )
            else:
                pool = await pool_for(rids)
                picked = bilibili.pick_videos(
                    pool,
                    keywords=list(global_keywords),
                    limit=1,
                    rng=self.rng,
                    watched=watched_now,
                    min_duration=min_duration,
                    max_duration=max_duration,
                    min_views=self._int_cfg("tv_min_views", 1000),
                )

            if picked:
                videos.append(picked[0])

        if not videos and categories:
            # 一条都没挑到就退一步：从综合热门里随便拿一个在时长区间里的
            popular = await pool_for([])
            videos = bilibili.pick_videos(
                popular,
                limit=count,
                rng=self.rng,
                watched=list(state.get("tv_watched_bvids") or []),
                min_duration=min_duration,
                max_duration=max_duration,
                min_views=self._int_cfg("tv_min_views", 1000),
            )

        if not videos:
            return ActivityResult(
                False,
                "tv_empty",
                "电视开着，但今天没翻到什么想看的。",
                state=state,
                furniture=furniture,
                effect=effect,
                source=source,
            )

        robot = models.robot_of(room, str(state.get("robot_id") or "default"))
        robot_name = str(robot.get("name") or "小鲸鱼")
        room_name = str(room.get("room", {}).get("name") or "我的房间")

        narration, share, reason = "", False, ""
        message = ""
        if speak:
            prompt = llm.build_tv_prompt(
                robot_name=robot_name,
                room_name=room_name,
                clock_text=now.strftime("%H:%M"),
                videos=videos,
                state=state,
            )
            try:
                raw = await self._generate(llm.SYSTEM_PROMPT, prompt)
                narration, share, reason = llm.parse_tv_decision(raw, videos)
            except Exception as exc:  # noqa: BLE001
                self._log("warning", f"[virtual_activity] 电视文案生成失败: {exc}")
            if not narration:
                narration = f"我在电视上看了《{videos[0]['title']}》。"

            message = narration
            if share and videos:
                lines = [narration, ""]
                for video in videos[:3]:
                    lines.append(f"·《{video['title']}》 {video['url']}")
                if reason:
                    lines.append(f"觉得你会感兴趣：{reason}")
                message = "\n".join(lines)

        duration = max(30, int(effect.get("duration_sec") or self.default_duration()))
        ends_at = now + timedelta(seconds=duration)
        center_x, center_y = models.furniture_center(furniture)

        state["behavior"] = {
            "furniture_id": furniture.get("id"),
            "furniture_name": furniture.get("name"),
            "effect_id": effect.get("id"),
            "name": effect.get("name"),
        }
        state["location"] = {
            "furniture_id": furniture.get("id"),
            "x": round(center_x, 1),
            "y": round(center_y, 1),
        }
        state["position"] = dict(state["location"])
        state["started_at"] = store_module.isoformat(now)
        state["ends_at"] = store_module.isoformat(ends_at)
        if narration:
            state["last_text"] = narration
            state["last_text_at"] = store_module.isoformat(now)
        state["last_trigger_source"] = source
        state["last_error"] = None
        self._apply_body_state(state, now, effect)
        self._append_recent(state, furniture, effect, now)

        # 电视专属计数：当天看了几个、看过哪些
        used = self.tv_used_today(state, now)
        state["tv_date"] = self._tv_date(now)
        state["tv_watched"] = used + len(videos)
        watched = state.get("tv_watched_bvids")
        if not isinstance(watched, list):
            watched = []
        watched.extend(video["bvid"] for video in videos)
        state["tv_watched_bvids"] = watched[-50:]

        base = ends_at if ends_at > now else now
        state["next_tick_at"] = self._next_tick_after(base)
        saved = self.store.save_state(state)

        self._log(
            "info",
            f"[virtual_activity] {source} -> 电视看了 {len(videos)} 个视频: "
            + " / ".join(video["title"] for video in videos),
        )
        return ActivityResult(
            True,
            "ok",
            message,
            text=message,
            furniture=furniture,
            effect=effect,
            state=saved,
            source=source,
            bypassed=bypassed,
            next_tick_at=saved.get("next_tick_at"),
            kind="tv",
        )

    # ------------------------------------------------------------ 读书

    async def _run_reading(
        self,
        room: dict[str, Any],
        state: dict[str, Any],
        furniture: dict[str, Any],
        effect: dict[str, Any],
        source: str,
        now: datetime,
        bypassed: list[str],
        speak: bool = True,
    ) -> ActivityResult:
        if self._books is None:
            return ActivityResult(
                False, "no_book", "书架还没准备好。", state=state, furniture=furniture, source=source
            )

        try:
            chars = max(100, min(3000, self._int_cfg("read_chars", 500)))
        except (TypeError, ValueError):
            chars = 500
        loop_cfg = self._cfg("read_loop", True)
        loop = loop_cfg if isinstance(loop_cfg, bool) else str(loop_cfg).strip().lower() in {"1", "true", "yes", "on"}

        chunk = self._books.read_next(chars=chars, loop=loop)
        if not chunk:
            return ActivityResult(
                False,
                "no_book",
                "书架是空的，或者这本已经读完了。",
                state=state,
                furniture=furniture,
                effect=effect,
                source=source,
            )

        robot = models.robot_of(room, str(state.get("robot_id") or "default"))
        robot_name = str(robot.get("name") or "小鲸鱼")
        room_name = str(room.get("room", {}).get("name") or "我的房间")

        narration = ""
        if speak:
            prompt = llm.build_reading_prompt(
                robot_name=robot_name,
                room_name=room_name,
                clock_text=now.strftime("%H:%M"),
                title=chunk["title"],
                text=chunk["text"],
                progress=chunk["progress"],
                state=state,
            )
            try:
                raw = await self._generate(llm.SYSTEM_PROMPT, prompt)
                narration = llm.parse_phrase(raw)
            except Exception as exc:  # noqa: BLE001
                self._log("warning", f"[virtual_activity] 读书文案生成失败: {exc}")
            if not narration:
                narration = f"我在读《{chunk['title']}》，刚翻到 {chunk['progress']:.0f}%。"

        excerpt = str(chunk["text"]).replace("\n", " ").strip()
        for index, char in enumerate(excerpt[:60]):
            if char in "。！？…":
                excerpt = excerpt[: index + 1]
                break
        else:
            excerpt = excerpt[:60]

        duration = max(30, int(effect.get("duration_sec") or self.default_duration()))
        ends_at = now + timedelta(seconds=duration)
        center_x, center_y = models.furniture_center(furniture)

        state["behavior"] = {
            "furniture_id": furniture.get("id"),
            "furniture_name": furniture.get("name"),
            "effect_id": effect.get("id"),
            "name": effect.get("name"),
        }
        state["location"] = {
            "furniture_id": furniture.get("id"),
            "x": round(center_x, 1),
            "y": round(center_y, 1),
        }
        state["position"] = dict(state["location"])
        state["started_at"] = store_module.isoformat(now)
        state["ends_at"] = store_module.isoformat(ends_at)
        state["book_title"] = chunk["title"]
        state["book_excerpt"] = excerpt
        state["read_progress"] = chunk["progress"]
        if narration:
            state["last_text"] = narration
            state["last_text_at"] = store_module.isoformat(now)
        state["last_trigger_source"] = source
        state["last_error"] = None
        self._apply_body_state(state, now, effect)
        self._append_recent(state, furniture, effect, now)

        base = ends_at if ends_at > now else now
        state["next_tick_at"] = self._next_tick_after(base)
        saved = self.store.save_state(state)

        self._log(
            "info",
            f"[virtual_activity] {source} -> 读书《{chunk['title']}》 {chunk['chars']} 字，"
            f"进度 {chunk['progress']}%",
        )
        return ActivityResult(
            True,
            "ok",
            narration,
            text=narration,
            furniture=furniture,
            effect=effect,
            state=saved,
            source=source,
            bypassed=bypassed,
            next_tick_at=saved.get("next_tick_at"),
            kind="reading",
        )

    # ------------------------------------------------------------ 主流程

    async def tick(
        self,
        robot_id: str = "default",
        source: str = "tick",
        bypass: bool = False,
    ) -> ActivityResult:
        state = self.load_state(robot_id)
        if self._should_socialize(state):
            return await self.run_social(robot_id, source=source, bypass=bypass)

        room = self.store.load_room()
        pickable = models.candidates(room)
        if not pickable:
            return ActivityResult(False, "no_candidate", "房间里还没有绑定行为的家具。", source=source)
        try:
            energy = float(state.get("energy", 80.0))
        except (TypeError, ValueError):
            energy = 80.0

        now = self.now()
        tv_open = bypass or (self.tv_allowed(now) and self.tv_remaining_today(state, now) > 0)
        try:
            tv_boost = float(self._cfg("tv_weight_boost", 2.5))
        except (TypeError, ValueError):
            tv_boost = 2.5

        def allowed(item: dict[str, Any]) -> bool:
            return tv_open or not models.is_tv_furniture(item)

        boosts: dict[str, float] = {}
        if tv_open:
            for item in pickable:
                if models.is_tv_furniture(item):
                    boosts[str(item.get("id"))] = max(0.1, tv_boost)

        try:
            read_boost = float(self._cfg("read_weight_boost", 1.6))
        except (TypeError, ValueError):
            read_boost = 1.6
        if read_boost != 1.0:
            for item in pickable:
                if models.is_reading_furniture(item):
                    key = str(item.get("id"))
                    boosts[key] = boosts.get(key, 1.0) * max(0.1, read_boost)

        weather = await self.current_weather()
        if weather is not None:
            for item in pickable:
                factor = self.weather_boost(item, weather)
                if factor != 1.0:
                    key = str(item.get("id"))
                    boosts[key] = boosts.get(key, 1.0) * factor

        furniture = models.pick_furniture(room, energy, self.rng, boosts=boosts, allowed=allowed)
        if furniture is None:
            return ActivityResult(False, "no_candidate", "这个时间点没有能做的事。", source=source)
        return await self.run_activity(
            robot_id=robot_id,
            furniture_id=str(furniture.get("id")),
            source=source,
            bypass=bypass,
        )

    def _should_socialize(self, state: dict[str, Any]) -> bool:
        """社交指数越高，越可能主动找主人说话。"""
        threshold = self._float_cfg("social_talk_threshold", 70.0)
        try:
            social = float(state.get("social_hunger", 0.0))
        except (TypeError, ValueError):
            social = 0.0
        if social < threshold:
            return False
        probability = min(1.0, (social / 100.0) ** 2)
        return self.rng.random() < probability

    async def run_social(
        self,
        robot_id: str = "default",
        source: str = "tick",
        bypass: bool = False,
    ) -> ActivityResult:
        """不做事，主动找主人搭话；社交指数降下来。"""
        async with self._lock:
            room = self.store.load_room()
            state = self.load_state(robot_id)
            now = self.now()

            bypassed: list[str] = []
            if bypass:
                bypassed = ["活动时段", "静默时段", "进行中锁"]
            else:
                if self.is_busy(state):
                    remain = self.busy_remaining_sec(state)
                    behavior = state.get("behavior") or {}
                    minutes = max(1, round(remain / 60))
                    return ActivityResult(
                        False,
                        "busy",
                        f"正在{behavior.get('name') or '忙'}，还有大约 {minutes} 分钟。",
                        state=state,
                        source=source,
                    )
                if not self._window_ok(now):
                    return ActivityResult(
                        False,
                        "outside_window",
                        "现在不在活动时段里。",
                        state=state,
                        source=source,
                    )

            robot = models.robot_of(room, robot_id)
            robot_name = str(robot.get("name") or "小鲸鱼")
            room_name = str(room.get("room", {}).get("name") or "我的房间")
            behavior = state.get("behavior") or {}

            text = await self._social_text(robot_name, room_name, state, behavior, now)
            if not text:
                text = self.rng.choice(["在忙吗？", "你干嘛呢？", "突然想跟你说句话。"])

            self._apply_body_state(state, now, effect=None)
            cost = max(0.0, self._float_cfg("social_talk_cost", 45.0))
            try:
                social = float(state.get("social_hunger", 0.0))
            except (TypeError, ValueError):
                social = 0.0
            state["social_hunger"] = round(_clamp(social - cost, 0.0, 100.0), 1)
            state["last_text"] = text
            state["last_text_at"] = store_module.isoformat(now)
            state["last_trigger_source"] = source

            recent = state.get("recent_behaviors")
            if not isinstance(recent, list):
                recent = []
            recent.append(
                {
                    "furniture_id": None,
                    "furniture_name": "主人",
                    "effect_id": "social_talk",
                    "effect_name": "聊两句",
                    "at": store_module.isoformat(now),
                }
            )
            state["recent_behaviors"] = recent[-self.recent_limit():]
            state["next_tick_at"] = self._next_tick_after(now)
            saved = self.store.save_state(state)

            self._log("info", f"[virtual_activity] {source} -> 找主人说话: {text}")
            return ActivityResult(
                True,
                "ok",
                text,
                text=text,
                state=saved,
                source=source,
                bypassed=bypassed,
                next_tick_at=saved.get("next_tick_at"),
                kind="social",
            )

    async def _social_text(
        self,
        robot_name: str,
        room_name: str,
        state: dict[str, Any],
        behavior: dict[str, Any],
        now: datetime,
    ) -> str:
        prompt = llm.build_social_prompt(
            robot_name=robot_name,
            room_name=room_name,
            clock_text=now.strftime("%H:%M"),
            state=state,
            behavior=behavior,
            social_threshold=self._float_cfg("social_talk_threshold", 70.0),
        )
        try:
            raw = await self._generate(llm.SYSTEM_PROMPT, prompt)
        except Exception as exc:  # noqa: BLE001
            self._log("warning", f"[virtual_activity] 生成搭话失败: {exc}")
            return ""
        return llm.parse_phrase(raw)

    async def run_activity(
        self,
        robot_id: str,
        furniture_id: str,
        source: str,
        effect_id: str | None = None,
        bypass: bool = False,
    ) -> ActivityResult:
        async with self._lock:
            room = self.store.load_room()
            state = self.load_state(robot_id)
            now = self.now()

            bypassed: list[str] = []
            if bypass:
                bypassed = ["活动时段", "静默时段", "进行中锁"]
            else:
                if self.is_busy(state):
                    remain = self.busy_remaining_sec(state)
                    behavior = state.get("behavior") or {}
                    label = behavior.get("name") or "忙"
                    minutes = max(1, round(remain / 60))
                    return ActivityResult(
                        False,
                        "busy",
                        f"正在{label}，还有大约 {minutes} 分钟。",
                        state=state,
                        source=source,
                    )
                if not self._window_ok(now):
                    return ActivityResult(
                        False,
                        "outside_window",
                        "现在不在活动时段里。",
                        state=state,
                        source=source,
                    )

            furniture = models.find_furniture(room, furniture_id)
            if furniture is None:
                return ActivityResult(
                    False,
                    "not_found",
                    f"房间里没有叫「{furniture_id}」的家具。",
                    state=state,
                    source=source,
                )
            if not furniture.get("enabled", True):
                return ActivityResult(
                    False,
                    "disabled",
                    f"「{furniture.get('name')}」现在被收起来了。",
                    state=state,
                    furniture=furniture,
                    source=source,
                )

            effects = furniture.get("effects") if isinstance(furniture.get("effects"), list) else []
            if not effects:
                return ActivityResult(
                    False,
                    "no_effect",
                    f"「{furniture.get('name')}」只是个摆件，没什么可做的。",
                    state=state,
                    furniture=furniture,
                    source=source,
                )

            if models.is_tv_furniture(furniture) and not bypass:
                if not self.tv_allowed(now):
                    return ActivityResult(
                        False,
                        "tv_closed",
                        f"电视只在谷时才能看（{self.tv_hours_text(now)}）。",
                        state=state,
                        furniture=furniture,
                        source=source,
                    )
                if self.tv_remaining_today(state, now) <= 0:
                    return ActivityResult(
                        False,
                        "tv_limit",
                        "今天电视已经看够了。",
                        state=state,
                        furniture=furniture,
                        source=source,
                    )

            robot = models.robot_of(room, robot_id)
            robot_name = str(robot.get("name") or "小鲸鱼")
            room_name = str(room.get("room", {}).get("name") or "我的房间")

            effect: dict[str, Any] | None = None
            text = ""
            weather = await self.current_weather()
            weather_text = weather.summary() if weather is not None else ""
            if effect_id:
                effect = models.find_effect(furniture, effect_id)
                if effect is None:
                    return ActivityResult(
                        False,
                        "effect_not_found",
                        f"「{furniture.get('name')}」上没有「{effect_id}」这个活动。",
                        state=state,
                        furniture=furniture,
                        source=source,
                    )

            policy = self.policy()
            speak = self.should_speak(source)
            if effect is None and policy == "llm" and len(effects) > 1:
                chosen, decided_text = await self._decide(
                    robot_name, room_name, furniture, effects, state, now, weather_text=weather_text
                )
                if chosen is not None:
                    effect = chosen
                    text = decided_text if speak else ""
                else:
                    # 模型没按格式给出效果，丢弃它的自由文本，走兜底链路
                    text = ""
            if effect is None:
                fallback_policy = "first" if policy == "first" else "random"
                effect = models.pick_effect(furniture, fallback_policy, self.rng, energy=state.get("energy"))
            if effect is None:
                return ActivityResult(
                    False,
                    "no_effect",
                    f"「{furniture.get('name')}」只是个摆件，没什么可做的。",
                    state=state,
                    furniture=furniture,
                    source=source,
                )

            if str(effect.get("action") or "") == "tv":
                return await self._run_tv(room, state, furniture, effect, source, bypass, now, bypassed, speak)

            if str(effect.get("action") or "") == "read_book":
                return await self._run_reading(room, state, furniture, effect, source, now, bypassed, speak)

            if speak and not text:
                text = await self._phrase(robot_name, room_name, furniture, effect, state, now, weather_text=weather_text)
            if speak and not text:
                text = llm.fallback_text(str(furniture.get("name", "家具")), str(effect.get("name", "活动")))

            duration = int(effect.get("duration_sec") or self.default_duration())
            duration = max(30, duration)
            ends_at = now + timedelta(seconds=duration)
            center_x, center_y = models.furniture_center(furniture)

            state["behavior"] = {
                "furniture_id": furniture.get("id"),
                "furniture_name": furniture.get("name"),
                "effect_id": effect.get("id"),
                "name": effect.get("name"),
            }
            state["location"] = {
                "furniture_id": furniture.get("id"),
                "x": round(center_x, 1),
                "y": round(center_y, 1),
            }
            state["position"] = dict(state["location"])
            state["started_at"] = store_module.isoformat(now)
            state["ends_at"] = store_module.isoformat(ends_at)
            if text:
                state["last_text"] = text
                state["last_text_at"] = store_module.isoformat(now)
            state["last_trigger_source"] = source
            state["last_error"] = None
            self._apply_body_state(state, now, effect)
            self._append_recent(state, furniture, effect, now)

            base = ends_at if ends_at > now else now
            state["next_tick_at"] = self._next_tick_after(base)
            saved = self.store.save_state(state)

            self._log(
                "info",
                f"[virtual_activity] {source} -> {furniture.get('name')}/{effect.get('name')} "
                f"({duration}s): {text}",
            )

            return ActivityResult(
                True,
                "ok",
                text,
                text=text,
                furniture=furniture,
                effect=effect,
                state=saved,
                source=source,
                bypassed=bypassed,
                next_tick_at=saved.get("next_tick_at"),
            )

    # ------------------------------------------------------------ 查询

    def status(self, robot_id: str = "default") -> dict[str, Any]:
        room = self.store.load_room()
        state = self.load_state(robot_id)
        now = self.now()
        next_at = store_module.parse_iso(state.get("next_tick_at"))
        return {
            "now": store_module.isoformat(now),
            "room": models.room_summary(room),
            "state": state,
            "busy": self.is_busy(state),
            "busy_remaining_sec": self.busy_remaining_sec(state),
            "next_tick_in_sec": int((next_at - now).total_seconds()) if next_at else None,
            "auto_tick": self.auto_tick_enabled(),
            "windows": {
                "active": self._cfg("active_hours", "08:00-20:00"),
                "quiet": self._cfg("quiet_hours", "23:00-08:00"),
            },
        }


__all__ = ["ActivityResult", "VirtualRoomEngine", "TextGenerator", "Awaitable"]

