"""Bounded, authenticated playback of one ordered precipitation PNG layer."""

import hashlib
import json
import logging
import math
import os
import re
import struct
import zlib
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from aiohttp import web
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ProviderError, utcnow
from .const import ATTRIBUTION, DOMAIN, INTERVALS, MAX_AGE
from .model import timestamp

_LOGGER = logging.getLogger(__name__)
FRAME_ID = re.compile(r"[0-9a-f]{64}")
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAP_VALIDATION_VERSION = 2
# Exact physical parameter, plus the approved order UI's surface-qualified ID.
# Any other provider identifier needs an explicit capability review.
PRECIPITATION_RATE_IDS = frozenset(
    {
        "total_precipitation_rate",
        "single_total_precipitation_rate_ground_0",
    }
)


def png_valid(content):
    if (
        len(content) < 24
        or len(content) > MAX_FRAME_BYTES
        or content[:8] != b"\x89PNG\r\n\x1a\n"
        or content[12:16] != b"IHDR"
    ):
        return False
    width, height = struct.unpack(">II", content[16:24])
    if not (0 < width <= 8192 and 0 < height <= 8192 and width * height <= 16_777_216):
        return False
    offset, has_image_data = 8, False
    while offset + 12 <= len(content):
        length = struct.unpack(">I", content[offset : offset + 4])[0]
        end = offset + 12 + length
        if end > len(content):
            return False
        kind = content[offset + 4 : offset + 8]
        if struct.unpack(">I", content[end - 4 : end])[0] != zlib.crc32(
            content[offset + 4 : end - 4]
        ):
            return False
        has_image_data |= kind == b"IDAT" and length > 0
        if kind == b"IEND":
            return length == 0 and end == len(content) and has_image_data
        offset = end
    return False


def frame_plan(payload, order_id, now):
    """Use advertised file timestamps; there is no invented WMS TIME parameter."""
    files = payload.get("orderDetails", {}).get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        raise ValueError("Map order has no file list")
    first = now.replace(minute=0, second=0, microsecond=0)
    wanted = [first + timedelta(hours=i) for i in range(25)]
    groups = {}
    parameters = set()
    for file in files:
        if not isinstance(file, dict):
            raise ValueError("Invalid map file metadata")
        names = file.get("parameters", [])
        if not isinstance(names, list) or len(names) != 1 or not isinstance(names[0], str):
            raise ValueError("Map order must contain exactly one total precipitation rate layer")
        parameter = re.sub(r"[-\s]+", "_", names[0].strip().lower())
        if parameter not in PRECIPITATION_RATE_IDS:
            raise ValueError("Map parameter is not an approved total precipitation rate identifier")
        parameters.add(parameter)
        run = timestamp(file.get("runDateTime"))
        steps = file.get("timesteps", [])
        file_id = file.get("fileId")
        if (
            not run
            or not isinstance(file_id, str)
            or not file_id
            or not isinstance(steps, list)
            or len(steps) != 1
        ):
            raise ValueError("Each map PNG must advertise one run and one timestep")
        try:
            hours = float(steps[0])
        except ValueError, TypeError:
            raise ValueError("Invalid map timestep") from None
        if not math.isfinite(hours) or not 0 <= hours <= 168:
            raise ValueError("Invalid map forecast horizon")
        valid = run + timedelta(hours=hours)
        if valid not in wanted or run > now or now - run > timedelta(hours=30):
            continue
        region = file.get("region")
        if (
            not isinstance(region, dict)
            or not isinstance(region.get("name"), str)
            or not region["name"].strip()
        ):
            raise ValueError("Map frame is missing region metadata")
        geography = json.dumps(region, sort_keys=True, separators=(",", ":"), allow_nan=False)
        identifier = hashlib.sha256(
            f"{order_id}\0{run.isoformat()}\0{file_id}\0{parameter}\0{geography}\0land=true;legend=true".encode()
        ).hexdigest()
        frame = {
            "id": identifier,
            "file_id": file_id,
            "valid_time": valid.isoformat(),
            "model_run": run.isoformat(),
            "region": region,
        }
        by_time = groups.setdefault(run, {})
        if valid in by_time and by_time[valid]["id"] != identifier:
            raise ValueError("Map order has ambiguous frames for one valid time")
        by_time[valid] = frame
    if len(parameters) != 1:
        raise ValueError("Map order must contain one consistent precipitation parameter")
    for run in sorted(groups, reverse=True):
        if all(time in groups[run] for time in wanted):
            selected = [groups[run][time] for time in wanted]
            if any(frame["region"] != selected[0]["region"] for frame in selected[1:]):
                raise ValueError("Selected map frames have inconsistent region or extent metadata")
            return selected
    raise ValueError("A complete rolling 24-hour hourly map is not available yet")


