"""Direct Met Office DataHub access with YAML-only credentials."""

import asyncio
from dataclasses import dataclass

import probatio as vol
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.reload import async_integration_yaml_config

from .api import Client, StateStore
from .const import DOMAIN, ROOTS
from .coordinator import ProviderCoordinator
from .maps import MapCoordinator, MapImageView

PLATFORMS = [Platform.WEATHER, Platform.SENSOR, Platform.BINARY_SENSOR]
CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(DOMAIN): vol.Schema(
            {
                vol.Required("global_spot_key"): cv.string,
                **{
                    vol.Optional(f"{product}_key"): cv.string
                    for product in ROOTS
                    if product != "global_spot"
                },
                vol.Optional("latitude"): cv.latitude,
                vol.Optional("longitude"): cv.longitude,
                vol.Optional("warning_regions", default=[]): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional("map_order_id"): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


@dataclass
class Runtime:
    client: Client
    coordinators: dict[str, ProviderCoordinator | MapCoordinator]


async def async_setup(hass, config):
    data = hass.data.setdefault(DOMAIN, {})
    if not data.get("view_registered"):
        hass.http.register_view(MapImageView())
        data["view_registered"] = True
    if DOMAIN in config:
        # Only non-secret metadata enters the config-entry import flow.
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}, data={"name": "Met Office Direct"}
            )
        )
    return True


async def async_setup_entry(hass, entry: ConfigEntry):
    yaml = await async_integration_yaml_config(hass, DOMAIN)
    if not yaml or DOMAIN not in yaml:
        raise ConfigEntryError(
            "Configure metoffice_direct in configuration.yaml using !secret references"
        )
    config = dict(yaml[DOMAIN])
    config.setdefault("latitude", hass.config.latitude)
    config.setdefault("longitude", hass.config.longitude)
    keys = {
        product: config.pop(f"{product}_key") for product in ROOTS if config.get(f"{product}_key")
    }
    if not keys.get("global_spot"):
        raise ConfigEntryError("The Global Spot API secret must be configured")
    store = StateStore(hass)
    await store.load()
    location = [config["latitude"], config["longitude"], config["warning_regions"]]
    if store.cache("configured_location") != location:
        # Preserve request counters across a location change, discard old location data.
        store.data["cache"] = {}
        await store.save_cache("configured_location", location)
    client = Client(async_get_clientsession(hass), store, keys)
    kinds = ["hourly", "daily", "three_hourly"]
    kinds.extend(product for product in ("warnings", "observations", "bpf") if product in keys)
    coordinators = {kind: ProviderCoordinator(hass, entry, client, kind, config) for kind in kinds}
    if "map_images" in keys:
        maps = MapCoordinator(hass, entry, client, config)
        await maps.load_cache()
        coordinators["map_images"] = maps
    entry.runtime_data = Runtime(client, coordinators)
    # A failed optional API does not disable working forecasts or other products.
    # async_refresh retains unavailable entities for honest status and later recovery.
    try:
        await asyncio.gather(
            *(coordinator.async_refresh() for coordinator in coordinators.values())
        )
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await asyncio.gather(
            *(coordinator.async_shutdown() for coordinator in coordinators.values())
        )
        raise
    hass.data.setdefault(DOMAIN, {})["runtime"] = entry.runtime_data
    return True


async def async_unload_entry(hass, entry):
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop("runtime", None)
    return unloaded
