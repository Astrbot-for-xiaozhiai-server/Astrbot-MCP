"""Key-free public APIs: weather, time and currency exchange."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.fastmcp import FastMCP

from ..context import AppContext
from ..utils import json_dumps

# WMO weather interpretation codes used by Open-Meteo.
_WEATHER_CODES: dict[int, str] = {
    0: "晴",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "小阵雨",
    81: "中阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴小冰雹",
    99: "雷阵雨伴大冰雹",
}

_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


async def _get_json(
    ctx: AppContext,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> Any:
    async with ctx.http_client(timeout=timeout) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool()
    async def get_weather(location: str, days: int = 3) -> str:
        """查询城市天气（Open-Meteo，无需 API Key）。

        Args:
            location: 城市名称，例如 北京、Shanghai、Tokyo。
            days: 预报天数，1-16。
        """
        forecast_days = max(1, min(int(days), 16))
        language = ctx.config.services.public_apis.weather_language or "zh"

        geo = await _get_json(
            ctx,
            "https://geocoding-api.open-meteo.com/v1/search",
            params={
                "name": location,
                "count": 1,
                "language": language,
                "format": "json",
            },
        )
        places = geo.get("results") or []
        if not places:
            raise ValueError(f"Cannot find a location named {location!r}")
        place = places[0]

        forecast = await _get_json(
            ctx,
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": (
                    "temperature_2m,relative_humidity_2m,apparent_temperature,"
                    "is_day,precipitation,weather_code,wind_speed_10m"
                ),
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_sum,precipitation_probability_max,"
                    "sunrise,sunset"
                ),
                "timezone": "auto",
                "forecast_days": forecast_days,
                "temperature_unit": "celsius",
                "wind_speed_unit": "kmh",
                "precipitation_unit": "mm",
            },
        )

        current_raw = forecast.get("current", {})
        current = {
            "time": current_raw.get("time"),
            "condition": _WEATHER_CODES.get(current_raw.get("weather_code"), "未知"),
            "weather_code": current_raw.get("weather_code"),
            "temperature_c": current_raw.get("temperature_2m"),
            "apparent_temperature_c": current_raw.get("apparent_temperature"),
            "humidity_percent": current_raw.get("relative_humidity_2m"),
            "precipitation_mm": current_raw.get("precipitation"),
            "wind_speed_kmh": current_raw.get("wind_speed_10m"),
            "is_day": bool(current_raw.get("is_day")),
        }

        daily_raw = forecast.get("daily", {})
        daily: list[dict[str, Any]] = []
        dates = daily_raw.get("time", [])
        for index, date_value in enumerate(dates):
            code = _sequence_get(daily_raw.get("weather_code"), index)
            daily.append(
                {
                    "date": date_value,
                    "condition": _WEATHER_CODES.get(code, "未知"),
                    "temperature_max_c": _sequence_get(
                        daily_raw.get("temperature_2m_max"), index
                    ),
                    "temperature_min_c": _sequence_get(
                        daily_raw.get("temperature_2m_min"), index
                    ),
                    "precipitation_mm": _sequence_get(
                        daily_raw.get("precipitation_sum"), index
                    ),
                    "precipitation_probability_percent": _sequence_get(
                        daily_raw.get("precipitation_probability_max"), index
                    ),
                    "sunrise": _sequence_get(daily_raw.get("sunrise"), index),
                    "sunset": _sequence_get(daily_raw.get("sunset"), index),
                }
            )

        return json_dumps(
            {
                "location": {
                    "name": place.get("name"),
                    "admin1": place.get("admin1"),
                    "country": place.get("country"),
                    "latitude": place.get("latitude"),
                    "longitude": place.get("longitude"),
                    "timezone": forecast.get("timezone"),
                },
                "current": current,
                "daily": daily,
            }
        )

    @mcp.tool()
    async def get_time(timezone: str = "Asia/Shanghai") -> str:
        """获取指定时区的当前时间。

        Args:
            timezone: IANA 时区名，例如 Asia/Shanghai、UTC、America/New_York。
        """
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown timezone: {timezone!r}") from exc

        now = datetime.now(zone)
        offset = now.utcoffset()
        offset_hours = offset.total_seconds() / 3600 if offset else 0.0
        return json_dumps(
            {
                "timezone": timezone,
                "iso": now.isoformat(),
                "date": now.strftime("%Y-%m-%d"),
                "time": now.strftime("%H:%M:%S"),
                "weekday": _WEEKDAYS[now.weekday()],
                "utc_offset_hours": offset_hours,
                "unix": int(now.timestamp()),
            }
        )

    @mcp.tool()
    async def convert_currency(
        amount: float, from_currency: str, to_currency: str
    ) -> str:
        """按实时汇率换算货币（open.er-api.com，无需 API Key）。

        Args:
            amount: 金额。
            from_currency: 源货币代码，例如 USD、CNY、EUR。
            to_currency: 目标货币代码，例如 JPY、GBP。
        """
        base = from_currency.strip().upper()
        target = to_currency.strip().upper()
        if len(base) != 3 or len(target) != 3:
            raise ValueError("Currency codes must be 3 letters, e.g. USD or CNY")

        data = await _get_json(ctx, f"https://open.er-api.com/v6/latest/{base}")
        if data.get("result") != "success":
            raise ValueError(
                f"Exchange rate service error: {data.get('error-type', 'unknown')}"
            )

        rates = data.get("rates", {})
        if target not in rates:
            raise ValueError(f"Unsupported target currency: {target}")
        rate = float(rates[target])
        return json_dumps(
            {
                "from": base,
                "to": target,
                "amount": amount,
                "rate": rate,
                "converted": round(float(amount) * rate, 4),
                "updated": data.get("time_last_update_utc"),
                "source": "open.er-api.com",
            }
        )


def _sequence_get(values: Any, index: int) -> Any:
    if isinstance(values, list) and index < len(values):
        return values[index]
    return None
