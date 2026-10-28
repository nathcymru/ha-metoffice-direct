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
MAP_VALIDATION_VERSION = 3
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


def map_candidates(payload, now):
    """Choose advertised dated files for inspection; metadata must confirm every hint.

    The current API catalog also advertises moving +00/+12 aliases. Never download
    those: retain the immutable run spelling and validate actual times separately.
    """
    files = payload.get("orderDetails", {}).get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        raise ValueError("Map order has no file list")
    first = now.replace(minute=0, second=0, microsecond=0)
    wanted = [first + timedelta(hours=index) for index in range(25)]
    groups = {}
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Invalid map catalog record")
        identifier = item.get("fileId")
        if not isinstance(identifier, str):
            raise ValueError("Map catalog record has no file ID")
        match = re.fullmatch(r"total_precipitation_rate_ts(\d{1,3})_(\d{10}|\+\d{2})", identifier)
        if not match:
            raise ValueError("Map catalog contains an unsupported layer or file ID")
        run = timestamp(item.get("runDateTime"))
        if not run or run.minute or run.second or run.microsecond:
            raise ValueError("Map catalog has an invalid run time")
        step, suffix = int(match[1]), match[2]
        if step > 168 or str(item.get("run")) != run.strftime("%H"):
            raise ValueError("Map catalog run and forecast step are inconsistent")
        if suffix.startswith("+"):
            if suffix[1:] != run.strftime("%H"):
                raise ValueError("Map moving alias disagrees with its run")
            continue
        if suffix != run.strftime("%Y%m%d%H"):
            raise ValueError("Map dated file ID disagrees with its run")
        expected = run + timedelta(hours=step)
        if expected not in wanted or run > now or now - run > timedelta(hours=30):
            continue
        by_time = groups.setdefault(run, {})
        candidate = {
            "fileId": identifier,
            "runDateTime": run.isoformat(),
            "expected_time": expected.isoformat(),
        }
        if expected in by_time and by_time[expected] != candidate:
            raise ValueError("Map catalog has ambiguous dated files")
        by_time[expected] = candidate
    for run in sorted(groups, reverse=True):
        if all(valid in groups[run] for valid in wanted):
            return [groups[run][valid] for valid in wanted]
    raise ValueError("A complete dated rolling 24-hour map is not advertised yet")


