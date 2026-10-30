"""自身状态注入文本的单元测试。"""

from __future__ import annotations

import unittest

from astrbot_plugin_virtual_activity.core import prompt


class TestShouldInject(unittest.TestCase):
    def test_state_questions(self):
        self.assertTrue(prompt.should_inject("你在干嘛"))
        self.assertTrue(prompt.should_inject("你现在在哪儿"))
        self.assertTrue(prompt.should_inject("外面下雨了吗"))
        self.assertTrue(prompt.should_inject("今天天气怎么样"))

    def test_plain_chat_skips(self):
        self.assertFalse(prompt.should_inject("帮我写个正则"))
        self.assertFalse(prompt.should_inject("1+1 等于几"))
        self.assertFalse(prompt.should_inject(""))

    def test_furniture_names(self):
        names = ["沙发", "电视", "绿植"]
        self.assertTrue(prompt.should_inject("沙发舒服吗", names))
        self.assertFalse(prompt.should_inject("帮我看看这个 bug", names))

    def test_single_char_words_do_not_trigger(self):
        # 以前「热 / 冷 / 吃 / 睡」是单字关键词，热更新、冷启动、吃灰全误命中
        self.assertFalse(prompt.should_inject("这个热更新有 bug"))
        self.assertFalse(prompt.should_inject("冷启动失败了"))
        self.assertFalse(prompt.should_inject("这条评论在吃灰"))
        self.assertFalse(prompt.should_inject("睡觉前别忘了关灯"))
        # 明确在问状态的仍然命中
        self.assertTrue(prompt.should_inject("外面好热"))
        self.assertTrue(prompt.should_inject("今天好冷啊"))
        self.assertTrue(prompt.should_inject("你刚吃饭了吗"))
        self.assertTrue(prompt.should_inject("你睡着了吗"))

    def test_single_char_furniture_name_skipped(self):
        # 「床」会把「临床」「床位」也命中，单字家具名不参与匹配
        self.assertFalse(prompt.should_inject("临床数据分析", ["床", "书桌"]))
        self.assertTrue(prompt.should_inject("你还在书桌上吗", ["床", "书桌"]))

    def test_book_keywords(self):
        self.assertTrue(prompt.should_inject("你在读什么小说"))
        self.assertTrue(prompt.should_inject("书里那个妹妹是谁"))
        self.assertTrue(prompt.should_inject("你读到哪了"))
        self.assertFalse(prompt.should_inject("帮我把这本书转成 pdf"))


    def test_reading_prompt_marks_fiction(self):
        from astrbot_plugin_virtual_activity.core import llm

        prompt = llm.build_reading_prompt(
            robot_name="小鲸鱼",
            room_name="我的房间",
            clock_text="09:00",
            title="某轻小说",
            text="张三对李四说：我们私奔吧。",
            progress=12.0,
            state={},
        )
        self.assertIn("虚构小说", prompt)
        self.assertIn("不是你现实里认识的人", prompt)
        self.assertIn("不要把书里的人物说成现实里的人", prompt)


class TestStateBlock(unittest.TestCase):
    def test_busy_block(self):
        block = prompt.build_state_block(
            room_name="我的房间",
            now_text="09:12",
            furniture_name="沙发",
            effect_name="休息",
            remaining_minutes=8,
            position="沙发（房间坐标 215, 385）",
            energy=74,
            social=41,
            recent=["窗边·看窗外", "沙发·休息"],
            last_text="我在沙发上躺会儿。",
            last_text_at="09:00",
        )
        self.assertIn(prompt.MARKER, block)
        self.assertIn("我的房间", block)
        self.assertIn("「沙发」休息", block)
        self.assertIn("还有 8 分钟", block)
        self.assertIn("09:12", block)
        self.assertIn("215", block)
        self.assertIn("精力 74", block)
        self.assertIn("窗边·看窗外 → 沙发·休息", block)
        self.assertIn("我在沙发上躺会儿。", block)

    def test_idle_block(self):
        block = prompt.build_state_block(
            room_name="小屋",
            now_text="21:30",
            furniture_name=None,
            effect_name=None,
            energy=50,
            social=60,
        )
        self.assertIn("闲着", block)
        self.assertNotIn("还有", block)

    def test_numbers_tolerate_none(self):
        block = prompt.build_state_block(
            room_name="小屋",
            now_text="21:30",
            energy=None,
            social=None,
        )
        self.assertNotIn("身体：", block)

    def test_merge_appends_once(self):
        block = prompt.build_state_block(room_name="小屋", now_text="08:00")
        merged = prompt.merge_into_system_prompt("原有系统提示", block)
        self.assertTrue(merged.startswith("原有系统提示"))
        self.assertIn(prompt.MARKER, merged)
        again = prompt.merge_into_system_prompt(merged, block)
        self.assertEqual(again.count(prompt.MARKER), 1)

    def test_merge_into_empty_prompt(self):
        block = prompt.build_state_block(room_name="小屋", now_text="08:00")
        self.assertTrue(prompt.merge_into_system_prompt(None, block).startswith(prompt.MARKER))
        self.assertTrue(prompt.merge_into_system_prompt("", block).startswith(prompt.MARKER))

    def test_merge_empty_block_is_noop(self):
        self.assertEqual(prompt.merge_into_system_prompt("abc", ""), "abc")


if __name__ == "__main__":
    unittest.main()
