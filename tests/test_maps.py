"""Synthetic map manifests and PNG bytes; no provider sample images."""

import struct
import zlib
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from homeassistant.components.http import KEY_HASS
from homeassistant.core import HomeAssistant
from test_homeassistant import entry

from custom_components.metoffice_direct.api import Client, StateStore, utcnow
from custom_components.metoffice_direct.maps import (
    MapCoordinator,
    MapImageView,
    frame_plan,
    png_valid,
)


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


PNG = (
    b"\x89PNG\r\n\x1a\n"
    + chunk(b"IHDR", struct.pack(">IIBBBBB", 10, 10, 8, 6, 0, 0, 0))
    + chunk(b"IDAT", zlib.compress(b"\0" * 410))
    + chunk(b"IEND", b"")
)


def manifest(now, offset=0):
    run = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=offset)
    return {
        "orderDetails": {
            "files": [
                {
                    "fileId": f"rain_{hour}",
                    "parameters": ["total_precipitation_rate"],
                    "runDateTime": run.isoformat(),
                    "timesteps": [str(hour)],
                    "region": {
                        "name": "Synthetic region",
                        "extent": {
                            "x": {
                                "label": "longitude",
                                "lowerBound": "-10",
                                "upperBound": "0",
                                "uomLabel": "degree",
                            },
                            "y": {
                                "label": "latitude",
                                "lowerBound": "50",
                                "upperBound": "60",
                                "uomLabel": "degree",
                            },
                        },
                    },
                }
                for hour in range(49)
            ]
        }
    }


def test_map_plan_picks_25_frames_with_hash_ids_and_no_false_temporal_precision():
    now = utcnow()
    frames = frame_plan(manifest(now), "synthetic-order", now)
    assert len(frames) == 25
    assert frames[0]["valid_time"] == now.replace(minute=0, second=0, microsecond=0).isoformat()
    assert (
        frames[-1]["valid_time"]
        == (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=24)).isoformat()
    )
    assert len({frame["id"] for frame in frames}) == 25
    assert all(len(frame["id"]) == 64 for frame in frames)
    bad = manifest(now)
    bad["orderDetails"]["files"][0]["timesteps"] = ["0", "1"]
    with pytest.raises(ValueError, match="one timestep"):
        frame_plan(bad, "synthetic-order", now)


def test_map_plan_rejects_partial_window_and_non_precipitation_order():
    now = utcnow()
    partial = manifest(now)
    partial["orderDetails"]["files"] = partial["orderDetails"]["files"][:2]
    with pytest.raises(ValueError, match="complete"):
        frame_plan(partial, "synthetic-order", now)
    other = manifest(now)
    other["orderDetails"]["files"][0]["parameters"] = ["temperature"]
    with pytest.raises(ValueError, match="precipitation"):
        frame_plan(other, "synthetic-order", now)
    assert png_valid(PNG)
    assert not png_valid(b"<html>provider error</html>")
    assert not png_valid(PNG[:16] + struct.pack(">II", 99999, 10))


@pytest.mark.parametrize(
    "parameter",
    [
        "rainbow_temperature",
        "total_precipitation_amount",
        "precipitation_probability",
        "not_total_precipitation_rate",
        "single_total_precipitation_rate_ground_0_extra",
    ],
)
def test_map_parameter_allowlist_rejects_lookalikes_and_other_precipitation_metrics(parameter):
    now = utcnow()
    data = manifest(now)
    for frame in data["orderDetails"]["files"]:
        frame["parameters"] = [parameter]
    with pytest.raises(ValueError, match="approved total precipitation rate"):
        frame_plan(data, "synthetic-order", now)


@pytest.mark.parametrize(
    "parameter",
    [
        "total_precipitation_rate",
        "Total-Precipitation-Rate",
        "single_total_precipitation_rate_ground_0",
    ],
)
def test_map_parameter_allowlist_accepts_exact_normalized_rate_identifiers(parameter):
    now = utcnow()
    data = manifest(now)
    for frame in data["orderDetails"]["files"]:
        frame["parameters"] = [parameter]
    assert len(frame_plan(data, "synthetic-order", now)) == 25


@pytest.mark.parametrize("change", ["name", "extent", "missing_extent", "missing_region"])
def test_map_plan_rejects_geographic_changes_within_selected_frames(change):
    now = utcnow()
    data = manifest(now)
    frame = data["orderDetails"]["files"][now.hour + 1]
    if change == "name":
        frame["region"]["name"] = "Different region"
    elif change == "extent":
        frame["region"]["extent"]["x"]["upperBound"] = "5"
    elif change == "missing_extent":
        frame["region"].pop("extent")
    else:
        frame.pop("region")
    with pytest.raises(ValueError, match="region|extent"):
        frame_plan(data, "synthetic-order", now)


