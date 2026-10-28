"""Point, station and warning sensors with explicit source and missing values."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory

from .api import utcnow
from .const import BPF_FIELDS, DAILY_LIMITS, INTERVALS, MAX_AGE, OBSERVATION_FIELDS, POINT_FIELDS
from .entity import ProviderEntity
from .model import condition, number, timestamp


async def async_setup_entry(hass, entry, async_add_entities):
    coordinators = entry.runtime_data.coordinators
    entities = []
    for kind, fields in (
        ("hourly", POINT_FIELDS),
        ("observations", OBSERVATION_FIELDS),
        ("bpf", BPF_FIELDS),
    ):
        if kind in coordinators:
            coordinator = coordinators[kind]
            entities.extend(
                ValueSensor(coordinator, key, details) for key, details in fields.items()
            )
            if kind != "bpf":
                entities.append(TextSensor(coordinator, "condition", "Condition"))
            entities.append(TimeSensor(coordinator, "current_time", "Data time"))
            if kind == "observations":
                entities.extend(
                    TextSensor(coordinator, key, name)
                    for key, name in (
                        ("wind_direction", "Wind direction"),
                        ("pressure_tendency", "Pressure tendency"),
                    )
                )
    for coordinator in coordinators.values():
        entities.append(StatusSensor(coordinator))
        entities.append(TimeSensor(coordinator, "fetched_at", "Last successful update"))
        if coordinator.kind in ("hourly", "daily", "three_hourly"):
            entities.append(TimeSensor(coordinator, "model_run", "Model run"))
    if "warnings" in coordinators:
        entities.extend(
            WarningSensor(coordinators["warnings"], key, name)
            for key, name in (
                ("count", "Warning count"),
                ("active_count", "Active warning count"),
                ("level", "Warning level"),
                ("active_level", "Active warning level"),
            )
        )
    hourly = coordinators["hourly"]
    if "map_images" in coordinators:
        entities.append(MapSensor(coordinators["map_images"]))
    entities.extend(BudgetSensor(hourly, product) for product in entry.runtime_data.client.keys)
    async_add_entities(entities)


class ValueSensor(ProviderEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, key, details):
        super().__init__(coordinator, key, details[0])
        self.key = key
        self._attr_native_unit_of_measurement = details[1]
        self._attr_device_class = SensorDeviceClass(details[2]) if details[2] else None
        if self._attr_device_class == SensorDeviceClass.WIND_DIRECTION:
            self._attr_state_class = SensorStateClass.MEASUREMENT_ANGLE

    @property
    def native_value(self):
        return number((self.current() or {}).get(self.key))

    @property
    def available(self):
        return super().available and self.native_value is not None


class TextSensor(ProviderEntity, SensorEntity):
    def __init__(self, coordinator, key, name):
        super().__init__(coordinator, key, name)
        self.key = key

    @property
    def native_value(self):
        row = self.current() or {}
        if self.key == "condition":
            return condition(
                row.get(
                    "weather_code"
                    if self.coordinator.kind == "observations"
                    else "significantWeatherCode"
                )
            )
        value = row.get(self.key)
        return str(value)[:255] if value is not None else None

    @property
    def available(self):
        return super().available and self.native_value is not None


class TimeSensor(ProviderEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, key, name):
        super().__init__(coordinator, key, name)
        self.key = key

    @property
    def native_value(self):
        return timestamp(self.extra_state_attributes.get(self.key))


class WarningSensor(ProviderEntity, SensorEntity):
    def __init__(self, coordinator, key, name):
        super().__init__(coordinator, key, name)
        self.key = key
        self.entity_id = (
            "sensor.metoffice_direct_"
            + {
                "count": "warning_count",
                "active_count": "active_warning_count",
                "level": "warning_level",
                "active_level": "active_warning_level",
            }[key]
        )
        if "count" in key:
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        summary = self.coordinator.warnings()
        if summary is None:
            return None
        if self.key == "count":
            return len(summary["warnings"])
        if self.key == "active_count":
            return len(summary["active"])
        return summary[self.key]

    @property
    def extra_state_attributes(self):
        summary = self.coordinator.warnings()
        return {
            **super().extra_state_attributes,
            "warnings": summary["warnings"] if summary else None,
            "stale_after_seconds": int(MAX_AGE["warnings"].total_seconds()),
            "poll_interval_seconds": int(INTERVALS["warnings"].total_seconds()),
            "filter_policy": "Home polygon; configured region fallback only when geometry is unavailable",
        }


class StatusSensor(ProviderEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator, "status", "API status")

    @property
    def available(self):
        return True

    @property
    def native_value(self):
        return "available" if self.coordinator.fresh else "unavailable"

    @property
    def extra_state_attributes(self):
        return {
            **super().extra_state_attributes,
            "last_error": self.coordinator.error_code,
            "refresh_interval_seconds": self.coordinator.update_interval.total_seconds(),
        }


class BudgetSensor(ProviderEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, product):
        super().__init__(
            coordinator,
            product + "_requests",
            product.replace("_", " ").title() + " requests today",
        )
        self.product = product

    @property
    def available(self):
        return True

    @property
    def native_value(self):
        budget = self.coordinator.client.store.data["budgets"].get(self.product, {})
        return budget.get("count", 0) if budget.get("day") == utcnow().date().isoformat() else 0

    @property
    def extra_state_attributes(self):
        budget = self.coordinator.client.store.data["budgets"].get(self.product, {})
        blocked = budget.get("blocked_until", 0)
        return {
            "local_daily_ceiling": DAILY_LIMITS[self.product],
            "count_scope": "Requests made by this installation, UTC day",
            "provider_backoff_active": blocked > utcnow().timestamp(),
            "product_configured": True,
            "automatic_downloads": self.product != "atmospheric"
            and (self.product != "map_images" or bool(self.coordinator.config.get("map_order_id"))),
        }


class MapSensor(ProviderEntity, SensorEntity):
    _attr_icon = "mdi:weather-rainy"

    def __init__(self, coordinator):
        super().__init__(coordinator, "forecast_map", "Forecast map")
        self.entity_id = "sensor.metoffice_direct_forecast_map"

    @property
    def native_value(self):
        return "ready" if self.coordinator.fresh else None

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        return {
            key: data.get(key)
            for key in (
                "frames",
                "fetched_at",
                "model_run",
                "coverage_start",
                "coverage_end",
                "source",
                "attribution",
                "region",
                "bounds",
            )
        }
