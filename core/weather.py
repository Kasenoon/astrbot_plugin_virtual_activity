"""和风天气（QWeather）接入：当前天气 + 今日高低温，带缓存与降级。"""

from __future__ import annotations

import asyncio
import gzip
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

USER_AGENT = "astrbot-plugin-virtual-activity/0.4"

# 重庆（渝中区所在城市级 LocationID）；区县可以自己换，比如渝中 101040200
DEFAULT_LOCATION_ID = "101040100"
DEFAULT_CITY = "重庆"
DEFAULT_HOST = "devapi.qweather.com"

WET_WORDS = ("雨", "雪", "雷", "冰雹", "冻雨")
CLEAR_WORDS = ("晴",)


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Weather:
    city: str
    text: str
    temp: float | None = None
    feels_like: float | None = None
    temp_min: float | None = None
    temp_max: float | None = None
    pop: int | None = None
    humidity: int | None = None
    wind: str = ""
    obs_time: str = ""
    fetched_at: datetime | None = None

    # ---------------------------------------------------------- 判定

    def is_wet(self) -> bool:
        return any(word in self.text for word in WET_WORDS)

    def is_clear(self) -> bool:
        return any(word in self.text for word in CLEAR_WORDS)

    def is_hot(self) -> bool:
        return self.temp is not None and self.temp >= 30

    def is_cold(self) -> bool:
        return self.temp is not None and self.temp <= 5

    # ---------------------------------------------------------- 文案

    def summary(self) -> str:
        parts = [self.text or "天气未知"]
        if self.temp is not None:
            parts[0] = f"{parts[0]} {self.temp:.0f}°C"
            if self.feels_like is not None and abs(self.feels_like - self.temp) >= 3:
                parts[0] += f"（体感 {self.feels_like:.0f}°C）"
        if self.temp_min is not None and self.temp_max is not None:
            parts.append(f"今天 {self.temp_min:.0f}~{self.temp_max:.0f}°C")
        if self.pop is not None:
            parts.append(f"降水概率 {self.pop}%")
        if self.wind:
            parts.append(self.wind)
        return "·".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "city": self.city,
            "text": self.text,
            "temp": self.temp,
            "feels_like": self.feels_like,
            "temp_min": self.temp_min,
            "temp_max": self.temp_max,
            "pop": self.pop,
            "summary": self.summary(),
            "wet": self.is_wet(),
            "clear": self.is_clear(),
            "obs_time": self.obs_time,
        }


def parse_weather(now_payload: Any, daily_payload: Any, city: str) -> Weather | None:
    """解析 /v7/weather/now 与 /v7/weather/3d 的响应。"""
    if not isinstance(now_payload, dict) or str(now_payload.get("code")) != "200":
        return None
    now = now_payload.get("now")
    if not isinstance(now, dict):
        return None

    weather = Weather(
        city=city,
        text=str(now.get("text") or "").strip(),
        temp=_num(now.get("temp")),
        feels_like=_num(now.get("feelsLike")),
        humidity=int(_num(now.get("humidity")) or 0) or None,
        wind=str(now.get("windDir") or "").strip(),
        obs_time=str(now.get("obsTime") or "").strip(),
    )

    if isinstance(daily_payload, dict) and str(daily_payload.get("code")) == "200":
        daily = daily_payload.get("daily")
        if isinstance(daily, list) and daily and isinstance(daily[0], dict):
            today = daily[0]
            weather.temp_min = _num(today.get("tempMin"))
            weather.temp_max = _num(today.get("tempMax"))
            pop = _num(today.get("pop"))
            weather.pop = int(pop) if pop is not None else None
    return weather


def build_url(host: str, path: str, location: str, key: str) -> str:
    host = (host or DEFAULT_HOST).strip().strip("/")
    if not host.startswith("http"):
        host = f"https://{host}"
    query = urllib.parse.urlencode({"location": location, "key": key, "lang": "zh"})
    return f"{host}{path}?{query}"


def decode_body(raw: bytes) -> dict[str, Any]:
    """和风的 CDN 有时会直接返回 gzip（0x1f 0x8b），这里统一处理。"""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8", errors="replace"))


def _http_json(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return decode_body(response.read())


async def _get(url: str, timeout: float) -> dict[str, Any]:
    return await asyncio.to_thread(_http_json, url, timeout)


async def fetch_weather(
    host: str,
    key: str,
    location: str,
    city: str,
    timeout: float = 8.0,
    logger: Any = None,
) -> Weather | None:
    if not key:
        return None

    async def get(path: str) -> dict[str, Any]:
        url = build_url(host, path, location, key)
        try:
            return await _get(url, timeout)
        except Exception as exc:  # noqa: BLE001 - 天气失败不该影响主流程
            if logger is not None:
                logger.warning(f"[virtual_activity] 天气请求失败 {path}: {exc}")
            return {}

    now_payload = await get("/v7/weather/now")
    if str(now_payload.get("code")) != "200" and logger is not None:
        logger.warning(f"[virtual_activity] 天气接口返回 {now_payload.get('code')}: {now_payload.get('message') or ''}")
    daily_payload = await get("/v7/weather/3d")
    return parse_weather(now_payload, daily_payload, city)


class WeatherService:
    """带缓存的门面：30 分钟内不重复请求，拿不到就返回上次的或 None。"""

    def __init__(
        self,
        config: Callable[[str, Any], Any],
        logger: Any = None,
        clock: Callable[[], datetime] | None = None,
        fetch: Callable[[str, str, str, str, float, Any], Any] | None = None,
    ) -> None:
        self._cfg = config
        self._logger = logger
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._fetch = fetch or fetch_weather
        self._cached: Weather | None = None
        self._cached_at: datetime | None = None

    def enabled(self) -> bool:
        return bool(self._cfg("weather_enabled", True))

    def last(self) -> Weather | None:
        if not self.enabled():
            return None
        return self._cached

    def _ttl(self) -> timedelta:
        try:
            minutes = max(1, int(self._cfg("weather_cache_minutes", 30)))
        except (TypeError, ValueError):
            minutes = 30
        return timedelta(minutes=minutes)

    async def get(self, force: bool = False) -> Weather | None:
        if not self.enabled():
            return None
        key = str(self._cfg("weather_key", "") or "").strip()
        if not key:
            return self._cached

        now = self._clock()
        if (
            not force
            and self._cached_at is not None
            and now - self._cached_at < self._ttl()
        ):
            return self._cached

        host = str(self._cfg("weather_api_host", DEFAULT_HOST) or DEFAULT_HOST)
        location = str(self._cfg("weather_location_id", DEFAULT_LOCATION_ID) or DEFAULT_LOCATION_ID)
        city = str(self._cfg("weather_city", DEFAULT_CITY) or DEFAULT_CITY)
        try:
            timeout = float(self._cfg("weather_timeout_sec", 8))
        except (TypeError, ValueError):
            timeout = 8.0

        weather = await self._fetch(host, key, location, city, timeout, self._logger)
        if weather is None:
            # 失败也记一次时间，避免 key 配错时每次聊天都去撞接口
            self._cached_at = now
            return self._cached
        weather.fetched_at = now
        self._cached = weather
        self._cached_at = now
        return weather