class MapCoordinator(DataUpdateCoordinator):
    kind = "map_images"

    def __init__(self, hass, entry, client, config):
        super().__init__(
            hass,
            _LOGGER,
            name="Met Office Direct forecast map",
            config_entry=entry,
            update_interval=INTERVALS[self.kind],
        )
        self.client, self.config = client, config
        self.data = client.store.cache("map_images")
        self.error_code = None
        self.directory = Path(hass.config.path(".storage", "metoffice_direct_maps"))
        self.sizes = {}

    def _scan(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        sizes = {}
        for path in self.directory.iterdir():
            if path.suffix == ".tmp" and FRAME_ID.fullmatch(path.stem):
                path.unlink(missing_ok=True)
                continue
            if (
                path.suffix == ".png"
                and FRAME_ID.fullmatch(path.stem)
                and path.is_file()
                and not path.is_symlink()
            ):
                size = path.stat().st_size
                with path.open("rb") as file:
                    content = file.read(MAX_FRAME_BYTES + 1)
                if 0 < size <= MAX_FRAME_BYTES and png_valid(content):
                    sizes[path.stem] = size
                else:
                    path.unlink(missing_ok=True)
        # The directory is exclusively integration-owned. Bound leftovers on reload.
        if sum(sizes.values()) > MAX_CACHE_BYTES:
            keep = {frame["id"] for frame in (self.data or {}).get("frames", [])}
            for identifier in list(sizes):
                if identifier not in keep:
                    (self.directory / f"{identifier}.png").unlink(missing_ok=True)
                    sizes.pop(identifier)
            if sum(sizes.values()) > MAX_CACHE_BYTES:
                raise ProviderError("Map cache exceeds its size limit")
        return sizes

    async def load_cache(self):
        self.sizes = await self.hass.async_add_executor_job(self._scan)

    @property
    def fresh(self):
        data = self.data or {}
        fetched, run = timestamp(data.get("fetched_at")), timestamp(data.get("model_run"))
        frames = data.get("frames", [])
        return bool(
            self.last_update_success
            and data.get("validation_version") == MAP_VALIDATION_VERSION
            and fetched
            and run
            and frames
            and timedelta(0) <= utcnow() - fetched < MAX_AGE[self.kind]
            and timedelta(0) <= utcnow() - run < timedelta(hours=30)
            and all(frame["id"] in self.sizes for frame in frames)
            and timestamp(frames[-1]["valid_time"]) > utcnow()
        )

    def _write(self, identifier, content):
        path = self.directory / f"{identifier}.png"
        temporary = self.directory / f"{identifier}.tmp"
        try:
            with open(temporary, "wb") as file:
                os.chmod(temporary, 0o600)
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    async def _remove(self, identifiers):
        def remove():
            for identifier in identifiers:
                (self.directory / f"{identifier}.png").unlink(missing_ok=True)

        await self.hass.async_add_executor_job(remove)
        for identifier in identifiers:
            self.sizes.pop(identifier, None)

    async def _async_update_data(self):
        try:
            order = self.config.get("map_order_id")
            if not order:
                raise ProviderError("map_images: select a precipitation map_order_id in YAML")
            cached = self.client.store.cache("map_images")
            fetched = timestamp((cached or {}).get("fetched_at"))
            if (
                fetched
                and timedelta(0) <= utcnow() - fetched < INTERVALS[self.kind]
                and cached.get("order_id") == order
                and cached.get("validation_version") == MAP_VALIDATION_VERSION
                and all(frame["id"] in self.sizes for frame in cached.get("frames", []))
            ):
                self.error_code = None
                return cached
            attempted = timestamp(self.client.store.cache("attempt_map_images"))
            if attempted and timedelta(0) <= utcnow() - attempted < INTERVALS[self.kind]:
                raise ProviderError("map_images: scheduled retry pending")
            await self.client.store.save_cache("attempt_map_images", utcnow().isoformat())
            path = "/orders/" + quote(order, safe="") + "/latest"
            metadata = await self.client.get("map_images", path)
            plan_time = utcnow()
            frames = frame_plan(metadata, order, plan_time)
            keep = {frame["id"] for frame in frames}
            downloaded = set()
            for frame in frames:
                identifier = frame["id"]
                if identifier in self.sizes:
                    continue
                content = await self.client.get(
                    "map_images",
                    path + "/" + quote(frame["file_id"], safe="") + "/data",
                    params={"includeLand": "true", "legend": "true"},
                    binary=True,
                )
                if not png_valid(content):
                    raise ProviderError("map_images: received an invalid or oversized PNG")
                if sum(self.sizes.values()) + len(content) > MAX_CACHE_BYTES:
                    await self._remove(set(self.sizes) - keep)
                if sum(self.sizes.values()) + len(content) > MAX_CACHE_BYTES:
                    raise ProviderError("map_images: selected frames exceed the 64 MiB cache bound")
                await self.hass.async_add_executor_job(self._write, identifier, content)
                self.sizes[identifier] = len(content)
                downloaded.add(identifier)
            # Latest can change while downloading: do not mix images from two runs.
            if downloaded:
                verification = await self.client.get("map_images", path)
                if {frame["id"] for frame in frame_plan(verification, order, plan_time)} != keep:
                    await self._remove(downloaded)
                    raise ProviderError(
                        "map_images: provider frame metadata changed during download"
                    )
            region = frames[0].get("region")
            data = {
                "validation_version": MAP_VALIDATION_VERSION,
                "frames": [
                    {key: value for key, value in frame.items() if key not in ("file_id", "region")}
                    for frame in frames
                ],
                "fetched_at": utcnow().isoformat(),
                "model_run": frames[0]["model_run"],
                "coverage_start": frames[0]["valid_time"],
                "coverage_end": frames[-1]["valid_time"],
                "order_id": order,
                "source": "Met Office forecast",
                "attribution": ATTRIBUTION,
                "region": region,
                "bounds": (region or {}).get("extent") if isinstance(region, dict) else None,
            }
            await self.client.store.save_cache("map_images", data)
            await self._remove(set(self.sizes) - keep)
            self.error_code = None
            return data
        except (ProviderError, ValueError, TypeError, KeyError, OSError) as err:
            self.error_code = (
                str(err)
                if isinstance(err, ProviderError)
                else "map_images: invalid map metadata or local cache failure"
            )
            raise UpdateFailed(self.error_code) from err


class MapImageView(HomeAssistantView):
    url = "/api/metoffice_direct/map/{frame_id}"
    name = "api:metoffice_direct:map"
    requires_auth = True

    async def get(self, request, frame_id):
        if not FRAME_ID.fullmatch(frame_id):
            raise web.HTTPNotFound()
        runtime = request.app[KEY_HASS].data.get(DOMAIN, {}).get("runtime")
        coordinator = runtime.coordinators.get("map_images") if runtime else None
        if (
            not coordinator
            or not coordinator.fresh
            or frame_id not in {frame["id"] for frame in (coordinator.data or {}).get("frames", [])}
        ):
            raise web.HTTPNotFound()
        return web.FileResponse(
            coordinator.directory / f"{frame_id}.png",
            headers={
                "Content-Type": "image/png",
                "Cache-Control": "private, max-age=3600",
                "X-Content-Type-Options": "nosniff",
            },
        )