def validate_map_file(payload, candidate):
    """Resolve one catalog hint against its authenticated file metadata."""
    file = payload.get("fileDetails", {}).get("file") if isinstance(payload, dict) else None
    if not isinstance(file, dict) or file.get("fileId") != candidate["fileId"]:
        raise ValueError("Map metadata does not identify the requested dated file")
    run = timestamp(file.get("runDateTime"))
    if run != timestamp(candidate["runDateTime"]) or str(file.get("run")) != run.strftime("%H"):
        raise ValueError("Map metadata run does not match the catalog")
    match = re.fullmatch(r"total_precipitation_rate_ts(\d{1,3})_(\d{10})", file["fileId"])
    if not match or match[2] != run.strftime("%Y%m%d%H"):
        raise ValueError("Map metadata has an inconsistent dated identifier")
    step = int(match[1])
    allowed = file.get("timesteps")
    if not isinstance(allowed, list) or not allowed or len(allowed) > 169:
        raise ValueError("Map metadata has no bounded timestep set")
    try:
        steps = [float(value) for value in allowed if not isinstance(value, bool)]
    except TypeError, ValueError:
        raise ValueError("Invalid map metadata timesteps") from None
    if (
        len(steps) != len(allowed)
        or len(set(steps)) != len(steps)
        or any(not math.isfinite(value) or not 0 <= value <= 168 for value in steps)
        or step not in steps
    ):
        raise ValueError("Map dated timestep is not in the advertised set")
    if run + timedelta(hours=step) != timestamp(candidate["expected_time"]):
        raise ValueError("Map valid time disagrees with the catalog")
    parameters = file.get("parameters")
    if parameters != ["total_precipitation_rate"]:
        raise ValueError("Map metadata is not total precipitation rate")
    region = file.get("region")
    if (
        not isinstance(region, dict)
        or not isinstance(region.get("name"), str)
        or not region["name"].strip()
    ):
        raise ValueError("Map metadata has no region")
    extent = region.get("extent")
    if not isinstance(extent, dict):
        raise ValueError("Map metadata has no geographic extent")
    for axis, limit, direction in (("x", 180, "east"), ("y", 90, "north")):
        value = extent.get(axis)
        if not isinstance(value, dict):
            raise ValueError("Map metadata has an incomplete geographic extent")
        try:
            lower, upper = float(value["lowerBound"]), float(value["upperBound"])
        except KeyError, TypeError, ValueError:
            raise ValueError("Map geographic bounds must be numeric") from None
        unit = str(value.get("uomLabel", "")).lower()
        if not -limit <= lower < upper <= limit or unit not in (
            "degree",
            "degrees",
            "deg",
            "°",
            "degrees_" + direction,
            "degree_" + direction,
        ):
            raise ValueError("Map geographic extent is not valid longitude/latitude degrees")
    # The API supplies the whole order's allowed timestep set, even for one PNG.
    # Pin it to the independently validated tsN member before planning frames.
    return {**file, "timesteps": [str(step)]}


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
        trusted = {
            frame["id"]
            for frame in (self.data or {}).get("frames", [])
            if (self.data or {}).get("validation_version") == MAP_VALIDATION_VERSION
        }
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
                if path.stem in trusted and 0 < size <= MAX_FRAME_BYTES and png_valid(content):
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

        for identifier in identifiers:
            self.sizes.pop(identifier, None)
        await self.hass.async_add_executor_job(remove)

    async def _file_metadata(self, listing, path, now):
        candidates = map_candidates(listing, now)
        cache = self.client.store.cache("map_file_metadata") or {}
        prior = (
            cache.get("files", {}) if cache.get("order_id") == self.config["map_order_id"] else {}
        )
        selected, updated = [], {}
        anchor = None
        for candidate in candidates:
            identifier = candidate["fileId"]
            record = prior.get(identifier, {})
            saved = timestamp(record.get("fetched_at"))
            file = record.get("file", {})
            reuse = bool(
                anchor
                and isinstance(file, dict)
                and saved
                and timedelta(0) <= now - saved < timedelta(hours=6)
                and file.get("region") == anchor["region"]
                and file.get("parameters") == anchor["parameters"]
            )
            if reuse:
                file = validate_map_file({"fileDetails": {"file": file}}, candidate)
            else:
                payload = await self.client.get(
                    "map_images", path + "/" + quote(identifier, safe="")
                )
                file = validate_map_file(payload, candidate)
                record = {"fetched_at": utcnow().isoformat(), "file": file}
            selected.append(file)
            updated[identifier] = record
            if anchor is None:
                anchor = file
        # Validate consistency before persisting the bounded metadata cache.
        enriched = {"orderDetails": {"files": selected}}
        frame_plan(enriched, self.config["map_order_id"], now)
        await self.client.store.save_cache(
            "map_file_metadata", {"order_id": self.config["map_order_id"], "files": updated}
        )
        return enriched, candidates[0]

    async def _async_update_data(self):
        downloaded = set()
        published = False
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
            if (
                self.client.store.cache("map_attempt_validation_version") == MAP_VALIDATION_VERSION
                and attempted
                and timedelta(0) <= utcnow() - attempted < INTERVALS[self.kind]
            ):
                raise ProviderError("map_images: scheduled retry pending")
            await self.client.store.save_cache(
                "map_attempt_validation_version", MAP_VALIDATION_VERSION
            )
            await self.client.store.save_cache("attempt_map_images", utcnow().isoformat())
            path = "/orders/" + quote(order, safe="") + "/latest"
            metadata = await self.client.get("map_images", path)
            plan_time = utcnow()
            thin = all(
                "parameters" not in file
                for file in metadata.get("orderDetails", {}).get("files", [])
                if isinstance(file, dict)
            )
            anchor_candidate = None
            if thin:
                metadata, anchor_candidate = await self._file_metadata(metadata, path, plan_time)
            frames = frame_plan(metadata, order, plan_time)
            keep = {frame["id"] for frame in frames}
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
                if anchor_candidate:
                    # Dated IDs prevent run races; recheck the order's geography after PNGs.
                    verification = await self.client.get(
                        "map_images", path + "/" + quote(anchor_candidate["fileId"], safe="")
                    )
                    anchor = validate_map_file(verification, anchor_candidate)
                    changed = anchor != metadata["orderDetails"]["files"][0]
                else:
                    verification = await self.client.get("map_images", path)
                    changed = {
                        frame["id"] for frame in frame_plan(verification, order, plan_time)
                    } != keep
                if changed:
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
            published = True
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
        finally:
            if not published and downloaded:
                await self._remove(downloaded)


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
