"""天气模块测试：解析、判定、缓存，不联网。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from astrbot_plugin_virtual_activity.core.weather import (
    Weather,
    WeatherService,
    build_url,
    parse_weather,
)

NOW_PAYLOAD = {
    "code": "200",
    "now": {
        "obsTime": "2026-10-27T13:00+08:00",
        "temp": "12",
        "feelsLike": "9",
        "text": "小雨",
        "windDir": "东北风",
        "humidity": "80",
    },
}

DAILY_PAYLOAD = {
    "code": "200",
    "daily": [
        {
            "fxDate": "2026-10-27",
            "tempMax": "15",
            "tempMin": "9",
            "textDay": "小雨",
            "pop": "70",
        }
    ],
}


class TestParsing(unittest.TestCase):
    def test_parse_full(self):
        weather = parse_weather(NOW_PAYLOAD, DAILY_PAYLOAD, "重庆")
        self.assertIsNotNone(weather)
        self.assertEqual(weather.city, "重庆")
        self.assertEqual(weather.text, "小雨")
        self.assertEqual(weather.temp, 12)
        self.assertEqual(weather.temp_min, 9)
        self.assertEqual(weather.temp_max, 15)
        self.assertEqual(weather.pop, 70)
        self.assertTrue(weather.is_wet())
        self.assertFalse(weather.is_clear())

    def test_summary(self):
        weather = parse_weather(NOW_PAYLOAD, DAILY_PAYLOAD, "重庆")
        summary = weather.summary()
        self.assertIn("小雨 12°C", summary)
        self.assertIn("体感 9°C", summary)
        self.assertIn("9~15°C", summary)
        self.assertIn("70%", summary)

    def test_parse_error_code(self):
        self.assertIsNone(parse_weather({"code": "401"}, {}, "重庆"))
        self.assertIsNone(parse_weather({}, {}, "重庆"))

    def test_weather_without_daily(self):
        weather = parse_weather(NOW_PAYLOAD, {"code": "500"}, "重庆")
        self.assertIsNotNone(weather)
        self.assertIsNone(weather.temp_max)

    def test_clear_and_hot(self):
        weather = Weather(city="重庆", text="晴", temp=35)
        self.assertTrue(weather.is_clear())
        self.assertTrue(weather.is_hot())
        self.assertFalse(weather.is_wet())

    def test_build_url(self):
        url = build_url("devapi.qweather.com", "/v7/weather/now", "101040100", "abc")
        self.assertTrue(url.startswith("https://devapi.qweather.com/v7/weather/now?"))
        self.assertIn("location=101040100", url)
        self.assertIn("key=abc", url)

    def test_decode_gzip_body(self):
        import gzip
        import json

        from astrbot_plugin_virtual_activity.core.weather import decode_body

        raw = gzip.compress(json.dumps(NOW_PAYLOAD, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(raw[:2], b"\x1f\x8b")
        self.assertEqual(decode_body(raw)["now"]["text"], "小雨")

    def test_decode_plain_body(self):
        import json

        from astrbot_plugin_virtual_activity.core.weather import decode_body

        raw = json.dumps(NOW_PAYLOAD, ensure_ascii=False).encode("utf-8")
        self.assertEqual(decode_body(raw)["code"], "200")


class FakeClock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


class FakeFetch:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, host, key, location, city, timeout, logger):
        self.calls += 1
        return Weather(city=city, text="多云", temp=20)


class TestService(unittest.IsolatedAsyncioTestCase):
    def make(self, cfg=None):
        config = {"weather_enabled": True, "weather_key": "k", "weather_cache_minutes": 30}
        config.update(cfg or {})
        self.clock = FakeClock(datetime(2026, 10, 27, 12, 0).astimezone())
        self.fetch = FakeFetch()
        return WeatherService(
            config=lambda key, default=None: config.get(key, default),
            clock=self.clock,
            fetch=self.fetch,
        )

    async def test_cache_hit(self):
        service = self.make()
        first = await service.get()
        second = await service.get()
        self.assertIs(first, second)
        self.assertEqual(self.fetch.calls, 1)
        self.assertIsNotNone(service.last())

    async def test_cache_expires(self):
        service = self.make()
        await service.get()
        self.clock.moment += timedelta(minutes=31)
        await service.get()
        self.assertEqual(self.fetch.calls, 2)

    async def test_force_refresh(self):
        service = self.make()
        await service.get()
        await service.get(force=True)
        self.assertEqual(self.fetch.calls, 2)

    async def test_no_key_skips(self):
        service = self.make({"weather_key": ""})
        self.assertIsNone(await service.get())
        self.assertEqual(self.fetch.calls, 0)

    async def test_disabled(self):
        service = self.make({"weather_enabled": False})
        self.assertIsNone(await service.get())
        self.assertEqual(self.fetch.calls, 0)

    async def test_failure_keeps_last(self):
        service = self.make()
        await service.get()
        good = service.last()

        async def broken(*args, **kwargs):
            self.fetch.calls += 1
            return None

        service._fetch = broken
        self.clock.moment += timedelta(minutes=31)
        self.assertIs(await service.get(), good)

    async def test_failure_backs_off(self):
        service = self.make()

        async def broken(*args, **kwargs):
            self.fetch.calls += 1
            return None

        service._fetch = broken
        self.assertIsNone(await service.get())
        await service.get()
        self.assertEqual(self.fetch.calls, 1)  # 失败后 30 分钟内不再重试
        self.clock.moment += timedelta(minutes=31)
        await service.get()
        self.assertEqual(self.fetch.calls, 2)


class TestWeatherBoost(unittest.TestCase):
    def setUp(self):
        from astrbot_plugin_virtual_activity.core.engine import VirtualRoomEngine

        self.boost = VirtualRoomEngine.weather_boost

    def test_rain_boosts_window(self):
        window = {"name": "窗边", "effects": [{"name": "看窗外"}]}
        bed = {"name": "床", "effects": [{"name": "睡觉"}]}
        rain = Weather(city="重庆", text="小雨", temp=12)
        self.assertGreater(self.boost(window, rain), 1.0)
        self.assertEqual(self.boost(bed, rain), 1.0)

    def test_hot_boosts_fridge(self):
        fridge = {"name": "冰箱", "effects": [{"name": "翻冰箱"}]}
        hot = Weather(city="重庆", text="晴", temp=36)
        self.assertGreater(self.boost(fridge, hot), 1.0)

    def test_no_weather_is_neutral(self):
        window = {"name": "窗边", "effects": [{"name": "看窗外"}]}
        self.assertEqual(self.boost(window, None), 1.0)


if __name__ == "__main__":
    unittest.main()
