"""Real HA 2026.10 classes, synthetic provider transport and timestamps."""

from datetime import timedelta
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import reload as reload_helper
from homeassistant.util.unit_system import METRIC_SYSTEM

import custom_components.metoffice_direct as integration
from custom_components.metoffice_direct.api import Client, StateStore, utcnow
from custom_components.metoffice_direct.binary_sensor import WarningBinary
from custom_components.metoffice_direct.const import POINT_FIELDS
from custom_components.metoffice_direct.coordinator import ProviderCoordinator
from custom_components.metoffice_direct.sensor import ValueSensor, WarningSensor
from custom_components.metoffice_direct.weather import DirectWeather


def entry():
    return ConfigEntry(
        version=1,
        minor_version=1,
        domain="metoffice_direct",
        title="Met Office Direct",
        data={},
        options={},
        source="import",
        unique_id="metoffice_direct",
        discovery_keys=MappingProxyType({}),
        subentries_data=None,
    )


def coordinator(hass, kind="hourly"):
    client = Client(Mock(), StateStore(hass), {"global_spot": "SYNTHETIC_SECRET"})
    return ProviderCoordinator(
        hass, entry(), client, kind, {"latitude": 53, "longitude": -3, "warning_regions": ["Wales"]}
    )


@pytest.mark.asyncio
async def test_real_weather_conversion_preserves_rh_and_dew_point(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    hass.config.units = METRIC_SYSTEM
    try:
        hourly, daily = coordinator(hass), coordinator(hass, "daily")
        now = utcnow()
        hourly.data = {
            "fetched_at": now.isoformat(),
            "model_run": (now - timedelta(minutes=30)).isoformat(),
            "rows": [
                {
                    "time": now.replace(minute=0, second=0, microsecond=0).isoformat(),
                    "screenTemperature": 3,
                    "screenRelativeHumidity": 90,
                    "screenDewPointTemperature": 1.5,
                    "windSpeed10m": 2,
                    "significantWeatherCode": 0,
                }
            ],
        }
        weather = DirectWeather({"hourly": hourly, "daily": daily})
        weather.hass = hass
        assert weather.entity_id == "weather.metoffice_direct"
        assert weather.available
        assert weather.condition == "clear-night"
        attrs = weather.state_attributes
        assert attrs["wind_speed"] == pytest.approx(7.2)
        assert attrs["wind_speed_unit"] == "km/h"
        converted = weather._convert_forecast(await weather.async_forecast_hourly())[0]
        assert converted["humidity"] == 90
        assert converted["dew_point"] == 1.5
        assert converted["wind_speed"] == pytest.approx(7.2)
        assert weather.extra_state_attributes["current_time"] == hourly.data["rows"][0]["time"]
        assert weather.extra_state_attributes["model_run"] == hourly.data["model_run"]
        hourly.data["model_run"] = (now - timedelta(hours=7)).isoformat()
        assert not weather.available
        assert await weather.async_forecast_hourly() is None
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_all_point_sensor_classes_construct_and_missing_value_is_unavailable(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        coord = coordinator(hass)
        coord.data = {
            "fetched_at": utcnow().isoformat(),
            "model_run": utcnow().isoformat(),
            "rows": [],
        }
        sensors = [ValueSensor(coord, key, info) for key, info in POINT_FIELDS.items()]
        assert all(sensor.entity_registry_enabled_default for sensor in sensors)
        assert not any(sensor.available for sensor in sensors)
        assert len({sensor.entity_id for sensor in sensors}) == len(sensors)
        assert (
            next(s.entity_id for s in sensors if s.key == "uvIndex")
            == "sensor.metoffice_direct_uv_index"
        )
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_recent_persistent_cache_prevents_restart_refresh_burst(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        coord = coordinator(hass)
        cached = {"fetched_at": utcnow().isoformat(), "rows": [{"time": utcnow().isoformat()}]}
        await coord.client.store.save_cache("hourly", cached)
        coord.client.get = AsyncMock()
        await coord.async_refresh()
        assert coord.data == cached
        coord.client.get.assert_not_called()
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_warning_unavailable_is_never_no_warning(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        coord = coordinator(hass, "warnings")
        binary = WarningBinary(coord, "active", "Warning active")
        level = WarningSensor(coord, "level", "Warning level")
        coord.data = {
            "fetched_at": utcnow().isoformat(),
            "payload": {"type": "FeatureCollection", "features": []},
        }
        assert binary.available and binary.is_on is False
        assert level.native_value == "none"
        coord.last_update_success = False
        assert not binary.available
        assert binary.is_on is None
        assert level.native_value is None
        assert level.extra_state_attributes["warnings"] is None
        coord.last_update_success = True
        coord.data["fetched_at"] = (utcnow() - timedelta(minutes=46)).isoformat()
        assert not binary.available
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_import_flow_payload_never_contains_secrets(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        hass.config_entries = ConfigEntries(hass, {})
        hass.http = Mock()
        hass.config_entries.flow.async_init = AsyncMock()
        await integration.async_setup(
            hass, {"metoffice_direct": {"global_spot_key": "SYNTHETIC_SECRET"}}
        )
        await hass.async_block_till_done()
        call = hass.config_entries.flow.async_init.call_args
        assert call.kwargs["data"] == {"name": "Met Office Direct"}
        assert "SYNTHETIC_SECRET" not in str(call)
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_real_yaml_secrets_and_setup_entry_shape_keep_keys_runtime_only(
    tmp_path, monkeypatch
):
    hass = HomeAssistant(str(tmp_path))
    try:
        (tmp_path / "configuration.yaml").write_text(
            "metoffice_direct:\n  global_spot_key: !secret direct_spot\n"
        )
        (tmp_path / "secrets.yaml").write_text("direct_spot: SYNTHETIC_SECRET\n")
        component = SimpleNamespace(
            domain="metoffice_direct",
            documentation="https://example.invalid/",
            async_get_component=AsyncMock(return_value=integration),
            platforms_exists=Mock(return_value=False),
        )
        monkeypatch.setattr(
            reload_helper, "async_get_integration", AsyncMock(return_value=component)
        )
        hass.config_entries = ConfigEntries(hass, {})
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        now = utcnow()
        payload = {
            "features": [
                {
                    "properties": {
                        "modelRunDate": now.isoformat(),
                        "timeSeries": [{"time": now.isoformat(), "screenTemperature": 3}],
                    }
                }
            ]
        }
        monkeypatch.setattr(Client, "get", AsyncMock(return_value=payload))
        monkeypatch.setattr(integration, "async_get_clientsession", Mock(return_value=Mock()))
        configured = entry()
        assert await integration.async_setup_entry(hass, configured)
        assert configured.data == {}
        assert configured.runtime_data.client.keys == {"global_spot": "SYNTHETIC_SECRET"}
        assert set(configured.runtime_data.coordinators) == {"hourly", "daily", "three_hourly"}
        assert all(
            not any("key" in key for key in coordinator.config)
            for coordinator in configured.runtime_data.coordinators.values()
        )
        assert (
            "SYNTHETIC_SECRET"
            not in (tmp_path / ".storage" / "metoffice_direct_runtime.json").read_text()
        )
        hass.config_entries.async_forward_entry_setups.assert_awaited_once()
    finally:
        await hass.async_stop()
