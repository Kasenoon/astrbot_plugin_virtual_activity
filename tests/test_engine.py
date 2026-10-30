"""离线验证核心逻辑：不依赖 AstrBot 运行时。"""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from astrbot_plugin_virtual_activity.core import models, store as store_module
from astrbot_plugin_virtual_activity.core.books import BookShelf
from astrbot_plugin_virtual_activity.core.engine import VirtualRoomEngine
from astrbot_plugin_virtual_activity.core.store import RoomStore
from astrbot_plugin_virtual_activity.core.timewin import (
    in_window,
    is_allowed,
    next_allowed,
    parse_windows,
)

BASE_CONFIG = {
    "tick_min_minutes": 20,
    "tick_max_minutes": 45,
    "active_hours": "08:00-20:00",
    "quiet_hours": "23:00-08:00",
    "multi_effect_policy": "llm",
    "default_duration_sec": 600,
    "recent_limit": 20,
    "enable_auto_tick": True,
    "llm_timeout_sec": 20,
}


class FakeClock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **kwargs) -> None:
        self.moment = self.moment + timedelta(**kwargs)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 1, 1, hour, minute, 0).astimezone()


class TestTimeWindow(unittest.TestCase):
    def test_parse_windows(self):
        self.assertEqual(parse_windows("08:00-20:00"), [(480, 1200)])
        self.assertEqual(parse_windows("08:00-12:00, 14:00-18:00"), [(480, 720), (840, 1080)])
        self.assertEqual(parse_windows(""), [])
        self.assertEqual(parse_windows("坏数据"), [])

    def test_cross_midnight(self):
        quiet = parse_windows("23:00-08:00")
        self.assertTrue(in_window(23 * 60 + 30, quiet[0]))
        self.assertTrue(in_window(3 * 60, quiet[0]))
        self.assertFalse(in_window(12 * 60, quiet[0]))

    def test_is_allowed(self):
        active = parse_windows("08:00-20:00")
        quiet = parse_windows("23:00-08:00")
        self.assertTrue(is_allowed(at(9), active, quiet))
        self.assertFalse(is_allowed(at(7), active, quiet))
        self.assertFalse(is_allowed(at(21), active, quiet))

    def test_next_allowed_moves_forward(self):
        active = parse_windows("08:00-20:00")
        quiet = parse_windows("23:00-08:00")
        moved = next_allowed(at(21), active, quiet)
        self.assertEqual((moved.hour, moved.minute), (8, 0))


class EngineTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.store = RoomStore(self.tmpdir)
        self.store.ensure()
        self.shelf = BookShelf(self.tmpdir)
        self.shelf.add_text(
            "测试小说.txt",
            ("第一句话。第二句话！第三句话？" * 60 + "\n") * 2,
        )
        self.clock = FakeClock(at(9))
        self.cfg = dict(BASE_CONFIG)
        self.prompts: list[tuple[str, str]] = []
        self.responses: list[object] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------ 构造

    def make_engine(self, videos: list | None = None, books="default") -> VirtualRoomEngine:
        responses = list(self.responses)
        self.video_requests: list[tuple] = []

        async def generate(system_prompt: str, prompt: str) -> str:
            self.prompts.append((system_prompt, prompt))
            if not responses:
                return ""
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return str(item)

        async def video_source(rids, cookie, timeout):
            self.video_requests.append((tuple(rids), cookie, timeout))
            return list(videos if videos is not None else [])

        return VirtualRoomEngine(
            store=self.store,
            config=lambda key, default=None: self.cfg.get(key, default),
            generate=generate,
            logger=None,
            clock=self.clock,
            video_source=video_source,
            books=self.shelf if books == "default" else books,
        )

    def room(self) -> dict:
        return self.store.load_room()

    # ------------------------------------------------------------ 基本

    async def test_click_writes_state(self):
        engine = self.make_engine()
        result = await engine.run_activity("default", "sofa", source="click", effect_id="sofa_rest")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.effect["id"], "sofa_rest")
        self.assertTrue(result.text)

        state = self.store.load_state("default")
        self.assertEqual(state["behavior"]["furniture_id"], "sofa")
        self.assertEqual(state["behavior"]["effect_id"], "sofa_rest")
        self.assertIsNotNone(state["started_at"])
        self.assertIsNotNone(state["ends_at"])
        self.assertEqual(state["last_trigger_source"], "click")
        self.assertEqual(state["recent_behaviors"][-1]["effect_id"], "sofa_rest")
        # 位置取的是家具中心
        sofa = models.find_furniture(self.room(), "sofa")
        self.assertEqual(state["location"]["x"], sofa["x"] + sofa["w"] / 2)
        # 供房间视图画机器人图标的位置，活动结束后也保留
        self.assertEqual(state["position"]["x"], state["location"]["x"])

    async def test_position_survives_activity_end(self):
        engine = self.make_engine()
        await engine.run_activity("default", "sofa", source="click", effect_id="sofa_rest")
        before = self.store.load_state("default")["position"]
        self.clock.advance(minutes=30)
        refreshed = engine.load_state("default")
        self.assertIsNone(refreshed["location"])
        self.assertIsNone(refreshed["behavior"])
        self.assertEqual(refreshed["position"], before)

    async def test_activity_blocks_second_trigger(self):
        engine = self.make_engine()
        first = await engine.run_activity("default", "sofa", source="click", effect_id="sofa_rest")
        self.assertTrue(first.ok)
        second = await engine.run_activity("default", "desk", source="click", effect_id="desk_code")
        self.assertFalse(second.ok)
        self.assertEqual(second.reason, "busy")

    async def test_lock_releases_after_ends_at(self):
        engine = self.make_engine()
        await engine.run_activity("default", "sofa", source="click", effect_id="sofa_rest")
        self.clock.advance(minutes=20)
        again = await engine.run_activity("default", "desk", source="click", effect_id="desk_code")
        self.assertTrue(again.ok, again.message)

    async def test_prop_has_no_effect(self):
        engine = self.make_engine()
        result = await engine.run_activity("default", "plant", source="click")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "no_effect")
        self.assertIn("摆件", result.message)

    async def test_unknown_furniture(self):
        engine = self.make_engine()
        result = await engine.run_activity("default", "不存在的家具", source="click")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "not_found")

    async def test_outside_window_blocked(self):
        self.clock.moment = at(23, 30)
        engine = self.make_engine()
        result = await engine.run_activity("default", "sofa", source="tick")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "outside_window")

    async def test_bypass_ignores_window_and_lock(self):
        self.clock.moment = at(23, 30)
        engine = self.make_engine()
        await engine.run_activity("default", "sofa", source="click", effect_id="sofa_rest")
        result = await engine.run_activity("default", "desk", source="test", bypass=True)
        self.assertTrue(result.ok, result.message)
        self.assertTrue(result.bypassed)

    # ------------------------------------------------------------ tick

    async def test_tick_only_picks_interactive_furniture(self):
        engine = self.make_engine(videos=self.TV_POOL)
        picked = []
        for _ in range(25):
            engine.clear_behavior("default")
            fresh = self.store.load_state("default")
            fresh["tv_watched"] = 0
            fresh["tv_watched_bvids"] = []
            fresh["tv_date"] = None
            self.store._atomic_write(self.store.state_path("default"), fresh)
            result = await engine.tick(source="test", bypass=True)
            self.assertTrue(result.ok, result.message)
            self.assertTrue(result.furniture["effects"])
            picked.append(result.furniture["id"])
        self.assertNotIn("plant", picked)

    async def test_tick_without_candidates(self):
        room = self.room()
        for furniture in room["furniture"]:
            furniture["effects"] = []
        self.store.save_room(room)
        engine = self.make_engine()
        result = await engine.tick(source="test", bypass=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "no_candidate")

    # ------------------------------------------------------------ LLM

    async def test_llm_chooses_effect_and_text(self):
        self.responses = [json.dumps({"effect_id": "sofa_rest", "text": "我在沙发上躺会儿。"}, ensure_ascii=False)]
        engine = self.make_engine()
        result = await engine.run_activity("default", "sofa", source="click")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.effect["id"], "sofa_rest")
        self.assertEqual(result.text, "我在沙发上躺会儿。")
        self.assertEqual(len(self.prompts), 1, "选效果和写文案应合并成一次调用")

    def disable_reading(self) -> None:
        """把沙发上的「看书」改回普通互动，避免随机流走到读书分支。"""
        room = self.room()
        effect = models.find_effect(models.find_furniture(room, "sofa"), "sofa_read")
        effect["action"] = ""
        self.store.save_room(room)

    async def test_llm_garbage_falls_back_to_random_and_template(self):
        self.disable_reading()
        self.responses = ["这不是 JSON", "还是不是 JSON"]
        engine = self.make_engine()
        result = await engine.run_activity("default", "sofa", source="click")
        self.assertTrue(result.ok, result.message)
        self.assertIn(result.effect["id"], {"sofa_read", "sofa_rest"})
        self.assertIn("我在沙发这儿", result.text)

    async def test_decision_retries_once_on_bad_output(self):
        self.responses = ["这不是 JSON", '{"effect_id": "sofa_rest", "text": "躺一会儿"}']
        engine = self.make_engine()
        result = await engine.run_activity("default", "sofa", source="click")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.effect["id"], "sofa_rest")
        self.assertEqual(result.text, "躺一会儿")
        self.assertEqual(len(self.prompts), 2)
        self.assertIn("必须只输出一个 JSON", self.prompts[1][1])

    async def test_llm_exception_falls_back(self):
        self.disable_reading()
        self.responses = [RuntimeError("boom")]
        engine = self.make_engine()
        result = await engine.run_activity("default", "sofa", source="click")
        self.assertTrue(result.ok, result.message)
        self.assertIn("沙发", result.text)

    async def test_single_effect_uses_phrase_prompt(self):
        self.responses = [json.dumps({"text": "我在窗边看云。"}, ensure_ascii=False)]
        engine = self.make_engine()
        result = await engine.run_activity("default", "window_side", source="click")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.text, "我在窗边看云。")
        self.assertIn("窗边", self.prompts[0][1])

    async def test_text_is_cleaned(self):
        self.responses = [json.dumps({"text": "“我在沙发上躺会儿。”"}, ensure_ascii=False)]
        engine = self.make_engine()
        result = await engine.run_activity("default", "sofa", source="click", effect_id="sofa_rest")
        self.assertEqual(result.text, "我在沙发上躺会儿。")

    # ------------------------------------------------------------ 状态

    async def test_persistence_across_instances(self):
        engine = self.make_engine()
        await engine.run_activity("default", "sofa", source="click", effect_id="sofa_rest")
        second = self.make_engine()
        status = second.status("default")
        self.assertTrue(status["busy"])
        self.assertGreater(status["next_tick_in_sec"], 0)

    async def test_next_tick_inside_active_window(self):
        self.clock.moment = at(19, 50)
        engine = self.make_engine()
        result = await engine.run_activity("default", "sofa", source="click", effect_id="sofa_rest")
        self.assertTrue(result.ok)
        scheduled = store_module.parse_iso(result.next_tick_at)
        self.assertIsNotNone(scheduled)
        self.assertTrue(is_allowed(scheduled, parse_windows("08:00-20:00"), parse_windows("23:00-08:00")))

    async def test_recent_behaviors_trimmed(self):
        self.cfg["recent_limit"] = 5
        engine = self.make_engine()
        for _ in range(8):
            engine.clear_behavior("default")
            await engine.run_activity("default", "sofa", source="click", effect_id="sofa_rest")
        state = self.store.load_state("default")
        self.assertEqual(len(state["recent_behaviors"]), 5)

    async def test_no_temp_files_left(self):
        engine = self.make_engine()
        await engine.run_activity("default", "sofa", source="click", effect_id="sofa_rest")
        leftovers = list(self.tmpdir.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    async def test_room_file_is_valid_json(self):
        engine = self.make_engine()
        await engine.run_activity("default", "sofa", source="click")
        with (self.tmpdir / "room.json").open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertIn("furniture", data)
        self.assertTrue(data["furniture"])

    # ------------------------------------------------------------ 验收

    async def test_click_and_tick_share_pipeline(self):
        engine = self.make_engine(videos=self.TV_POOL)
        tick_result = await engine.tick(source="test", bypass=True)
        self.assertTrue(tick_result.ok, tick_result.message)
        engine.clear_behavior("default")
        fresh = self.store.load_state("default")
        fresh["tv_watched"] = 0
        fresh["tv_watched_bvids"] = []
        fresh["tv_date"] = None
        self.store._atomic_write(self.store.state_path("default"), fresh)
        click_result = await engine.run_activity(
            "default",
            tick_result.furniture["id"],
            source="click",
            effect_id=tick_result.effect["id"],
            bypass=True,
        )
        self.assertTrue(click_result.ok, click_result.message)

        self.assertEqual(set(tick_result.as_dict()), set(click_result.as_dict()))
        self.assertEqual(set(tick_result.state), set(click_result.state))
        for key in ("behavior", "location", "started_at", "ends_at"):
            self.assertIn(key, click_result.state)
        self.assertEqual(click_result.state["behavior"]["effect_id"], tick_result.state["behavior"]["effect_id"])

    async def test_disabled_furniture_rejected(self):
        room = self.room()
        models.find_furniture(room, "sofa")["enabled"] = False
        self.store.save_room(room)
        engine = self.make_engine()
        result = await engine.run_activity("default", "sofa", source="click")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "disabled")


    # ------------------------------------------------------------ 精力 / 社交

    def picks(self, energy: float, times: int = 400) -> dict[str, int]:
        room = self.room()
        rng = random.Random(20260101)
        counts: dict[str, int] = {}
        for _ in range(times):
            furniture = models.pick_furniture(room, energy, rng)
            counts[furniture["id"]] = counts.get(furniture["id"], 0) + 1
        return counts

    def test_low_energy_prefers_resting_furniture(self):
        low = self.picks(5)
        high = self.picks(95)
        rest_low = low.get("bed", 0) + low.get("sofa", 0)
        rest_high = high.get("bed", 0) + high.get("sofa", 0)
        self.assertGreater(rest_low / 400, 0.55, low)
        self.assertLess(rest_high / 400, 0.42, high)

    async def test_energy_delta_applied(self):
        engine = self.make_engine()
        # 先做一件费神的事
        first = await engine.run_activity("default", "desk", source="click", effect_id="desk_code", bypass=True)
        self.assertTrue(first.ok, first.message)
        after_desk = float(first.state["energy"])
        self.assertLessEqual(after_desk, 80 - 18 + 1)

        engine.clear_behavior("default")
        second = await engine.run_activity("default", "bed", source="click", effect_id="bed_sleep", bypass=True)
        self.assertTrue(second.ok, second.message)
        self.assertGreater(float(second.state["energy"]), after_desk)

    async def test_social_grows_over_time(self):
        state = self.store.load_state("default")
        state["social_hunger"] = 10.0
        state["updated_at"] = store_module.isoformat(self.clock.moment - timedelta(minutes=30))
        self.store._atomic_write(self.store.state_path("default"), state)

        engine = self.make_engine()
        result = await engine.run_activity("default", "sofa", source="click", effect_id="sofa_rest", bypass=True)
        self.assertTrue(result.ok, result.message)
        social = float(result.state["social_hunger"])
        # 每分钟 +1~+2，30 分钟应落在 30~60 之间
        self.assertGreaterEqual(social, 10 + 30)
        self.assertLessEqual(social, 10 + 60)

    async def test_social_grows_with_fractional_rate(self):
        # 每分钟只涨 0.2~0.2，30 分钟应该正好 +6.0（而不是被取整成 0 或 1）
        self.cfg["social_grow_min"] = 0.2
        self.cfg["social_grow_max"] = 0.2
        state = self.store.load_state("default")
        state["social_hunger"] = 10.0
        state["updated_at"] = store_module.isoformat(self.clock.moment - timedelta(minutes=30))
        self.store._atomic_write(self.store.state_path("default"), state)

        engine = self.make_engine()
        result = await engine.run_activity("default", "sofa", source="click", effect_id="sofa_rest", bypass=True)
        social = float(result.state["social_hunger"])
        self.assertAlmostEqual(social, 16.0, places=1)

    async def test_social_threshold_accepts_fraction(self):
        self.cfg["social_talk_threshold"] = 45.5
        engine = self.make_engine()
        below = {"energy": 80, "social_hunger": 45.4}
        self.assertFalse(engine._should_socialize(below))
        engine.rng.seed(1)
        above = {"energy": 80, "social_hunger": 45.6}
        # 45.6 已过阈值，概率为 (0.456)^2 ≈ 0.21，多试几次一定能触发
        self.assertTrue(any(engine._should_socialize(above) for _ in range(60)))

    async def test_social_talk_cost_accepts_fraction(self):
        self.cfg["social_talk_cost"] = 10.5
        self.responses = ['{"text": "在忙吗？"}']
        engine = self.make_engine()
        state = self.store.load_state("default")
        state["social_hunger"] = 80.0
        self.store._atomic_write(self.store.state_path("default"), state)
        result = await engine.run_social("default", source="test", bypass=True)
        self.assertTrue(result.ok, result.message)
        self.assertAlmostEqual(float(result.state["social_hunger"]), 69.5, places=1)

    async def test_high_social_triggers_talking(self):
        state = self.store.load_state("default")
        state["social_hunger"] = 100.0
        self.store._atomic_write(self.store.state_path("default"), state)

        engine = self.make_engine()
        result = await engine.tick(source="test", bypass=True)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.kind, "social")
        self.assertTrue(result.text)
        self.assertLess(float(result.state["social_hunger"]), 100.0)
        self.assertEqual(result.state["recent_behaviors"][-1]["effect_id"], "social_talk")

    async def test_social_below_threshold_keeps_room_activity(self):
        state = self.store.load_state("default")
        state["social_hunger"] = 50.0
        self.store._atomic_write(self.store.state_path("default"), state)

        engine = self.make_engine(videos=self.TV_POOL)
        result = await engine.tick(source="test", bypass=True)
        self.assertTrue(result.ok, result.message)
        self.assertNotEqual(result.kind, "social")

    async def test_decision_prompt_mentions_tiredness(self):
        self.responses = ['{"effect_id": "sofa_rest", "text": "我先躺会儿。"}']
        state = self.store.load_state("default")
        state["energy"] = 12.0
        state["social_hunger"] = 80.0
        self.store._atomic_write(self.store.state_path("default"), state)

        engine = self.make_engine()
        result = await engine.run_activity("default", "sofa", source="click", bypass=True)
        self.assertTrue(result.ok, result.message)
        prompt = self.prompts[0][1]
        self.assertIn("很累", prompt)
        self.assertIn("想他", prompt)
        self.assertIn("很放松", prompt)

        from astrbot_plugin_virtual_activity.core import llm

        desk = models.find_furniture(self.room(), "desk")
        desk_prompt = llm.build_decision_prompt(
            robot_name="小鲸鱼",
            room_name="我的房间",
            furniture=desk,
            effects=desk["effects"],
            state={"energy": 12, "social_hunger": 80},
            clock_text="09:00",
            social_threshold=70,
        )
        self.assertIn("挺费神", desk_prompt)


    async def test_tick_applies_read_boost(self):
        captured: dict = {}
        from astrbot_plugin_virtual_activity.core import models as models_module

        original = models_module.pick_furniture

        def spy(room, energy, rng=None, boosts=None, allowed=None):
            captured.update(boosts or {})
            return original(room, energy, rng, boosts, allowed)

        models_module.pick_furniture = spy
        try:
            engine = self.make_engine(videos=self.TV_POOL)
            await engine.tick(source="tick")
        finally:
            models_module.pick_furniture = original
        self.assertIn("sofa", captured)
        self.assertAlmostEqual(captured["sofa"], 1.6, places=2)

    # ------------------------------------------------------------ 读书

    async def test_reading_advances_book(self):
        engine = self.make_engine()
        result = await engine.run_activity("default", "sofa", source="click", effect_id="sofa_read")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.kind, "reading")
        self.assertTrue(result.state["book_title"])
        self.assertGreater(result.state["read_progress"], 0)
        self.assertTrue(result.state["book_excerpt"])
        self.assertGreater(self.shelf.list()[0]["offset"], 0)

    async def test_reading_without_books_fails(self):
        engine = self.make_engine(books=None)
        result = await engine.run_activity("default", "sofa", source="click", effect_id="sofa_read")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "no_book")

    async def test_reading_is_silent_on_tick(self):
        engine = self.make_engine()
        result = await engine.run_activity("default", "sofa", source="tick", effect_id="sofa_read", bypass=True)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.text, "")
        self.assertTrue(result.state["book_title"])

    # ------------------------------------------------------------ 是否报备

    async def test_tick_is_silent_by_default(self):
        engine = self.make_engine(videos=self.TV_POOL)
        engine.rng.seed(11)
        result = await engine.tick(source="tick")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.text, "")
        self.assertEqual(self.prompts, [])  # 不报备就不该再调 LLM 写文案

    async def test_tick_speaks_when_enabled(self):
        self.cfg["announce_activity"] = True
        self.responses = ['{"text": "我在沙发上看书。"}']
        engine = self.make_engine(videos=self.TV_POOL)
        engine.rng.seed(11)
        result = await engine.tick(source="tick")
        self.assertTrue(result.ok, result.message)
        self.assertTrue(result.text)

    async def test_manual_click_still_speaks(self):
        engine = self.make_engine(videos=self.TV_POOL)
        result = await engine.run_activity("default", "sofa", source="click", effect_id="sofa_rest")
        self.assertTrue(result.ok, result.message)
        self.assertTrue(result.text)

    async def test_tv_tick_silent_by_default(self):
        self.clock.moment = at(13)
        engine = self.make_engine(videos=self.TV_POOL)
        result = await engine.run_activity("default", "tv", source="tick", bypass=True)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.text, "")
        self.assertEqual(self.prompts, [])
        self.assertEqual(result.state["tv_watched"], 2)  # 状态照常记录

    async def test_state_keeps_old_last_text_when_silent(self):
        engine = self.make_engine(videos=self.TV_POOL)
        await engine.run_activity("default", "sofa", source="click", effect_id="sofa_rest")
        spoken = self.store.load_state("default")["last_text"]
        self.assertTrue(spoken)
        engine.clear_behavior("default")
        self.clock.advance(minutes=30)
        await engine.tick(source="tick")
        self.assertEqual(self.store.load_state("default")["last_text"], spoken)

    # ------------------------------------------------------------ 电视

    TV_POOL = [
        {
            "bvid": "BV1tv0001",
            "title": "猫咪的一天",
            "owner": "猫猫社",
            "views": 120000,
            "duration": 300,
            "tname": "生活",
            "desc": "记录一只猫",
            "url": "https://www.bilibili.com/video/BV1tv0001",
        },
        {
            "bvid": "BV1tv0002",
            "title": "恐怖游戏实况 第 3 期",
            "owner": "吓人UP",
            "views": 88000,
            "duration": 420,
            "tname": "单机游戏",
            "desc": "深夜慎入",
            "url": "https://www.bilibili.com/video/BV1tv0002",
        },
    ]

    def test_tv_window_weekday_and_weekend(self):
        engine = self.make_engine()
        self.assertTrue(engine.tv_allowed(at(13)))
        self.assertFalse(engine.tv_allowed(at(15)))
        self.assertTrue(engine.tv_allowed(at(20)))

        weekend = FakeClock(datetime(2026, 1, 3, 3, 0, 0).astimezone())
        engine2 = self.make_engine()
        engine2._clock = weekend
        self.assertTrue(engine2.tv_allowed(weekend()))

    async def test_tv_outside_hours_rejected(self):
        self.clock.moment = at(15)
        engine = self.make_engine(videos=self.TV_POOL)
        result = await engine.run_activity("default", "tv", source="click")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "tv_closed")
        self.assertIn("谷时", result.message)

    async def test_tv_daily_limit(self):
        self.clock.moment = at(13)
        state = self.store.load_state("default")
        state["tv_date"] = self.clock.moment.strftime("%Y-%m-%d")
        state["tv_watched"] = 3
        self.store._atomic_write(self.store.state_path("default"), state)

        engine = self.make_engine(videos=self.TV_POOL)
        result = await engine.run_activity("default", "tv", source="click")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "tv_limit")

    async def test_tv_watch_success(self):
        self.clock.moment = at(13)
        self.cfg["tv_videos_per_session"] = 2
        self.responses = [json.dumps({"text": "看了两个视频", "share": True, "reason": "你最近在玩这个游戏"}, ensure_ascii=False)]

        engine = self.make_engine(videos=self.TV_POOL)
        result = await engine.run_activity("default", "tv", source="click")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.kind, "tv")
        self.assertIn("BV1tv", result.text)
        self.assertIn("你最近在玩这个游戏", result.text)
        self.assertEqual(result.state["tv_watched"], 2)
        self.assertEqual(len(result.state["tv_watched_bvids"]), 2)
        self.assertTrue(self.video_requests)

    async def test_tv_share_false_keeps_message_short(self):
        self.clock.moment = at(13)
        self.responses = [json.dumps({"text": "看了会儿视频", "share": False}, ensure_ascii=False)]
        engine = self.make_engine(videos=self.TV_POOL)
        result = await engine.run_activity("default", "tv", source="click")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.text, "看了会儿视频")

    async def test_tv_empty_pool(self):
        self.clock.moment = at(13)
        engine = self.make_engine(videos=[])
        result = await engine.run_activity("default", "tv", source="click")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "tv_empty")

    async def test_tick_skips_tv_outside_hours(self):
        self.clock.moment = at(15)
        engine = self.make_engine(videos=self.TV_POOL)
        engine.rng.seed(7)
        for _ in range(40):
            engine.clear_behavior("default")
            result = await engine.tick(source="test")
            if result.ok:
                self.assertNotEqual(result.furniture["id"], "tv")


