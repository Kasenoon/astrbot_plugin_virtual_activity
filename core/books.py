"""书架：导入小说、记录阅读进度、按段落取下一段。"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "utf-16", "big5")
SENTENCE_END = "。！？…!?\n"
MAX_BOOK_BYTES = 20 * 1024 * 1024
DEFAULT_CHUNK = 1500

# 轻小说 txt 开头常有的录入信息，导入时顺手丢掉，别让机器人念版权页
CREDIT_PREFIXES = (
    "图源",
    "扫图",
    "录入",
    "修图",
    "校对",
    "翻译",
    "台版",
    "轻之国度",
    "内容简介",
    "作者",
    "插画",
    "本文由",
    "转载",
)


def clean_novel(text: str) -> str:
    """去掉录入信息行、压掉多余空行。"""
    lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped and len(stripped) <= 40 and stripped.startswith(CREDIT_PREFIXES):
            continue
        lines.append(line.rstrip())
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip() + "\n" if cleaned.strip() else ""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def decode_text(raw: bytes | str) -> str:
    if isinstance(raw, str):
        return raw.replace("\r\n", "\n").replace("\r", "\n")
    for encoding in ENCODINGS:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if text.strip():
            return text.replace("\r\n", "\n").replace("\r", "\n")
    return raw.decode("utf-8", errors="ignore").replace("\r\n", "\n")


def normalize_title(filename: str) -> str:
    stem = Path(filename).stem or "未命名"
    return re.sub(r"\s+", " ", stem).strip()[:60]


@dataclass
class Book:
    id: str
    title: str
    filename: str
    chars: int = 0
    offset: int = 0
    finished: bool = False
    added_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        progress = 0 if self.chars <= 0 else min(100.0, self.offset * 100.0 / self.chars)
        return {
            "id": self.id,
            "title": self.title,
            "filename": self.filename,
            "chars": self.chars,
            "offset": self.offset,
            "finished": self.finished,
            "progress": round(progress, 1),
            "added_at": self.added_at,
            "updated_at": self.updated_at,
        }


@dataclass
class BookShelf:
    data_dir: Path
    books: list[Book] = field(default_factory=list)
    active: str | None = None

    # ---------------------------------------------------------- 存储

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.books_dir = self.data_dir / "books"
        self.index_path = self.data_dir / "books.json"
        self.books_dir.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not self.index_path.exists():
            return
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.active = payload.get("active")
        for item in payload.get("books") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            self.books.append(
                Book(
                    id=str(item["id"]),
                    title=str(item.get("title") or "未命名"),
                    filename=str(item.get("filename") or ""),
                    chars=int(item.get("chars") or 0),
                    offset=int(item.get("offset") or 0),
                    finished=bool(item.get("finished")),
                    added_at=str(item.get("added_at") or ""),
                    updated_at=str(item.get("updated_at") or ""),
                )
            )

    def _save(self) -> None:
        payload = {
            "active": self.active,
            "books": [book.to_dict() | {"filename": book.filename} for book in self.books],
        }
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.index_path)

    # ---------------------------------------------------------- 查询

    def find(self, book_id: str) -> Book | None:
        for book in self.books:
            if book.id == book_id:
                return book
        return None

    def list(self) -> list[dict[str, Any]]:
        return [book.to_dict() for book in self.books]

    def active_book(self) -> Book | None:
        book = self.find(self.active) if self.active else None
        if book is not None:
            return book
        return self.books[0] if self.books else None

    def select(self, book_id: str) -> bool:
        if self.find(book_id) is None:
            return False
        self.active = book_id
        self._save()
        return True

    # ---------------------------------------------------------- 增删

    def add_text(self, filename: str, raw: bytes | str) -> dict[str, Any]:
        if len(raw) > MAX_BOOK_BYTES:
            raise ValueError("文件太大了（上限 20MB）")
        text = decode_text(raw)
        text = clean_novel(text)
        if not text.strip():
            raise ValueError("这个文件读不出内容")
        book_id = uuid.uuid4().hex[:12]
        path = self.books_dir / f"{book_id}.txt"
        path.write_text(text, encoding="utf-8")
        book = Book(
            id=book_id,
            title=normalize_title(filename),
            filename=filename,
            chars=len(text),
            offset=0,
            finished=False,
            added_at=_now(),
            updated_at=_now(),
        )
        self.books.append(book)
        if not self.active:
            self.active = book.id
        self._save()
        return book.to_dict()

    def delete(self, book_id: str) -> bool:
        book = self.find(book_id)
        if book is None:
            return False
        self.books = [item for item in self.books if item.id != book_id]
        try:
            (self.books_dir / f"{book_id}.txt").unlink(missing_ok=True)
        except OSError:
            pass
        if self.active == book_id:
            self.active = self.books[0].id if self.books else None
        self._save()
        return True

    # ---------------------------------------------------------- 阅读

    def text_of(self, book: Book) -> str:
        try:
            return (self.books_dir / f"{book.id}.txt").read_text(encoding="utf-8")
        except OSError:
            return ""

    def preview(self, book_id: str, chars: int = 240) -> dict[str, Any]:
        book = self.find(book_id)
        if book is None:
            return {}
        text = self.text_of(book)
        start = max(0, min(book.offset, len(text)))
        return {
            "id": book.id,
            "title": book.title,
            "progress": book.to_dict()["progress"],
            "text": text[start : start + chars],
        }

    def read_next(self, chars: int = DEFAULT_CHUNK, loop: bool = True, book_id: str | None = None) -> dict[str, Any] | None:
        """读取下一段；读完一本书按 loop 决定是否从头再来。"""
        book = self.find(book_id) if book_id else self.active_book()
        if book is None:
            return None
        text = self.text_of(book)
        if not text.strip():
            return None

        start = max(0, min(book.offset, len(text)))
        while start < len(text) and text[start].isspace():
            start += 1

        if start >= len(text):
            if not loop:
                book.finished = True
                book.offset = len(text)
                self._save()
                return None
            start = 0
            book.finished = False

        end = min(len(text), start + max(50, chars))
        if end < len(text):
            window = text[end : end + 120]
            for index, char in enumerate(window):
                if char in SENTENCE_END:
                    end = end + index + 1
                    break

        chunk = text[start:end].strip()
        if not chunk:
            return None

        book.offset = end
        book.finished = end >= len(text)
        book.updated_at = _now()
        self._save()

        total = len(text)
        return {
            "book_id": book.id,
            "title": book.title,
            "text": chunk,
            "chars": len(chunk),
            "offset": book.offset,
            "total": total,
            "progress": round(book.offset * 100.0 / total, 1) if total else 0.0,
            "finished": book.finished,
        }

    def search(
        self,
        query: str,
        book_id: str | None = None,
        limit: int = 2,
        excerpt_chars: int = 240,
    ) -> dict[str, Any] | None:
        """只在**已读部分**里找关键词，避免剧透。"""
        book = self.find(book_id) if book_id else self.active_book()
        if book is None:
            return None
        text = self.text_of(book)
        if not text.strip():
            return None

        keyword = str(query or "").strip()
        read_part = text[: max(0, book.offset)]
        hits: list[str] = []
        if keyword:
            start = 0
            while len(hits) < max(1, limit):
                index = read_part.find(keyword, start)
                if index < 0:
                    break
                left = max(0, index - excerpt_chars // 2)
                right = min(len(read_part), index + len(keyword) + excerpt_chars // 2)
                snippet = text[left:right].replace("\n", " ").strip()
                if snippet and all(snippet not in existing for existing in hits):
                    hits.append(snippet)
                start = index + len(keyword)

        unread_hit = bool(keyword) and not hits and keyword in text[book.offset :]
        return {
            "book_id": book.id,
            "title": book.title,
            "progress": book.to_dict()["progress"],
            "read_chars": book.offset,
            "hits": hits,
            "unread_hit": unread_hit,
        }
