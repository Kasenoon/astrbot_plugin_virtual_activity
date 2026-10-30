"""书架测试：编码识别、分段推进、进度与循环。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_virtual_activity.core.books import BookShelf, clean_novel, decode_text

SAMPLE = "".join(f"第{i}句话，内容各不相同。" for i in range(200))


class TestDecode(unittest.TestCase):
    def test_utf8(self):
        self.assertIn("第一句", decode_text("第一句。".encode("utf-8")))

    def test_gbk(self):
        self.assertIn("第一句", decode_text("第一句。".encode("gb18030")))

    def test_crlf_normalized(self):
        self.assertNotIn("\r", decode_text(b"a\r\nb"))


class TestClean(unittest.TestCase):
    def test_strip_credits(self):
        raw = "书名\n\n图源：某人\n扫图：某人\n录入：某人\n台版 转自 轻之国度\n\n正文第一句。"
        cleaned = clean_novel(raw)
        self.assertNotIn("图源", cleaned)
        self.assertNotIn("扫图", cleaned)
        self.assertNotIn("轻之国度", cleaned)
        self.assertIn("正文第一句", cleaned)

    def test_collapse_blank_lines(self):
        self.assertNotIn("\n\n\n", clean_novel("甲\n\n\n\n乙"))


class TestShelf(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.shelf = BookShelf(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_add_and_list(self):
        book = self.shelf.add_text("测试小说.txt", SAMPLE.encode("utf-8"))
        self.assertEqual(book["title"], "测试小说")
        self.assertEqual(len(self.shelf.list()), 1)
        self.assertEqual(self.shelf.active, book["id"])
        # 索引落盘后重新加载仍在
        again = BookShelf(Path(self._tmp.name))
        self.assertEqual(len(again.list()), 1)
        self.assertEqual(again.active, book["id"])

    def test_reject_empty(self):
        with self.assertRaises(ValueError):
            self.shelf.add_text("空.txt", b"   ")

    def test_read_next_advances(self):
        self.shelf.add_text("书.txt", SAMPLE.encode("utf-8"))
        first = self.shelf.read_next(chars=200)
        self.assertIsNotNone(first)
        self.assertGreater(first["chars"], 0)
        second = self.shelf.read_next(chars=200)
        self.assertNotEqual(first["text"], second["text"])
        self.assertGreater(second["offset"], first["offset"])
        self.assertGreater(second["progress"], first["progress"])

    def test_chunk_ends_at_sentence(self):
        self.shelf.add_text("书.txt", "短句。" * 200)
        chunk = self.shelf.read_next(chars=100)
        self.assertTrue(chunk["text"].endswith(("。", "！", "？")))

    def test_finish_then_loop(self):
        self.shelf.add_text("小书.txt", "只有一句话。" * 5)
        seen = 0
        for _ in range(20):
            chunk = self.shelf.read_next(chars=1000, loop=True)
            if chunk is None:
                break
            seen += 1
        self.assertGreaterEqual(seen, 1)
        # 循环模式下不会读空
        self.assertIsNotNone(self.shelf.read_next(chars=1000, loop=True))

    def test_no_loop_stops(self):
        self.shelf.add_text("小书.txt", "只有一句话。")
        chunk = self.shelf.read_next(chars=1000, loop=False)
        self.assertIsNotNone(chunk)
        self.assertIsNone(self.shelf.read_next(chars=1000, loop=False))

    def test_empty_shelf(self):
        self.assertIsNone(self.shelf.read_next())

    def test_select_and_delete(self):
        first = self.shelf.add_text("一.txt", SAMPLE.encode("utf-8"))
        second = self.shelf.add_text("二.txt", SAMPLE.encode("utf-8"))
        self.assertTrue(self.shelf.select(second["id"]))
        self.assertEqual(self.shelf.active, second["id"])
        self.assertTrue(self.shelf.delete(second["id"]))
        self.assertEqual(self.shelf.active, first["id"])
        self.assertFalse(self.shelf.select("不存在"))

    def test_preview(self):
        book = self.shelf.add_text("书.txt", SAMPLE.encode("utf-8"))
        preview = self.shelf.preview(book["id"], 50)
        self.assertEqual(preview["id"], book["id"])
        self.assertTrue(preview["text"])

    def test_search_only_in_read_part(self):
        text = "开头就提到妹妹。" + ("中间全是填充内容。" * 300) + "结尾妹妹又出现了。"
        self.shelf.add_text("书.txt", text)
        # 一个字都没读时，什么都搜不到，且提示「还没读到」
        before = self.shelf.search("妹妹")
        self.assertEqual(before["hits"], [])
        self.assertTrue(before["unread_hit"])

        self.shelf.read_next(chars=500, loop=False)
        after = self.shelf.search("妹妹")
        self.assertTrue(after["hits"])
        self.assertIn("妹妹", after["hits"][0])
        self.assertFalse(after["unread_hit"])

    def test_search_empty_keyword(self):
        self.shelf.add_text("书.txt", SAMPLE)
        result = self.shelf.search("", limit=3)
        self.assertEqual(result["hits"], [])
        self.assertFalse(result["unread_hit"])

    def test_search_respects_limit(self):
        self.shelf.add_text("书.txt", "关键词测试。" * 200)
        self.shelf.read_next(chars=2000, loop=False)
        result = self.shelf.search("关键词", limit=2, excerpt_chars=80)
        self.assertLessEqual(len(result["hits"]), 2)

    def test_search_without_books(self):
        self.assertIsNone(self.shelf.search("妹妹"))


if __name__ == "__main__":
    unittest.main()
