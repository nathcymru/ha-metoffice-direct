"""Independent review regressions for freshness and warning ambiguity."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from homeassistant.components.http import KEY_HASS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.http import KEY_AUTHENTICATED, request_handler_factory
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from test_homeassistant import coordinator

import custom_components.metoffice_direct as integration
from custom_components.metoffice_direct.maps import MapImageView
from custom_components.metoffice_direct.model import geometry_contains, warning_summary
from custom_components.metoffice_direct.weather import DirectWeather

NOW = datetime(2026, 12, 1, 18, tzinfo=UTC)


def warning(geometry=None, areas=None):
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "warningId": "synthetic-warning",
            "warningLevel": "amber",
            "validFromDate": (NOW - timedelta(hours=1)).isoformat(),
            "validToDate": (NOW + timedelta(hours=1)).isoformat(),
            "affectedAreas": areas if areas is not None else [{"regionName": "Wales"}],
        },
    }


@pytest.mark.parametrize(
    "regions,areas",
    [([], None), ([" "], None), (["Wales"], []), (["Wales"], [{"regionName": None}])],
)
def test_unlocatable_warning_cannot_become_confident_none(regions, areas):
    data = {"type": "FeatureCollection", "features": [warning(areas=areas)]}
    with pytest.raises(ValueError, match="could not be evaluated"):
        warning_summary(data, NOW, 53, -3, regions)


def test_meaningful_region_fallback_distinguishes_match_and_outside():
    data = {"type": "FeatureCollection", "features": [warning()]}
    assert warning_summary(data, NOW, 53, -3, [" Wales "])["active_level"] == "amber"
    assert warning_summary(data, NOW, 53, -3, ["Scotland"])["active_level"] == "none"


@pytest.mark.parametrize("properties", [None, "invalid", []])
def test_malformed_warning_properties_raise_a_validation_error(properties):
    with pytest.raises(ValueError, match="could not be evaluated"):
        warning_summary(
            {"type": "FeatureCollection", "features": [{"properties": properties}]},
            NOW,
            53,
            -3,
            ["Wales"],
        )


def test_invalid_geographic_coordinates_cannot_prove_home_outside():
    invalid = {"type": "Polygon", "coordinates": [[[200, 50], [210, 50], [210, 60], [200, 50]]]}
    assert geometry_contains(invalid, -3, 53) is None
    degenerate = {"type": "Polygon", "coordinates": [[[-3, 53], [-3, 53], [-3, 53], [-3, 53]]]}
    assert geometry_contains(degenerate, -3, 53) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [timedelta(hours=6), timedelta(hours=7), timedelta(minutes=-1)])
async def test_daily_action_rejects_stale_or_future_model_even_after_fresh_fetch(
    tmp_path, monkeypatch, age
):
    hass = HomeAssistant(str(tmp_path))
    try:
        monkeypatch.setattr("custom_components.metoffice_direct.weather.utcnow", lambda: NOW)
        hourly = coordinator(hass)
        daily = SimpleNamespace(
            data={
                "fetched_at": NOW.isoformat(),
                "model_run": (NOW - age).isoformat(),
                "rows": [{"time": NOW.isoformat(), "dayMaxScreenTemperature": 8}],
            },
            fresh=True,
        )
        weather = DirectWeather({"hourly": hourly, "daily": daily})
        weather.hass = hass
        assert await weather.async_forecast_daily() is None
        daily.data["model_run"] = (NOW - timedelta(hours=1)).isoformat()
        assert (await weather.async_forecast_daily())[0]["native_temperature"] == 8
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "units,temperature,wind,pressure",
    [(METRIC_SYSTEM, 0, 0, 1013), (US_CUSTOMARY_SYSTEM, 32, 0, 29.91)],
)
async def test_real_ha_native_conversions_preserve_zero_and_omit_missing_forecast_fields(
    tmp_path, monkeypatch, units, temperature, wind, pressure
):
    hass = HomeAssistant(str(tmp_path))
    hass.config.units = units
    try:
        monkeypatch.setattr("custom_components.metoffice_direct.weather.utcnow", lambda: NOW)
        monkeypatch.setattr("custom_components.metoffice_direct.entity.utcnow", lambda: NOW)
        monkeypatch.setattr("custom_components.metoffice_direct.coordinator.utcnow", lambda: NOW)
        hourly, daily = coordinator(hass), coordinator(hass, "daily")
        hourly.data = {
            "fetched_at": NOW.isoformat(),
            "model_run": NOW.isoformat(),
            "rows": [
                {
                    "time": NOW.isoformat(),
                    "screenTemperature": 0,
                    "windSpeed10m": 0,
                    "mslp": 101300,
                    "screenRelativeHumidity": None,
                    "totalPrecipAmount": 0,
                }
            ],
        }
        weather = DirectWeather({"hourly": hourly, "daily": daily})
        weather.hass = hass
        attributes = weather.state_attributes
        assert attributes["temperature"] == temperature
        assert attributes["wind_speed"] == wind
        assert attributes["pressure"] == pytest.approx(pressure, abs=0.01)
        converted = weather._convert_forecast(await weather.async_forecast_hourly())[0]
        assert converted["temperature"] == temperature
        assert converted["wind_speed"] == 0
        assert converted["precipitation"] == 0
        assert "humidity" not in converted
        assert "dew_point" not in converted
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_unload_withdraws_map_access_without_fetching_provider(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        image_id = "a" * 64
        client = SimpleNamespace(get=AsyncMock())
        maps = SimpleNamespace(fresh=True, data={"frames": [{"id": image_id}]}, directory=tmp_path)
        runtime = SimpleNamespace(client=client, coordinators={"map_images": maps})
        hass.data["metoffice_direct"] = {"runtime": runtime}
        hass.config_entries = SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True))
        request = make_mocked_request("GET", f"/api/metoffice_direct/map/{image_id}")
        request.app[KEY_HASS] = hass
        assert MapImageView.requires_auth is True
        assert isinstance(await MapImageView().get(request, image_id), web.FileResponse)
        assert await integration.async_unload_entry(hass, Mock()) is True
        with pytest.raises(web.HTTPNotFound):
            await MapImageView().get(request, image_id)
        client.get.assert_not_called()
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_real_ha_map_handler_rejects_anonymous_and_unknown_frames(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        image_id = "a" * 64
        maps = SimpleNamespace(fresh=True, data={"frames": [{"id": image_id}]}, directory=tmp_path)
        hass.data["metoffice_direct"] = {
            "runtime": SimpleNamespace(coordinators={"map_images": maps})
        }
        view = MapImageView()
        handler = request_handler_factory(hass, view, view.get)
        request = make_mocked_request(
            "GET", f"/api/metoffice_direct/map/{image_id}", match_info={"frame_id": image_id}
        )
        request.app[KEY_HASS] = hass
        with pytest.raises(web.HTTPUnauthorized):
            await handler(request)

        request[KEY_AUTHENTICATED] = True
        response = await handler(request)
        assert isinstance(response, web.FileResponse)
        assert response.headers["Cache-Control"].startswith("private")
        assert response.headers["X-Content-Type-Options"] == "nosniff"

        maps.data = {"frames": []}
        with pytest.raises(web.HTTPNotFound):
            await handler(request)
    finally:
        await hass.async_stop()
