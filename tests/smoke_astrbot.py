"""在真实 AstrBot v4.29.1 环境里做一次导入 / 注册冒烟测试。

运行方式（工作目录必须是 AstrBot 的 core 目录，才能 import astrbot）：

    python -m astrbot_plugin_virtual_activity.tests.smoke_astrbot
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

PLUGIN_PARENT = str(Path(__file__).resolve().parents[2])
if PLUGIN_PARENT not in sys.path:
    sys.path.insert(0, PLUGIN_PARENT)


async def main() -> int:
    failures: list[str] = []

    def check(condition: bool, label: str) -> None:
        print(("  ok  " if condition else " FAIL ") + label)
        if not condition:
            failures.append(label)

    import astrbot_plugin_virtual_activity.main as plugin_main
    from astrbot.core.star.star_handler import star_handlers_registry

    tmp = tempfile.TemporaryDirectory()
    plugin_main.get_astrbot_plugin_data_path = lambda: tmp.name

    registered: list[tuple] = []

    class FakePlatform:
        def __init__(self, platform_id: str, name: str) -> None:
            self._meta = SimpleNamespace(id=platform_id, name=name)

        def meta(self):
            return self._meta

    class FakeConversationManager:
        def __init__(self) -> None:
            self.pairs: list[tuple] = []
            self.created: list[str] = []

        async def get_curr_conversation_id(self, umo):
            return "cid-test"

        async def new_conversation(self, umo, **kwargs):
            self.created.append(umo)
            return "cid-new"

        async def add_message_pair(self, cid, user_message, assistant_message):
            self.pairs.append((cid, user_message, assistant_message))

    class FakeContext:
        def __init__(self) -> None:
            self.platform_manager = SimpleNamespace(
                platform_insts=[FakePlatform("T800", "aiocqhttp")],
            )
            self.sent: list[tuple[str, object]] = []
            self.conversation_manager = FakeConversationManager()

        def register_web_api(self, route, handler, methods, desc):
            registered.append((route, handler, tuple(methods), desc))

        async def send_message(self, session, chain):
            self.sent.append((str(session), chain))
            return True

        async def get_using_provider_async(self, umo=None):
            return None

        def get_provider_by_id(self, provider_id):
            return None

    context = FakeContext()
    plugin = plugin_main.VirtualActivityPlugin(context, {"admin_qq": "3145821247"})

    check(plugin.store.room_path.exists(), "首次启动写入 room.json")

    routes = {route for route, *_ in registered}
    for expected in ("room", "room/save", "trigger", "tick", "state"):
        check(f"/astrbot_plugin_virtual_activity/{expected}" in routes, f"注册 Web API {expected}")

    check(
        plugin._resolve_target_umo() == "T800:FriendMessage:3145821247",
        "默认发送目标解析为 T800:FriendMessage:3145821247",
    )

    # 指令 handler 是否被框架收集
    names = {md.handler_name for md in star_handlers_registry}
    for handler_name in ("cmd_home", "cmd_point", "cmd_test_point", "cmd_test_tick", "cmd_list", "cmd_room", "cmd_weather", "cmd_help", "cmd_book_lookup", "book_lookup", "on_owner_talk", "inject_state"):
        check(handler_name in names, f"注册 handler {handler_name}")

    status = plugin.engine.status("default")
    check(status["state"]["next_tick_at"] is None or isinstance(status["state"]["next_tick_at"], str), "状态结构可读")

    reading_text = plugin._reading_text(
        {"book_title": "某小说", "book_excerpt": "张三说：我们走吧。", "read_progress": 12.0}
    )
    check(
        reading_text is not None and "小说情节" in reading_text and "不是现实" in reading_text,
        "读书摘录在注入时被标注为小说情节",
    )

    # 电视走假数据，避免依赖网络和随机
    async def fake_videos(rids, cookie, timeout):
        return [
            {
                "bvid": "BV1smoke001",
                "title": "冒烟测试用视频",
                "owner": "测试UP",
                "views": 50000,
                "duration": 300,
                "tname": "测试",
                "tid": 4,
                "desc": "假的",
                "url": "https://www.bilibili.com/video/BV1smoke001",
            }
        ]

    plugin.engine._video_source = fake_videos
    plugin.shelf.add_text("冒烟小说.txt", "第一章开始了。" * 200)
    plugin.shelf.read_next(chars=300, loop=False)

    lookup = plugin._format_lookup(plugin._lookup_book("第一章"))
    check("冒烟小说" in lookup or "没找到" in lookup, "书内检索可用")

    response = await plugin.api_room()
    check(response is not None, "api_room 返回响应")

    result = await plugin.engine.tick(source="test", bypass=True)
    check(result.ok, f"强制 tick 可触发（{result.reason}）")
    if result.ok:
        print(f"       文案：{result.text}")
        print(f"       家具：{result.furniture['name']} / {result.effect['name']}")

    await plugin._on_tick_result(result)
    check(bool(context.sent), "tick 结果能通过 send_message 发到目标会话")
    check(
        bool(context.conversation_manager.pairs)
        and context.conversation_manager.pairs[-1][2]["content"] == result.text,
        "主动消息写入了对话历史",
    )

    # ---- 自身状态注入 ----
    from astrbot_plugin_virtual_activity.core import prompt as prompt_module

    class FakeEvent:
        def __init__(self, sender: str = "3145821247") -> None:
            self._sender = sender
            self.unified_msg_origin = "T800:FriendMessage:3145821247"

        def get_sender_id(self) -> str:
            return self._sender

        def get_message_str(self) -> str:
            return "你在干嘛？"

        def is_admin(self) -> bool:
            return self._sender == "3145821247"

    class FakeRequest:
        def __init__(self) -> None:
            self.system_prompt = "你是小鲸鱼。"
            self.prompt = "你在干嘛？"
            self.extra_user_content_parts: list = []

    # ---- 主人说话 → 社交指数重置 ----
    state = plugin.store.load_state("default")
    state["social_hunger"] = 90.0
    plugin.store.save_state(state)
    await plugin.on_owner_talk(FakeEvent())
    check(float(plugin.store.load_state("default")["social_hunger"]) == 0.0, "管理员说话后社交指数重置")

    state = plugin.store.load_state("default")
    state["social_hunger"] = 90.0
    plugin.store.save_state(state)
    await plugin.on_owner_talk(FakeEvent(sender="10086"))
    check(float(plugin.store.load_state("default")["social_hunger"]) == 90.0, "非管理员说话不重置")

    def state_parts(request):
        return [
            part
            for part in request.extra_user_content_parts
            if prompt_module.MARKER in str(getattr(part, "text", "") or "")
        ]

    req = FakeRequest()
    await plugin.inject_state(FakeEvent(), req)
    check(bool(state_parts(req)), "LLM 请求被注入自身状态")
    check(req.system_prompt == "你是小鲸鱼。", "system prompt 保持原样（不破坏前缀缓存）")
    print("       ---- 注入内容（用户消息末尾的临时块）----")
    for line in state_parts(req)[0].text.splitlines():
        print("       " + line)
    await plugin.inject_state(FakeEvent(), req)
    check(len(state_parts(req)) == 1, "同一请求重复触发不会重复注入")

    stranger = FakeEvent(sender="10086")
    req2 = FakeRequest()
    plugin.config["inject_scope"] = "admin"
    await plugin.inject_state(stranger, req2)
    check(not state_parts(req2), "inject_scope=admin 时非管理员不注入")
    plugin.config["inject_scope"] = "all"

    plugin.scheduler.start()
    await asyncio.sleep(0.2)
    check(plugin.scheduler.running, "tick 调度器启动")
    await plugin.terminate()
    check(not plugin.scheduler.running, "terminate() 停止调度器")

    tmp.cleanup()
    print()
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        return 1
    print("全部冒烟检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
