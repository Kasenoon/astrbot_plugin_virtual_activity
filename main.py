"""虚拟活动：房间家具 = 互动按钮，随机 tick + 手动点击共用一条判定管线。"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

try:  # 4.5.7+ 才有临时内容块，老版本降级成普通 dict
    from astrbot.core.agent.message import TextPart
except Exception:  # noqa: BLE001
    TextPart = None  # type: ignore[assignment]

from .core import models, prompt as prompt_module, store as store_module
from .core.books import BookShelf
from .core.engine import ActivityResult, VirtualRoomEngine
from .core.scheduler import TickScheduler
from .core.store import RoomStore
from .core.weather import WeatherService

PLUGIN_NAME = "astrbot_plugin_virtual_activity"
ROBOT_ID = "default"


def _fmt_num(value: Any) -> str:
    """整数就显示整数，带小数就保留一位（社交指数支持小数）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "?"
    return f"{number:.0f}" if number.is_integer() else f"{number:.1f}"


HELP_TEXT = (
    "虚拟活动\n"
    "· /回家  看看现在在干嘛\n"
    "· /点 沙发 [看书]  手动让它去某件家具（受时段和谷时限制）\n"
    "· /测试点 电视  同上，但绕过时段 / 静默 / 进行中锁 / 谷时与配额\n"
    "· /活动列表  家具与可做的事\n"
    "· /书里 关键词  查正在读的小说（只查已读部分，不剧透）\n"
    "· /测试tick  强制触发一次随机活动（同样绕过限制）\n"
    "· /天气  看一眼当前的窗外天气（需要配置和风 key）\n"
    "· /房间  在 WebUI 里打开房间视图"
)


class VirtualActivityPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config
        self.store = RoomStore(Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME)
        self.store.ensure()
        self.weather = WeatherService(self._cfg, logger=self.logger)
        self.shelf = BookShelf(self.store.data_dir)
        self.engine = VirtualRoomEngine(
            store=self.store,
            config=self._cfg,
            generate=self._generate,
            logger=self.logger,
            weather_source=self.weather.get,
            books=self.shelf,
        )
        self.scheduler = TickScheduler(
            engine=self.engine,
            logger=self.logger,
            interval=15.0,
            robot_id=ROBOT_ID,
            on_result=self._on_tick_result,
        )
        self._register_web_apis()
        self._start_scheduler()

    # ------------------------------------------------------------ 配置

    def _cfg(self, key: str, default: Any = None) -> Any:
        config = self.config
        if config is None:
            return default
        try:
            value = config.get(key, default)
        except AttributeError:
            value = getattr(config, key, default)
        return default if value is None else value

    def _admin_qq(self) -> str:
        return str(self._cfg("admin_qq", "") or "").strip()

    def _int_cfg(self, key: str, default: int) -> int:
        try:
            return int(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        admin_qq = self._admin_qq()
        sender = str(event.get_sender_id() or "").strip()
        if admin_qq and sender == admin_qq:
            return True
        try:
            return bool(event.is_admin())
        except Exception:  # noqa: BLE001 - 某些平台不支持管理员判定
            return False

    # ------------------------------------------------------------ 生命周期

    def _start_scheduler(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        if not self.scheduler.running:
            self.scheduler.start()
        asyncio.create_task(self._warm_weather())

    async def _warm_weather(self) -> None:
        try:
            await self.weather.get()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"[virtual_activity] 预热天气失败: {exc}")

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        self._start_scheduler()

    async def terminate(self) -> None:
        await self.scheduler.stop()

    # ------------------------------------------------------------ 注入自身状态

    def _inject_enabled(self, event: AstrMessageEvent) -> bool:
        value = self._cfg("inject_prompt", True)
        if isinstance(value, str):
            enabled = value.strip().lower() in {"1", "true", "yes", "on"}
        else:
            enabled = bool(value)
        if not enabled:
            return False
        scope = str(self._cfg("inject_scope", "all") or "all").strip().lower()
        if scope == "admin" and not self._is_admin(event):
            return False
        mode = str(self._cfg("inject_mode", "smart") or "smart").strip().lower()
        if mode == "always":
            return True
        # smart：只有聊到相关话题时才注入，省 token 也不啰嗦
        try:
            text = event.get_message_str()
        except Exception:  # noqa: BLE001
            text = ""
        extra = list(self._furniture_names())
        configured = self._cfg("inject_keywords", []) or []
        if isinstance(configured, str):
            configured = [item.strip() for item in configured.replace("，", ",").split(",") if item.strip()]
        extra.extend(str(item) for item in configured)
        return prompt_module.should_inject(text, extra)

    def _furniture_names(self) -> list[str]:
        """家具名一分钟缓存一次，避免每条消息都读 room.json。"""
        now = time.monotonic()
        cached_at, cached = getattr(self, "_furniture_cache", (0.0, []))
        if now - cached_at < 60 and cached:
            return cached
        try:
            names = [str(item.get("name") or "") for item in models.furniture_list(self.store.load_room())]
        except Exception:  # noqa: BLE001
            names = []
        self._furniture_cache = (now, names)
        return names

    def _state_block(self) -> str:
        status = self.engine.status(ROBOT_ID)
        state = status.get("state") or {}
        room = status.get("room") or {}
        behavior = state.get("behavior") or {}
        busy = bool(status.get("busy"))
        remaining = int(status.get("busy_remaining_sec") or 0)

        now = store_module.parse_iso(status.get("now"))
        now_text = now.strftime("%H:%M") if now else ""

        location = state.get("location") or {}
        position = None
        if location:
            name = behavior.get("furniture_name") or location.get("furniture_id") or "屋里的某处"
            try:
                x = float(location.get("x"))
                y = float(location.get("y"))
                position = f"{name}（房间坐标 {x:.0f}, {y:.0f}）"
            except (TypeError, ValueError):
                position = str(name)

        recent: list[str] = []
        for item in state.get("recent_behaviors") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("furniture_name") or item.get("furniture_id") or "?"
            effect = item.get("effect_name") or item.get("effect_id") or "?"
            recent.append(f"{name}·{effect}")

        last_at = store_module.parse_iso(state.get("last_text_at"))
        return prompt_module.build_state_block(
            room_name=str(room.get("name") or "我的房间"),
            now_text=now_text,
            furniture_name=str(behavior.get("furniture_name")) if busy and behavior else None,
            effect_name=str(behavior.get("name")) if busy and behavior else None,
            remaining_minutes=max(1, (remaining + 59) // 60) if busy and remaining else None,
            position=position,
            energy=state.get("energy"),
            social=state.get("social_hunger"),
            recent=recent,
            last_text=state.get("last_text"),
            last_text_at=last_at.strftime("%H:%M") if last_at else None,
            social_threshold=self._int_cfg("social_talk_threshold", 70),
            weather=self._weather_text(),
            reading=self._reading_text(state),
        )

    def _reading_text(self, state: dict[str, Any]) -> str | None:
        title = state.get("book_title")
        if not title:
            return None
        excerpt = str(state.get("book_excerpt") or "").strip()
        progress = state.get("read_progress")
        try:
            progress_text = f"{float(progress):.0f}%"
        except (TypeError, ValueError):
            progress_text = "读到一半"
        text = f"《{title}》 {progress_text}"
        if excerpt and bool(self._cfg("read_excerpt_in_prompt", True)):
            text += f"（书里刚写到「{excerpt}」——那是小说情节，不是现实里发生的事）"
        return text

    def _weather_text(self) -> str | None:
        if not bool(self._cfg("weather_inject", True)):
            return None
        weather = self.weather.last()
        return weather.summary() if weather is not None else None

    @filter.on_llm_request()
    async def inject_state(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """让机器人在正常对话里知道自己此刻在哪、在做什么。

        状态块是「每轮都会变」的内容，所以不往 system prompt 里塞：
        作为临时内容块追加到用户消息末尾（AstrBot 自己的时间/昵称提醒也是这么做的），
        这样 system prompt 保持完全稳定，前缀缓存才不会被破坏。
        """
        try:
            if not self._inject_enabled(event):
                return
            block = self._state_block()
            if not block:
                return

            parts = getattr(req, "extra_user_content_parts", None)
            if parts is None:
                parts = []
                req.extra_user_content_parts = parts
            if self._has_state_part(parts):
                return
            parts.append(self._make_temp_part(block))
        except Exception as exc:  # noqa: BLE001 - 注入失败不能影响正常对话
            self.logger.warning(f"[virtual_activity] 注入自身状态失败: {exc}")

    @staticmethod
    def _has_state_part(parts: list[Any]) -> bool:
        for part in parts:
            text = getattr(part, "text", None)
            if text is None and isinstance(part, dict):
                text = part.get("text")
            if prompt_module.MARKER in str(text or ""):
                return True
        return False

    @staticmethod
    def _make_temp_part(text: str) -> Any:
        if TextPart is None:
            return {"type": "text", "text": text}
        part = TextPart(text=text)
        mark = getattr(part, "mark_as_temp", None)
        return mark() if callable(mark) else part

    # ------------------------------------------------------------ LLM

    async def _generate(self, system_prompt: str, prompt: str) -> str:
        provider = None
        provider_id = str(self._cfg("llm_provider_id", "") or "").strip()
        try:
            if provider_id:
                provider = self.context.get_provider_by_id(provider_id)
            if provider is None:
                provider = await self.context.get_using_provider_async()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"[virtual_activity] 获取对话模型失败: {exc}")
            return ""
        if provider is None:
            self.logger.warning("[virtual_activity] 没有可用的对话模型，使用兜底文案")
            return ""

        try:
            timeout = max(5, int(self._cfg("llm_timeout_sec", 20)))
        except (TypeError, ValueError):
            timeout = 20
        try:
            response = await asyncio.wait_for(
                provider.text_chat(system_prompt=system_prompt, prompt=prompt),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            self.logger.warning("[virtual_activity] 生成文案超时")
            return ""
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"[virtual_activity] 生成文案失败: {exc}")
            return ""
        return str(getattr(response, "completion_text", "") or "").strip()

    # ------------------------------------------------------------ 发送

    def _platform_ids(self) -> list[str]:
        ids: list[str] = []
        try:
            instances = list(self.context.platform_manager.platform_insts)
        except Exception:  # noqa: BLE001
            return ids
        for instance in instances:
            try:
                meta = instance.meta()
            except Exception:  # noqa: BLE001
                continue
            if meta is not None and getattr(meta, "id", None):
                ids.append(meta.id)
        return ids

    def _resolve_target_umo(self) -> str:
        configured = str(self._cfg("admin_umo", "") or "").strip()
        if configured.count(":") >= 2:
            return configured
        saved = str(self.store.load_runtime().get("admin_umo") or "").strip()
        if saved.count(":") >= 2:
            return saved
        admin_qq = self._admin_qq()
        if not admin_qq:
            return ""
        preferred = str(self._cfg("platform_id", "") or "").strip()
        platform_ids = self._platform_ids()
        platform_id = ""
        if preferred and preferred in platform_ids:
            platform_id = preferred
        elif platform_ids:
            platform_id = platform_ids[0]
        if not platform_id:
            return ""
        return str(MessageSession(platform_id, MessageType.FRIEND_MESSAGE, admin_qq))

    async def _send(self, text: str) -> bool:
        text = str(text or "").strip()
        if not text:
            return False
        umo = self._resolve_target_umo()
        if not umo:
            self.logger.warning("[virtual_activity] 找不到发送目标会话，跳过")
            return False
        try:
            await self.context.send_message(umo, MessageChain().message(text))
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"[virtual_activity] 发送失败({umo}): {exc}")
            return False
        await self._record_proactive(umo, text)
        return True

    async def _record_proactive(self, umo: str, text: str) -> None:
        """把主动消息写进对话历史，否则下一轮模型不知道自己刚说过什么。"""
        if not bool(self._cfg("record_proactive_history", True)):
            return
        try:
            manager = self.context.conversation_manager
            cid = await manager.get_curr_conversation_id(umo)
            if not cid:
                cid = await manager.new_conversation(umo, title="虚拟活动")
            await manager.add_message_pair(
                cid,
                {
                    "role": "user",
                    "content": (
                        "[系统记录] 你刚刚主动给用户发了下面这条消息，用户还没回复。"
                        "如果内容来自读书或看视频，那只是虚构作品里的情节，"
                        "里面的人物不是现实里认识的人。"
                    ),
                },
                {"role": "assistant", "content": text},
            )
        except Exception as exc:  # noqa: BLE001 - 写历史失败不影响发送
            self.logger.warning(f"[virtual_activity] 写入会话历史失败: {exc}")

    async def _on_tick_result(self, result: ActivityResult) -> None:
        if not result.ok or not result.text:
            return
        if not await self._send(result.text):
            self.engine.record_error(ROBOT_ID, "活动消息没发出去：检查平台连接或 admin_umo")

    # ------------------------------------------------------------ 指令

    @filter.command("回家", alias={"home"})
    async def cmd_home(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能看这个。")
            return
        self.store.remember_admin_umo(event.unified_msg_origin)
        yield event.plain_result(self._format_status())

    @filter.command("点", alias={"dian"})
    async def cmd_point(self, event: AstrMessageEvent, target: str = "", effect: str = ""):
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能让它乱跑。")
            return
        self.store.remember_admin_umo(event.unified_msg_origin)
        target = str(target or "").strip()
        if not target:
            yield event.plain_result("要点哪件家具？\n" + self._format_list())
            return
        result = await self.engine.run_activity(
            robot_id=ROBOT_ID,
            furniture_id=target,
            source="click",
            effect_id=str(effect or "").strip() or None,
        )
        yield event.plain_result(result.text if result.ok else result.message)

    @filter.command("测试点", alias={"testpoint"})
    async def cmd_test_point(self, event: AstrMessageEvent, target: str = "", effect: str = ""):
        """和 /点 一样，但绕过所有限制，方便测试。"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能测试。")
            return
        self.store.remember_admin_umo(event.unified_msg_origin)
        target = str(target or "").strip()
        if not target:
            yield event.plain_result("要点哪件家具？\n" + self._format_list())
            return
        result = await self.engine.run_activity(
            robot_id=ROBOT_ID,
            furniture_id=target,
            source="test",
            effect_id=str(effect or "").strip() or None,
            bypass=True,
        )
        if result.ok:
            yield event.plain_result(
                result.text + "\n（测试模式：已绕过时段 / 静默 / 进行中锁 / 谷时与配额）"
            )
        else:
            yield event.plain_result(f"没触发成功：{result.reason}\n{result.message}")

    @filter.command("测试tick", alias={"testtick"})
    async def cmd_test_tick(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能测试。")
            return
        self.store.remember_admin_umo(event.unified_msg_origin)
        result = await self.engine.tick(robot_id=ROBOT_ID, source="test", bypass=True)
        if not result.ok:
            yield event.plain_result(f"没触发成功：{result.reason}\n{result.message}")
            return
        state = result.state or {}
        if result.kind == "social":
            lines = [
                "测试活动：主动找主人说话",
                f"文案：{result.text}",
                f"社交指数 → {_fmt_num(state.get('social_hunger'))}",
                f"精力 {state.get('energy')}",
                f"下次 tick：{result.next_tick_at or '未排'}",
            ]
            if result.bypassed:
                lines.append("已绕过：" + "、".join(result.bypassed))
            lines.append("（测试模式，未发送到目标会话）")
            yield event.plain_result("\n".join(lines))
            return
        furniture = result.furniture or {}
        eff = result.effect or {}
        lines = [
            f"测试活动：{furniture.get('name')} / {eff.get('name')}",
            f"文案：{result.text}",
            f"时长：{eff.get('duration_sec')} 秒",
            f"位置：{state.get('location')}",
            f"精力 {_fmt_num(state.get('energy'))}（该效果 {eff.get('energy_delta')}）· 社交 {_fmt_num(state.get('social_hunger'))}",
            f"下次 tick：{result.next_tick_at or '未排'}",
        ]
        if result.bypassed:
            lines.append("已绕过：" + "、".join(result.bypassed))
        lines.append("（测试模式，未发送到目标会话）")
        yield event.plain_result("\n".join(lines))

    @filter.command("活动列表", alias={"活动list"})
    async def cmd_list(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能看这个。")
            return
        self.store.remember_admin_umo(event.unified_msg_origin)
        yield event.plain_result(self._format_list())

    @filter.command("房间", alias={"room"})
    async def cmd_room(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能进房间。")
            return
        self.store.remember_admin_umo(event.unified_msg_origin)
        room = self.store.load_room()
        summary = models.room_summary(room)
        yield event.plain_result(
            f"「{summary['name']}」· {summary['furniture_count']} 件家具 "
            f"（{summary['interactive_count']} 件可互动）\n"
            f"画布 {summary['canvas']['width']}×{summary['canvas']['height']}\n"
            "WebUI → 插件 → 虚拟活动 → 打开「房间」视图，可以拖动摆放并点击家具。"
        )

    @filter.command("天气", alias={"tianqi"})
    async def cmd_weather(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能看天气。")
            return
        weather = await self.weather.get(force=True)
        if weather is None:
            yield event.plain_result(
                "拿不到天气：检查 weather_key / weather_api_host（或关掉 weather_enabled）"
            )
            return
        yield event.plain_result(f"{weather.city} {weather.summary()}\n观测时间：{weather.obs_time or '未知'}")

    @filter.command("书里", alias={"shuli"})
    async def cmd_book_lookup(self, event: AstrMessageEvent, keyword: str = ""):
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能查书里的内容。")
            return
        keyword = str(keyword or "").strip()
        if not keyword:
            yield event.plain_result("用法：/书里 关键词（人名、地名、事件都行）")
            return
        yield event.plain_result(self._format_lookup(self._lookup_book(keyword)))

    @filter.llm_tool(name="book_lookup")
    async def book_lookup(self, event: AstrMessageEvent, query: str) -> str:
        """查你正在读的这本小说里、已经读过的部分有没有相关内容。

        只在用户明确问起这本书的人物、情节、设定时才调用；日常闲聊、其它话题不要调用。

        Args:
            query(string): 要查的关键词，比如人名、地名或事件
        """
        return self._format_lookup(self._lookup_book(query))

    def _lookup_book(self, keyword: str) -> dict[str, Any] | None:
        try:
            limit = max(1, min(5, self._int_cfg("book_lookup_limit", 2)))
            chars = max(80, min(600, self._int_cfg("book_lookup_chars", 240)))
        except (TypeError, ValueError):
            limit, chars = 2, 240
        try:
            return self.shelf.search(keyword, limit=limit, excerpt_chars=chars)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"[virtual_activity] 查书失败: {exc}")
            return None

    @staticmethod
    def _format_lookup(payload: dict[str, Any] | None) -> str:
        if not payload:
            return "书架还是空的，先导入一本小说吧。"
        title = payload.get("title") or "这本书"
        progress = float(payload.get("progress") or 0)
        hits = payload.get("hits") or []
        if payload.get("unread_hit"):
            return f"《{title}》里应该有提到，但我还没读到那儿（现在读到 {progress:.0f}%）。"
        if not hits:
            return f"《{title}》已经读过的部分（{progress:.0f}%）里没找到相关的内容。"
        lines = [f"《{title}》（已读到 {progress:.0f}%）："]
        for index, hit in enumerate(hits, start=1):
            lines.append(f"{index}. …{hit}…")
        return "\n".join(lines)

    @filter.command("虚拟活动", alias={"va"})
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(HELP_TEXT)

    # ------------------------------------------------------------ 文本

    def _format_status(self) -> str:
        status = self.engine.status(ROBOT_ID)
        state = status["state"]
        behavior = state.get("behavior")
        lines: list[str] = []

        if status["busy"] and behavior:
            remain = status["busy_remaining_sec"]
            minutes = max(1, (remain + 59) // 60)
            lines.append(f"现在在「{behavior.get('furniture_name')}」{behavior.get('name')}，还有约 {minutes} 分钟。")
        else:
            lines.append("现在闲着。")

        if state.get("last_text"):
            lines.append(f"上次说过：{state.get('last_text')}")

        if not status["auto_tick"]:
            lines.append("自动活动：已关闭")
        else:
            next_in = status.get("next_tick_in_sec")
            if next_in is None:
                lines.append("自动活动：还没排时间")
            elif next_in <= 0:
                lines.append("自动活动：马上就该动一动了")
            else:
                lines.append(f"自动活动：约 {max(1, next_in // 60)} 分钟后")

        windows = status.get("windows") or {}
        lines.append(f"活动时段 {windows.get('active')} · 静默 {windows.get('quiet')}")
        body = f"精力 {_fmt_num(state.get('energy'))} · 社交 {_fmt_num(state.get('social_hunger'))}"
        try:
            if float(state.get("energy")) < 30:
                body += "（有点累）"
        except (TypeError, ValueError):
            pass
        try:
            if float(state.get("social_hunger")) >= self._int_cfg("social_talk_threshold", 70):
                body += "（想找你说话）"
        except (TypeError, ValueError):
            pass
        lines.append(body)

        tv_line = self._tv_status_line()
        if tv_line:
            lines.append(tv_line)

        recent = state.get("recent_behaviors") or []
        if recent:
            tail = recent[-3:]
            parts = [f"{item.get('furniture_name')}/{item.get('effect_name')}" for item in tail if isinstance(item, dict)]
            if parts:
                lines.append("最近：" + " → ".join(parts))
        if state.get("last_error"):
            lines.append(f"上次出错：{state.get('last_error')}")
        return "\n".join(lines)

    def _tv_status_line(self) -> str:
        room = self.store.load_room()
        if not any(models.is_tv_furniture(item) for item in models.furniture_list(room)):
            return ""
        state = self.engine.load_state(ROBOT_ID)
        now = self.engine.now()
        limit = self._int_cfg("tv_daily_limit", 3)
        used = self.engine.tv_used_today(state, now)
        remaining = self.engine.tv_remaining_today(state, now)
        if now.weekday() >= 5:
            hours = str(self._cfg("tv_hours_weekend", "00:00-24:00") or "")
        else:
            hours = str(self._cfg("tv_hours_weekday", "12:00-14:00,18:00-22:00") or "")
        open_now = self.engine.tv_allowed(now) and remaining > 0
        return f"电视：今天 {used}/{limit} 个，{'现在能看' if open_now else '现在不看'}（谷时 {hours}）"

    def _format_list(self) -> str:
        room = self.store.load_room()
        items = models.furniture_list(room)
        if not items:
            return "房间里还没有家具。"
        lines = []
        for index, furniture in enumerate(items, start=1):
            effects = furniture.get("effects") or []
            name = furniture.get("name")
            if effects:
                names = " / ".join(str(effect.get("name")) for effect in effects)
                lines.append(f"{index}. {name}：{names}")
            else:
                lines.append(f"{index}. {name}：摆件")
        return "\n".join(lines)

    # ------------------------------------------------------------ Web API

    def _register_web_apis(self) -> None:
        base = f"/{PLUGIN_NAME}"
        self.context.register_web_api(f"{base}/room", self.api_room, ["GET"], "获取房间与状态")
        self.context.register_web_api(f"{base}/room/save", self.api_save_room, ["POST"], "保存房间布局")
        self.context.register_web_api(f"{base}/trigger", self.api_trigger, ["POST"], "点击家具触发活动")
        self.context.register_web_api(f"{base}/tick", self.api_tick, ["POST"], "强制一次随机 tick")
        self.context.register_web_api(f"{base}/state", self.api_state, ["GET"], "获取机器人状态")
        self.context.register_web_api(f"{base}/books", self.api_books, ["GET"], "书架列表")
        self.context.register_web_api(f"{base}/books/upload", self.api_books_upload, ["POST"], "导入小说")
        self.context.register_web_api(f"{base}/books/select", self.api_books_select, ["POST"], "选择当前在读")
        self.context.register_web_api(f"{base}/books/delete", self.api_books_delete, ["POST"], "删除小说")

    async def api_room(self) -> Any:
        room = self.store.load_room()
        robots = room.get("room", {}).get("robots") if isinstance(room.get("room"), dict) else None
        if isinstance(robots, list) and robots:
            if not str(robots[0].get("avatar") or "").strip():
                robots[0]["avatar"] = str(self._cfg("robot_avatar", "🐋") or "🐋").strip()
        return json_response(
            {
                "ok": True,
                "room": room,
                "status": self.engine.status(ROBOT_ID),
            }
        )

    async def api_state(self) -> Any:
        return json_response({"ok": True, "status": self.engine.status(ROBOT_ID)})

    async def api_save_room(self) -> Any:
        payload = await request.json(default={})
        room = payload.get("room") if isinstance(payload, dict) else None
        try:
            saved = self.store.save_room(room if room is not None else payload)
        except (ValueError, TypeError) as exc:
            return error_response(f"房间数据不合法: {exc}", status_code=400)
        return json_response({"ok": True, "room": saved})

    async def api_trigger(self) -> Any:
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        furniture_id = str(payload.get("furniture_id") or payload.get("id") or "").strip()
        if not furniture_id:
            return error_response("缺少 furniture_id", status_code=400)
        effect_id = payload.get("effect_id")
        result = await self.engine.run_activity(
            robot_id=ROBOT_ID,
            furniture_id=furniture_id,
            source="click",
            effect_id=str(effect_id) if effect_id else None,
        )
        return json_response(result.as_dict())

    async def api_tick(self) -> Any:
        result = await self.engine.tick(robot_id=ROBOT_ID, source="test", bypass=True)
        return json_response(result.as_dict())

    # ------------------------------------------------------------ 书架 API

    async def api_books(self) -> Any:
        return json_response({"ok": True, "books": self.shelf.list(), "active": self.shelf.active})

    async def api_books_upload(self) -> Any:
        files = await request.files()
        upload = files.get("file")
        if upload is None:
            return error_response("没有收到文件", status_code=400)
        try:
            raw = await upload.read()
            book = self.shelf.add_text(upload.filename or "未命名.txt", raw)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:  # noqa: BLE001
            return error_response(f"导入失败：{exc}", status_code=400)
        return json_response(
            {"ok": True, "book": book, "books": self.shelf.list(), "active": self.shelf.active}
        )

    async def api_books_select(self) -> Any:
        payload = await request.json(default={})
        book_id = str(payload.get("id") or "").strip()
        if not self.shelf.select(book_id):
            return error_response("没有这本书", status_code=404)
        return json_response({"ok": True, "books": self.shelf.list(), "active": self.shelf.active})

    async def api_books_delete(self) -> Any:
        payload = await request.json(default={})
        book_id = str(payload.get("id") or "").strip()
        if not self.shelf.delete(book_id):
            return error_response("没有这本书", status_code=404)
        return json_response({"ok": True, "books": self.shelf.list(), "active": self.shelf.active})
