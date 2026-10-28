"""Shared entity provenance and time-dependent availability."""

from datetime import timedelta

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .api import utcnow
from .const import ATTRIBUTION, DOMAIN
from .model import current_row, timestamp


class ProviderEntity(CoordinatorEntity):
    _entity_domain = "sensor"
    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator, key, name):
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{coordinator.kind}_{key}"
        self._attr_name = name
        prefix = {"hourly": "", "observations": "station_", "bpf": "probabilistic_"}.get(
            coordinator.kind, coordinator.kind + "_"
        )
        self.entity_id = (
            f"{self._entity_domain}.metoffice_direct_{prefix}{slugify(name or 'weather')}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.kind)},
            manufacturer="Met Office",
            name={
                "hourly": "Met Office Direct",
                "observations": "Met Office station",
                "warnings": "Met Office warnings",
                "bpf": "Met Office probabilistic",
                "daily": "Met Office daily",
                "three_hourly": "Met Office extended",
                "map_images": "Met Office forecast map",
            }[coordinator.kind],
            model="DataHub " + coordinator.kind.replace("_", " "),
            configuration_url="https://datahub.metoffice.gov.uk/",
        )

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(async_track_time_interval(self.hass, self._tick, timedelta(minutes=1)))

    @callback
    def _tick(self, now):
        self.async_write_ha_state()

    @property
    def available(self):
        if self.coordinator.kind in ("hourly", "daily", "three_hourly"):
            run = timestamp((self.coordinator.data or {}).get("model_run"))
            if not run or not timedelta(0) <= utcnow() - run < timedelta(hours=6):
                return False
        return self.coordinator.fresh

    def current(self):
        observed = self.coordinator.kind == "observations"
        return current_row(
            self.coordinator.data,
            utcnow(),
            key="datetime" if observed else "time",
            maximum_age=timedelta(hours=3 if observed else 2),
        )

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        attrs = {"fetched_at": data.get("fetched_at"), "data_product": self.coordinator.kind}
        row = self.current()
        if row:
            attrs["current_time"] = row.get("time", row.get("datetime"))
        if self.coordinator.kind == "observations":
            attrs["station"] = data.get("station")
        else:
            attrs["model_run"] = data.get("model_run", data.get("instance"))
        return attrs
