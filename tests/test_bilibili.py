"""B 站抓取与筛选的纯函数测试（不联网）。"""

from __future__ import annotations

import random
import unittest

from astrbot_plugin_virtual_activity.core import bilibili

POPULAR_PAYLOAD = {
    "code": 0,
    "data": {
        "list": [
            {
                "bvid": "BV1aa",
                "title": "猫咪日常",
                "owner": {"name": "猫猫社"},
                "stat": {"view": 120000},
                "duration": 300,
                "tname": "生活",
                "desc": "记录一只猫",
            },
            {
                "bvid": "BV1bb",
                "title": "恐怖游戏实况",
                "owner": {"name": "吓人UP"},
                "stat": {"view": 88000},
                "duration": 1800,
                "tname": "单机游戏",
                "desc": "深夜慎入",
            },
            {
                "bvid": "BV1cc",
                "title": "某游戏公益服发布站",
                "owner": {"name": "广告号"},
                "stat": {"view": 10},
                "duration": 5,
                "tname": "游戏",
                "desc": "广告",
            },
        ]
    },
}

NEWLIST_PAYLOAD = {
    "code": 0,
    "data": {
        "archives": [
            {
                "bvid": "BV1dd",
                "title": "动画区新番吐槽",
                "owner": {"name": "动漫UP"},
                "stat": {"view": 5000},
                "duration": 600,
                "tname": "动画",
                "desc": "新番点评",
            }
        ]
    },
}


class TestResolveRids(unittest.TestCase):
    def test_names_and_numbers(self):
        self.assertEqual(bilibili.resolve_rids("动画,游戏,36"), [1, 4, 36])
        self.assertEqual(bilibili.resolve_rids("动画，动画"), [1])
        self.assertEqual(bilibili.resolve_rids(""), [])
        self.assertEqual(bilibili.resolve_rids("不存在的分区"), [])

    def test_list_input(self):
        self.assertEqual(bilibili.resolve_rids(["动画", "188"]), [1, 188])


class TestPayloadParsing(unittest.TestCase):
    def test_popular_payload(self):
        videos = bilibili.extract_pool(POPULAR_PAYLOAD)
        # 广告标题被过滤
        self.assertEqual([v["bvid"] for v in videos], ["BV1aa", "BV1bb"])
        self.assertEqual(videos[0]["owner"], "猫猫社")
        self.assertEqual(videos[0]["url"], "https://www.bilibili.com/video/BV1aa")

    def test_newlist_payload(self):
        videos = bilibili.extract_pool(NEWLIST_PAYLOAD)
        self.assertEqual(videos[0]["bvid"], "BV1dd")

    def test_bad_payload(self):
        self.assertEqual(bilibili.extract_pool(None), [])
        self.assertEqual(bilibili.extract_pool({"code": -352}), [])


class TestPickVideos(unittest.TestCase):
    def setUp(self):
        self.pool = bilibili.extract_pool(POPULAR_PAYLOAD) + bilibili.extract_pool(NEWLIST_PAYLOAD)

    def test_keyword_filter(self):
        picked = bilibili.pick_videos(self.pool, keywords=["猫"], limit=2)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["bvid"], "BV1aa")

    def test_keyword_fallback_when_no_match(self):
        picked = bilibili.pick_videos(self.pool, keywords=["完全不存在"], limit=2)
        self.assertTrue(picked)

    def test_require_keyword_returns_empty(self):
        picked = bilibili.pick_videos(self.pool, keywords=["完全不存在"], limit=2, require_keyword=True)
        self.assertEqual(picked, [])

    def test_min_views_filter(self):
        pool = [
            make_video("BVlow", 300, views=10),
            make_video("BVhigh", 300, views=50000),
        ]
        picked = bilibili.pick_videos(pool, limit=1, min_views=1000)
        self.assertEqual(picked[0]["bvid"], "BVhigh")

    def test_min_views_relaxes_when_all_low(self):
        pool = [make_video("BVlow", 300, views=10)]
        self.assertEqual(len(bilibili.pick_videos(pool, limit=1, min_views=1000)), 1)

    def test_skip_watched(self):
        picked = bilibili.pick_videos(self.pool, limit=5, watched=["BV1aa", "BV1bb", "BV1dd"])
        self.assertEqual(picked, [])

    def test_limit_and_order(self):
        picked = bilibili.pick_videos(self.pool, limit=2, rng=random.Random(1))
        self.assertEqual(len(picked), 2)
        self.assertNotIn("BV1cc", [v["bvid"] for v in picked])

    def test_empty_pool(self):
        self.assertEqual(bilibili.pick_videos([], limit=2), [])