class TestMigration(unittest.TestCase):
    def test_legacy_room_inherits_energy_delta(self):
        room = models.default_room()
        for furniture in room["furniture"]:
            for effect in furniture["effects"]:
                effect.pop("energy_delta", None)
                effect.pop("social_delta", None)
        bed = next(item for item in room["furniture"] if item["id"] == "bed")
        bed["effects"].append({"id": "custom_rest", "name": "自定义休息", "duration_sec": 300, "weight": 1})

        normalized = models.normalize_room(room)
        sleep = models.find_effect(models.find_furniture(normalized, "bed"), "bed_sleep")
        custom = models.find_effect(models.find_furniture(normalized, "bed"), "custom_rest")
        self.assertEqual(sleep["energy_delta"], 30)
        self.assertEqual(custom["energy_delta"], models.DEFAULT_ENERGY_DELTA)
        self.assertEqual(sleep["social_delta"], 0)

    def test_explicit_delta_is_kept(self):
        room = models.default_room()
        effect = models.find_effect(models.find_furniture(room, "sofa"), "sofa_rest")
        effect["energy_delta"] = 42
        normalized = models.normalize_room(room)
        kept = models.find_effect(models.find_furniture(normalized, "sofa"), "sofa_rest")
        self.assertEqual(kept["energy_delta"], 42)

    def test_legacy_room_gets_tv_once(self):
        room = models.default_room()
        room["furniture"] = [item for item in room["furniture"] if item["id"] != "tv"]
        room.pop("migrations", None)

        normalized = models.normalize_room(room)
        tvs = [item for item in normalized["furniture"] if models.is_tv_furniture(item)]
        self.assertEqual(len(tvs), 1)
        self.assertIn("tv", normalized["migrations"])

        again = models.normalize_room(normalized)
        tvs_again = [item for item in again["furniture"] if models.is_tv_furniture(item)]
        self.assertEqual(len(tvs_again), 1)

    def test_removed_tv_is_not_re_added(self):
        room = models.default_room()
        room["furniture"] = [item for item in room["furniture"] if item["id"] != "tv"]
        room["migrations"] = ["tv"]
        normalized = models.normalize_room(room)
        self.assertEqual([item for item in normalized["furniture"] if models.is_tv_furniture(item)], [])

    def test_long_avatar_data_uri_kept(self):
        room = models.default_room()
        uri = "data:image/webp;base64," + "A" * 5000
        room["room"]["robots"][0]["avatar"] = uri
        normalized = models.normalize_room(room)
        self.assertEqual(normalized["room"]["robots"][0]["avatar"], uri)

    def test_robot_avatar_top_level_fallback(self):
        room = models.default_room()
        room["room"]["robots"][0]["avatar"] = ""
        room["robot_avatar"] = "🐋"
        normalized = models.normalize_room(room)
        self.assertEqual(normalized["room"]["robots"][0]["avatar"], "🐋")

    def test_avatar_size_and_fit_normalized(self):
        room = models.default_room()
        room["room"]["robots"][0]["avatar_size"] = 900
        room["room"]["robots"][0]["avatar_fit"] = "奇怪的值"
        normalized = models.normalize_room(room)
        self.assertEqual(normalized["room"]["robots"][0]["avatar_size"], 512)
        self.assertEqual(normalized["room"]["robots"][0]["avatar_fit"], "cover")

    def test_avatar_defaults(self):
        room = models.default_room()
        room["room"]["robots"][0].pop("avatar_size", None)
        room["room"]["robots"][0].pop("avatar_fit", None)
        normalized = models.normalize_room(room)
        self.assertEqual(normalized["room"]["robots"][0]["avatar_size"], 128)
        self.assertEqual(normalized["room"]["robots"][0]["avatar_fit"], "cover")


if __name__ == "__main__":
    unittest.main()