def test_map_geographic_change_across_refresh_changes_frame_ids_and_prevents_stale_image_reuse():
    now = utcnow()
    data = manifest(now)
    changed = deepcopy(data)
    for frame in changed["orderDetails"]["files"]:
        frame["region"]["extent"]["x"]["upperBound"] = "5"
    old_ids = {frame["id"] for frame in frame_plan(data, "synthetic-order", now)}
    new_ids = {frame["id"] for frame in frame_plan(changed, "synthetic-order", now)}
    assert old_ids.isdisjoint(new_ids)


@pytest.mark.asyncio
async def test_map_cache_deduplicates_downloads_across_restart_and_serves_authenticated_only(
    tmp_path,
):
    hass = HomeAssistant(str(tmp_path))
    try:
        now = utcnow()
        payload = manifest(now)
        client = Client(Mock(), StateStore(hass), {"map_images": "SYNTHETIC_SECRET"})

        async def get(product, path, **kwargs):
            return PNG if kwargs.get("binary") else payload

        client.get = AsyncMock(side_effect=get)
        config = {"map_order_id": "synthetic-order"}
        coord = MapCoordinator(hass, entry(), client, config)
        await coord.load_cache()
        await coord.async_refresh()
        assert coord.fresh
        assert sum(call.kwargs.get("binary", False) for call in client.get.call_args_list) == 25
        assert client.get.call_count == 27  # Manifest before and after for run consistency.
        restarted = MapCoordinator(hass, entry(), client, config)
        await restarted.load_cache()
        await restarted.async_refresh()
        assert restarted.fresh
        assert client.get.call_count == 27
        assert len(list(coord.directory.glob("*.png"))) == 25
        hass.data["metoffice_direct"] = {
            "runtime": SimpleNamespace(coordinators={"map_images": restarted})
        }
        request = make_mocked_request("GET", "/api/metoffice_direct/map/unused")
        request.app[KEY_HASS] = hass
        identifier = coord.data["frames"][0]["id"]
        response = await MapImageView().get(request, identifier)
        assert isinstance(response, web.FileResponse)
        assert response.headers["Content-Type"] == "image/png"
        assert MapImageView.requires_auth is True
        restarted.data["validation_version"] = 1
        assert not restarted.fresh
        with pytest.raises(web.HTTPNotFound):
            await MapImageView().get(request, identifier)
        restarted.data["validation_version"] = 2
        with pytest.raises(web.HTTPNotFound):
            await MapImageView().get(request, "../secrets.yaml")
        with pytest.raises(web.HTTPNotFound):
            await MapImageView().get(request, "0" * 64)
        restarted.last_update_success = False
        with pytest.raises(web.HTTPNotFound):
            await MapImageView().get(request, identifier)
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_unconfigured_map_order_makes_no_requests(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        client = Client(Mock(), StateStore(hass), {"map_images": "SYNTHETIC_SECRET"})
        client.get = AsyncMock()
        coord = MapCoordinator(hass, entry(), client, {})
        await coord.load_cache()
        await coord.async_refresh()
        assert not coord.fresh
        assert "map_order_id" in coord.error_code
        client.get.assert_not_called()
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_model_run_race_never_publishes_mixed_frames(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    try:
        now = utcnow().replace(hour=18, minute=30)
        from custom_components.metoffice_direct import maps

        monkeypatch.setattr(maps, "utcnow", lambda: now)
        client = Client(Mock(), StateStore(hass), {"map_images": "SYNTHETIC_SECRET"})
        metadata_calls = 0

        async def get(product, path, **kwargs):
            nonlocal metadata_calls
            if kwargs.get("binary"):
                return PNG
            metadata_calls += 1
            return manifest(now, 0 if metadata_calls == 1 else 12)

        client.get = AsyncMock(side_effect=get)
        coord = MapCoordinator(hass, entry(), client, {"map_order_id": "synthetic-order"})
        await coord.load_cache()
        await coord.async_refresh()
        assert not coord.fresh
        assert "changed during download" in coord.error_code
        assert not list(coord.directory.glob("*.png"))
        assert client.store.cache("map_images") is None
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_map_cache_size_is_enforced_before_disk_write(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    try:
        from custom_components.metoffice_direct import maps

        monkeypatch.setattr(maps, "MAX_CACHE_BYTES", len(PNG) * 2)
        client = Client(Mock(), StateStore(hass), {"map_images": "SYNTHETIC_SECRET"})

        async def get(product, path, **kwargs):
            return PNG if kwargs.get("binary") else manifest(utcnow())

        client.get = AsyncMock(side_effect=get)
        coord = MapCoordinator(hass, entry(), client, {"map_order_id": "synthetic-order"})
        await coord.load_cache()
        await coord.async_refresh()
        assert not coord.fresh
        assert sum(path.stat().st_size for path in coord.directory.glob("*.png")) <= len(PNG) * 2
        assert client.store.cache("map_images") is None
    finally:
        await hass.async_stop()