class TestTvPromptParsing(unittest.TestCase):
    def test_parse_share_true(self):
        from astrbot_plugin_virtual_activity.core import llm

        text, share, reason = llm.parse_tv_decision(
            '{"text": "看了个猫片", "share": true, "reason": "你爱猫"}', []
        )
        self.assertEqual(text, "看了个猫片")
        self.assertTrue(share)
        self.assertEqual(reason, "你爱猫")

    def test_parse_share_false(self):
        from astrbot_plugin_virtual_activity.core import llm

        text, share, reason = llm.parse_tv_decision('{"text": "随便看看", "share": false}', [])
        self.assertEqual(text, "随便看看")
        self.assertFalse(share)

    def test_parse_garbage(self):
        from astrbot_plugin_virtual_activity.core import llm

        text, share, reason = llm.parse_tv_decision("我今天看了个视频", [])
        self.assertEqual(text, "我今天看了个视频")
        self.assertFalse(share)


class TestCategories(unittest.TestCase):
    def test_parse_lines(self):
        cats = bilibili.parse_categories(
            "游戏实况 | 游戏,单机游戏 | 实况,通关\n读书书评 |  | 读书,书评", weight=2.0
        )
        self.assertEqual([c.name for c in cats], ["游戏实况", "读书书评"])
        self.assertEqual(cats[0].partitions, [4, 17])
        self.assertEqual(cats[0].keywords, ["实况", "通关"])
        self.assertEqual(cats[1].keywords, ["读书", "书评"])
        self.assertEqual(cats[0].weight, 2.0)

    def test_skip_empty_lines(self):
        self.assertEqual(bilibili.parse_categories("没有分隔符"), [])
        self.assertEqual(bilibili.parse_categories(""), [])
        self.assertEqual(bilibili.parse_categories("# 注释\n\n"), [])

    def test_keyword_only_line(self):
        cats = bilibili.parse_categories("剧情 | | 剧情,解析")
        self.assertEqual(cats[0].partitions, [])
        self.assertEqual(cats[0].keywords, ["剧情", "解析"])

    def test_pick_category_prefers_heavier(self):
        cats = [
            bilibili.Category("多推", [1], [], 2.0),
            bilibili.Category("可看", [2], [], 1.0),
        ]
        rng = random.Random(7)
        counts = {"多推": 0, "可看": 0}
        for _ in range(300):
            counts[bilibili.pick_category(cats, rng).name] += 1
        self.assertGreater(counts["多推"], counts["可看"] * 1.5)


def make_video(bvid: str, duration: int, views: int = 1000, title: str = "视频") -> dict:
    return {
        "bvid": bvid,
        "title": title,
        "owner": "UP",
        "views": views,
        "duration": duration,
        "tname": "生活",
        "tid": 160,
        "desc": "",
        "url": f"https://www.bilibili.com/video/{bvid}",
    }


class TestDurationRange(unittest.TestCase):
    def test_filter_range(self):
        pool = [make_video("BVshort", 120), make_video("BVmid", 300), make_video("BVlong", 900)]
        picked = bilibili.pick_videos(pool, min_duration=180, max_duration=480, limit=5)
        self.assertEqual([v["bvid"] for v in picked], ["BVmid"])

    def test_fallback_when_nothing_in_range(self):
        pool = [make_video("BVtiny", 60), make_video("BVhuge", 3600)]
        picked = bilibili.pick_videos(pool, min_duration=180, max_duration=480, limit=1)
        self.assertEqual(len(picked), 1)
        self.assertIn(picked[0]["bvid"], {"BVtiny", "BVhuge"})

    def test_no_range_keeps_all(self):
        pool = [make_video("BV1", 60), make_video("BV2", 3600)]
        self.assertEqual(len(bilibili.pick_videos(pool, limit=5)), 2)


if __name__ == "__main__":
    unittest.main()
