"""Independent provider updates and cached forecast data."""

import logging
from datetime import timedelta
from urllib.parse import quote
from xml.etree.ElementTree import ParseError, fromstring

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ProviderError, utcnow
from .const import BPF_FIELDS, INTERVALS, MAX_AGE
from .model import (
    parse_bpf,
    parse_observations,
    parse_spot,
    select_bpf_instance,
    timestamp,
    warning_summary,
)

_LOGGER = logging.getLogger(__name__)
BPF_PARSER_REVISION = 2


class ProviderCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, client, kind, config):
        super().__init__(
            hass,
            _LOGGER,
            name=f"Met Office Direct {kind}",
            config_entry=entry,
            update_interval=INTERVALS[kind],
        )
        self.client, self.kind, self.config = client, kind, config
        self.data = client.store.cache(kind)
        self.error_code = None

    @property
    def fresh(self):
        fetched = timestamp((self.data or {}).get("fetched_at"))
        return bool(
            self.last_update_success
            and (
                self.kind != "bpf"
                or (self.data or {}).get("parser_revision") == BPF_PARSER_REVISION
            )
            and fetched
            and timedelta(0) <= utcnow() - fetched < MAX_AGE[self.kind]
        )

    async def _async_update_data(self):
        try:
            cached = self.client.store.cache(self.kind)
            fetched = timestamp((cached or {}).get("fetched_at"))
            parser_current = (
                self.kind != "bpf"
                or self.client.store.cache("bpf_attempt_parser_revision") == BPF_PARSER_REVISION
            )
            cache_current = (
                self.kind != "bpf" or (cached or {}).get("parser_revision") == BPF_PARSER_REVISION
            )
            if (
                cache_current
                and fetched
                and timedelta(0) <= utcnow() - fetched < INTERVALS[self.kind]
            ):
                self.error_code = None
                return cached
            attempted = timestamp(self.client.store.cache("attempt_" + self.kind))
            if (
                parser_current
                and attempted
                and timedelta(0) <= utcnow() - attempted < INTERVALS[self.kind]
            ):
                raise ProviderError(f"{self.kind}: scheduled retry pending")
            if self.kind == "bpf":
                await self.client.store.save_cache(
                    "bpf_attempt_parser_revision", BPF_PARSER_REVISION
                )
            await self.client.store.save_cache("attempt_" + self.kind, utcnow().isoformat())
            if self.kind in ("hourly", "daily", "three_hourly"):
                data = await self._spot()
            elif self.kind == "warnings":
                data = await self._warnings()
            elif self.kind == "observations":
                data = await self._observations()
            else:
                data = await self._bpf()
            if self.kind == "bpf":
                data["parser_revision"] = BPF_PARSER_REVISION
            data["fetched_at"] = utcnow().isoformat()
            await self.client.store.save_cache(self.kind, data)
            self.error_code = None
            return data
        except (
            ProviderError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            AttributeError,
            ParseError,
        ) as err:
            self.error_code = (
                str(err)
                if isinstance(err, ProviderError)
                else f"{self.kind}: unsupported or invalid provider data"
            )
            raise UpdateFailed(self.error_code) from err

    async def _spot(self):
        payload = await self.client.get(
            "global_spot",
            f"/point/{self.kind.replace('_', '-')}",
            params={
                "latitude": self.config["latitude"],
                "longitude": self.config["longitude"],
                "dataSource": "BD1",
                "includeLocationName": "true",
                "excludeParameterMetadata": "false",
            },
        )
        return parse_spot(payload)

    async def _observations(self):
        station = self.client.store.cache("observation_station")
        if not station:
            nearest = await self.client.get(
                "observations",
                "/nearest",
                params={
                    "lat": f"{self.config['latitude']:.2f}",
                    "lon": f"{self.config['longitude']:.2f}",
                    "max": 1,
                },
            )
            if (
                not isinstance(nearest, list)
                or not nearest
                or not isinstance(nearest[0].get("geohash"), str)
            ):
                raise ValueError("Observation station discovery failed")
            station = nearest[0]
            geohash = station["geohash"]
            if not geohash.isalnum() or len(geohash) > 12:
                raise ValueError("Invalid observation station")
            await self.client.store.save_cache("observation_station", station)
        payload = await self.client.get("observations", "/" + quote(station["geohash"], safe=""))
        return {**parse_observations(payload), "station": station}

    async def _warnings(self):
        feed = await self.client.get("warnings", "/objects/feed", text=True)
        if "<!DOCTYPE" in feed.upper() or "<!ENTITY" in feed.upper():
            raise ValueError("Warning feed cannot contain document declarations")
        root = fromstring(feed)
        links = root.findall("{http://www.w3.org/2005/Atom}link")
        urls = [
            link.get("href") for link in links if link.get("rel") == "related" and link.get("href")
        ]
        if len(urls) != 1:
            raise ValueError("Warning snapshot link missing or ambiguous")
        url = self.client.safe_url("warnings", urls[0])
        cached = self.client.store.cache("warning_snapshot")
        if cached and cached.get("url") == url:
            payload = cached["payload"]
        else:
            payload = await self.client.get("warnings", url)
            # Validate before replacing the authoritative cached snapshot.
            warning_summary(
                payload,
                utcnow(),
                self.config["latitude"],
                self.config["longitude"],
                self.config["warning_regions"],
            )
            await self.client.store.save_cache("warning_snapshot", {"url": url, "payload": payload})
        return {"payload": payload}

    def warnings(self):
        if not self.fresh:
            return None
        return warning_summary(
            self.data["payload"],
            utcnow(),
            self.config["latitude"],
            self.config["longitude"],
            self.config["warning_regions"],
        )

    async def _bpf(self):
        """Use advertised UK percentile metadata; unsupported shapes remain unavailable."""
        collection = self.client.store.cache("bpf_collection")
        if collection != "uk-spot-percentiles":
            payload = await self.client.get("bpf", "/collections")
            collections = payload if isinstance(payload, list) else payload.get("collections", [])
            candidates = [
                item
                for item in collections
                if isinstance(item, dict) and item.get("id") == "uk-spot-percentiles"
            ]
            if len(candidates) != 1:
                raise ProviderError("bpf: cannot identify one advertised UK percentile collection")
            collection = candidates[0]["id"]
            await self.client.store.save_cache("bpf_collection", collection)
        path = "/collections/" + quote(collection, safe="")
        metadata = await self.client.get("bpf", path + "/instances")
        instances = (
            metadata
            if isinstance(metadata, list)
            else metadata.get("instances", metadata.get("collections", []))
        )
        # A named blended instance is a stream, not a model-run timestamp.
        selected = select_bpf_instance(instances, utcnow())
        instance = selected["id"]
        advertised = selected.get("parameter_names", {})
        fields = [field for field in BPF_FIELDS if field in advertised]
        if not fields:
            raise ProviderError("bpf: no supported parameters advertised by the instance")
        payload = await self.client.get(
            "bpf",
            path + "/instances/" + quote(instance, safe="") + "/position",
            params={
                "coords": f"POINT({self.config['longitude']} {self.config['latitude']})",
                "parameter-name": ",".join(fields),
                "percentiles": "50",
                "datetime": utcnow().replace(minute=0, second=0, microsecond=0).isoformat() + "/..",
            },
        )
        return {
            **parse_bpf(payload, advertised),
            "instance": instance,
            "collection": collection,
            "model_run": (timestamp(instance).isoformat() if timestamp(instance) else None),
        }
