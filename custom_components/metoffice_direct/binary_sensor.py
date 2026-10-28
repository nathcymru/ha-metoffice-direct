"""Official warning presence with unavailable distinct from no warnings."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .entity import ProviderEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data.coordinators.get("warnings")
    if coordinator:
        async_add_entities(
            [
                WarningBinary(coordinator, key, name)
                for key, name in (("active", "Warning active"), ("upcoming", "Warning upcoming"))
            ]
        )


class WarningBinary(ProviderEntity, BinarySensorEntity):
    _entity_domain = "binary_sensor"
    _attr_device_class = BinarySensorDeviceClass.SAFETY

    def __init__(self, coordinator, key, name):
        super().__init__(coordinator, key, name)
        self.key = key
        self.entity_id = f"binary_sensor.metoffice_direct_warning_{key}"

    @property
    def is_on(self):
        summary = self.coordinator.warnings()
        return bool(summary[self.key]) if summary is not None else None

    @property
    def extra_state_attributes(self):
        summary = self.coordinator.warnings()
        return {
            **super().extra_state_attributes,
            "warnings": summary[self.key] if summary else None,
        }
