"""Official symbols retain distinctions hidden by Home Assistant conditions."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.metoffice_direct.model import daily_forecasts, hourly_forecasts, weather_code

NOW = datetime(2026, 10, 28, 8, tzinfo=UTC)


@pytest.mark.parametrize("code", [None, True, False, "1", 4, -1, 31, 1.5, float("nan")])
def test_unsupported_symbol_is_unknown(code):
    assert weather_code(code) is None


def test_drizzle_rain_and_day_night_showers_keep_exact_codes():
    rows = [
        {
            "time": NOW.replace(hour=8 + index).isoformat(),
            "screenTemperature": 10,
            "significantWeatherCode": code,
        }
        for index, code in enumerate([9, 10, 11, 12])
    ]
    result = hourly_forecasts({"rows": rows}, NOW)
    assert [row["condition"] for row in result] == ["rainy"] * 4
    assert [row["metoffice_weather_code"] for row in result] == [9, 10, 11, 12]


def test_daily_keeps_distinct_day_and_night_symbols_without_inventing_missing():
    data = {
        "rows": [
            {
                "time": NOW.isoformat(),
                "dayMaxScreenTemperature": 10,
                "daySignificantWeatherCode": 3,
                "nightSignificantWeatherCode": 0,
            }
        ]
    }
    result = daily_forecasts(data, NOW, ZoneInfo("Europe/London"))[0]
    assert result["metoffice_weather_code"] == 3
    assert result["metoffice_night_weather_code"] == 0
    data["rows"][0]["nightSignificantWeatherCode"] = 4
    result = daily_forecasts(data, NOW, ZoneInfo("Europe/London"))[0]
    assert "metoffice_night_weather_code" not in result
