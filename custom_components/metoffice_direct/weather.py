"""HA weather backed by the point forecast, never by a distant station."""

from datetime import timedelta
from zoneinfo import ZoneInfo

from homeassistant.components.weather import WeatherEntity, WeatherEntityFeature
from homeassistant.const import (
    UnitOfLength,
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import callback

from .api import utcnow
from .entity import ProviderEntity
from .model import condition, daily_forecasts, hourly_forecasts, number, timestamp


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DirectWeather(entry.runtime_data.coordinators)])


class DirectWeather(ProviderEntity, WeatherEntity):
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_pressure_unit = UnitOfPressure.PA
    _attr_native_wind_speed_unit = UnitOfSpeed.METERS_PER_SECOND
    _attr_native_visibility_unit = UnitOfLength.METERS
    _attr_native_precipitation_unit = UnitOfPrecipitationDepth.MILLIMETERS
    _attr_supported_features = (
        WeatherEntityFeature.FORECAST_HOURLY | WeatherEntityFeature.FORECAST_DAILY
    )

    def __init__(self, coordinators):
        super().__init__(coordinators["hourly"], "weather", None)
        self.entity_id = "weather.metoffice_direct"
        self.daily = coordinators["daily"]

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self.daily.async_add_listener(self._handle_coordinator_update))

    @callback
    def _handle_coordinator_update(self):
        self.async_write_ha_state()
        self.hass.async_create_task(self.async_update_listeners(None))

    @property
    def available(self):
        run = timestamp((self.coordinator.data or {}).get("model_run"))
        return bool(
            super().available
            and self.current()
            and run
            and timedelta(0) <= utcnow() - run < timedelta(hours=6)
        )

    def value(self, key):
        return number((self.current() or {}).get(key))

    @property
    def condition(self):
        return condition((self.current() or {}).get("significantWeatherCode"))

    @property
    def native_temperature(self):
        return self.value("screenTemperature")

    @property
    def native_apparent_temperature(self):
        return self.value("feelsLikeTemperature")

    @property
    def native_dew_point(self):
        return self.value("screenDewPointTemperature")

    @property
    def humidity(self):
        return self.value("screenRelativeHumidity")

    @property
    def native_wind_speed(self):
        return self.value("windSpeed10m")

    @property
    def native_wind_gust_speed(self):
        return self.value("windGustSpeed10m")

    @property
    def wind_bearing(self):
        return self.value("windDirectionFrom10m")

    @property
    def native_pressure(self):
        return self.value("mslp")

    @property
    def native_visibility(self):
        return self.value("visibility")

    @property
    def uv_index(self):
        return self.value("uvIndex")

    async def async_forecast_hourly(self):
        return hourly_forecasts(self.coordinator.data, utcnow()) if self.available else None

    async def async_forecast_daily(self):
        now = utcnow()
        run = timestamp((self.daily.data or {}).get("model_run"))
        if not self.daily.fresh or not run or not timedelta(0) <= now - run < timedelta(hours=6):
            return None
        return daily_forecasts(self.daily.data, now, ZoneInfo(self.hass.config.time_zone))

    @property
    def extra_state_attributes(self):
        return {
            **super().extra_state_attributes,
            "source": "Global Spot point forecast",
            "station_observation": False,
        }
