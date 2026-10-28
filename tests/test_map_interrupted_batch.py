"""Unpublished map images cannot survive a failed batch or process restart."""

from collections import Counter
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant
from test_homeassistant import entry
from test_maps import PNG, detailed_file, thin_manifest

from custom_components.metoffice_direct import maps
from custom_components.metoffice_direct.api import Client, ProviderError, StateStore, utcnow
from custom_components.metoffice_direct.maps import MapCoordinator


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["binary", "anchor"])
@pytest.mark.parametrize("restart", [False, True])
async def test_failed_batch_is_discarded_before_retry_or_restart(
    tmp_path, monkeypatch, failure, restart
):
    hass = HomeAssistant(str(tmp_path))
    now = utcnow().replace(hour=6, minute=15, second=0, microsecond=0)
    monkeypatch.setattr(maps, "utcnow", lambda: now)
    binary_calls = 0
    metadata_calls = Counter()
    fail_once = True
    try:
        client = Client(Mock(), StateStore(hass), {"map_images": "SYNTHETIC_SECRET"})

        async def get(product, path, **kwargs):
            nonlocal binary_calls, fail_once
            if kwargs.get("binary"):
                binary_calls += 1
                if fail_once and failure == "binary" and binary_calls == 3:
                    fail_once = False
                    raise ProviderError("map_images: synthetic interrupted PNG download")
                return PNG
            if path.endswith("/latest"):
                return thin_manifest(now)
            metadata_calls[path] += 1
            if fail_once and failure == "anchor" and metadata_calls[path] == 2:
                fail_once = False
                raise ProviderError("map_images: synthetic anchor verification failure")
            return detailed_file(now, path.rsplit("/", 1)[-1])

        client.get = AsyncMock(side_effect=get)
        config = {"map_order_id": "synthetic-order"}
        coordinator = MapCoordinator(hass, entry(), client, config)
        await coordinator.load_cache()
        await coordinator.async_refresh()

        assert not fail_once  # The intended failure occurred after images were written.
        assert not coordinator.fresh
        assert client.store.cache("map_images") is None
        assert not coordinator.sizes
        assert not list(coordinator.directory.glob("*.png"))

        if restart:
            # A hard process exit can skip finally. Orphan bytes with a valid opaque
            # filename must still be rejected because no manifest published them.
            orphan = coordinator.directory / ("f" * 64 + ".png")
            orphan.write_bytes(PNG)
            store = StateStore(hass)
            await store.load()
            client.store = store
            coordinator = MapCoordinator(hass, entry(), client, config)
            await coordinator.load_cache()
            assert not coordinator.sizes
            assert not orphan.exists()

        now += timedelta(hours=1, seconds=1)
        before_retry = binary_calls
        await coordinator.async_refresh()

        assert coordinator.fresh
        assert binary_calls - before_retry == 25
        assert len(coordinator.sizes) == 25
        assert len(list(coordinator.directory.glob("*.png"))) == 25
        assert len(client.store.cache("map_images")["frames"]) == 25
    finally:
        await hass.async_stop()
