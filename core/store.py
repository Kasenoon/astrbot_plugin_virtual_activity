"""插件数据持久化：房间布局、机器人状态，全部 JSON + 原子写入。"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from . import models

ROBOT_ID_PATTERN = re.compile(r"^[a-z0-9_\-]{1,40}$")


def now_local() -> datetime:
    return datetime.now().astimezone()


def isoformat(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.isoformat(timespec="seconds")


def parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.astimezone()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment


def default_state(robot_id: str = "default") -> dict[str, Any]:
    return {
        "schema_version": models.SCHEMA_VERSION,
        "robot_id": robot_id,
        "behavior": None,
        "location": None,
        "position": None,
        "started_at": None,
        "ends_at": None,
        "energy": 80.0,
        "social_hunger": 45.0,
        "next_tick_at": None,
        "recent_behaviors": [],
        "last_trigger_source": None,
        "last_text": None,
        "last_text_at": None,
        "last_error": None,
        "tv_date": None,
        "tv_watched": 0,
        "tv_watched_bvids": [],
        "updated_at": isoformat(now_local()),
    }


class RoomStore:
    """``data/plugin_data/<plugin>/`` 下的读写封装。"""

    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir)
        self.room_path = self.data_dir / "room.json"
        self.runtime_path = self.data_dir / "runtime.json"

    # ------------------------------------------------------------ 基础

    def ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.room_path.exists():
            self.save_room(models.default_room())

    def state_path(self, robot_id: str) -> Path:
        safe = robot_id if ROBOT_ID_PATTERN.match(robot_id or "") else "default"
        return self.data_dir / f"state_{safe}.json"

    # ------------------------------------------------------------ JSON

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            # 损坏时留一份备份，避免直接覆盖掉用户数据
            try:
                backup = path.with_suffix(path.suffix + ".broken")
                shutil.copyfile(path, backup)
            except OSError:
                pass
            return default

    @staticmethod
    def _atomic_write(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    # ------------------------------------------------------------ 房间

    def load_room(self) -> dict[str, Any]:
        raw = self._read_json(self.room_path, None)
        try:
            return models.normalize_room(raw if raw is not None else models.default_room())
        except ValueError:
            return models.normalize_room(models.default_room())

    def save_room(self, room: dict[str, Any]) -> dict[str, Any]:
        normalized = models.normalize_room(room)
        self._atomic_write(self.room_path, normalized)
        return normalized

    # ------------------------------------------------------------ 状态

    def load_state(self, robot_id: str = "default") -> dict[str, Any]:
        raw = self._read_json(self.state_path(robot_id), None)
        state = default_state(robot_id)
        if isinstance(raw, dict):
            # 保留插件后续版本新增的字段（比如 tv_*），只做类型兜底
            state.update(raw)
        state["robot_id"] = robot_id
        if not isinstance(state.get("recent_behaviors"), list):
            state["recent_behaviors"] = []
        if not isinstance(state.get("tv_watched_bvids"), list):
            state["tv_watched_bvids"] = []
        return state

    def save_state(self, state: dict[str, Any]) -> dict[str, Any]:
        payload = dict(state)
        payload["updated_at"] = isoformat(now_local())
        self._atomic_write(self.state_path(str(payload.get("robot_id") or "default")), payload)
        return payload

    # ------------------------------------------------------------ 运行时

    def load_runtime(self) -> dict[str, Any]:
        raw = self._read_json(self.runtime_path, {})
        return raw if isinstance(raw, dict) else {}

    def save_runtime(self, runtime: dict[str, Any]) -> None:
        self._atomic_write(self.runtime_path, runtime)

    def remember_admin_umo(self, umo: str) -> None:
        if not umo:
            return
        runtime = self.load_runtime()
        if runtime.get("admin_umo") == umo:
            return
        runtime["admin_umo"] = umo
        self.save_runtime(runtime)
