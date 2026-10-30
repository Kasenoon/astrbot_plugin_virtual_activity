"""随机 tick 调度器：一个轻量 asyncio 循环，所有时间点都落盘。"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from . import store as store_module
from .engine import ActivityResult, VirtualRoomEngine


class TickScheduler:
    def __init__(
        self,
        engine: VirtualRoomEngine,
        logger: Any = None,
        interval: float = 15.0,
        robot_id: str = "default",
        on_result: Callable[[ActivityResult], Awaitable[None]] | None = None,
    ) -> None:
        self.engine = engine
        self._logger = logger
        self.interval = max(5.0, float(interval))
        self.robot_id = robot_id
        self._on_result = on_result
        self._task: asyncio.Task | None = None
        self._stopping = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _log(self, level: str, message: str) -> None:
        if self._logger is None:
            return
        handler = getattr(self._logger, level, None)
        if callable(handler):
            handler(message)

    def start(self) -> None:
        if self.running:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="virtual_activity_tick")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        self._log("info", "[virtual_activity] tick 调度器已启动")
        try:
            while not self._stopping:
                try:
                    await self._tick_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._log("warning", f"[virtual_activity] tick 执行异常: {exc}")
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            self._log("info", "[virtual_activity] tick 调度器已停止")
            raise

    async def _tick_once(self) -> None:
        state = self.engine.load_state(self.robot_id)
        now = self.engine.now()

        next_at = store_module.parse_iso(state.get("next_tick_at"))
        if next_at is None:
            if self.engine.auto_tick_enabled():
                self.engine.reschedule(self.robot_id)
            return
        if now < next_at:
            return

        if not self.engine.auto_tick_enabled():
            self.engine.reschedule(self.robot_id)
            return

        result = await self.engine.tick(robot_id=self.robot_id, source="tick")
        if result.ok:
            if self._on_result is not None:
                try:
                    await self._on_result(result)
                except Exception as exc:  # noqa: BLE001
                    self._log("warning", f"[virtual_activity] 活动消息发送失败: {exc}")
            return

        ends_at = store_module.parse_iso(state.get("ends_at"))
        if result.reason == "busy" and ends_at is not None:
            self.engine.reschedule(self.robot_id, after=ends_at)
            return
        self.engine.reschedule(self.robot_id)
        if result.reason not in {"outside_window", "busy"}:
            self._log("info", f"[virtual_activity] 本次 tick 跳过: {result.reason} {result.message}")
