"""Admin-only bounded schema inspection through the shared request ledger."""

import asyncio
import re
from datetime import timedelta
from urllib.parse import quote

import probatio as vol
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.service import async_register_admin_service

from .api import ProviderError, utcnow
from .const import BPF_FIELDS, DOMAIN
from .model import select_bpf_instance, timestamp

_PUBLIC_FIELDS = frozenset(
    {
        "id",
        "collectionId",
        "fileId",
        "parameterId",
        "parameters",
        "timesteps",
        "runDateTime",
        "run",
        "surfaceId",
        "levels",
        "modelId",
        "format",
        "axisNames",
        "shape",
        "symbol",
        "runDate",
        "datetime",
        "t",
        "z",
        "projection",
        "lowerBound",
        "upperBound",
        "uomLabel",
    }
)


def describe(payload, secrets):
    """Return structure and a narrow metadata allowlist; never raw payloads."""

    def clean(value):
        result = str(value)
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[redacted]")
        return result[:240]

    visited = 0

    def visit(value, depth=0, public=False, region=False):
        nonlocal visited
        visited += 1
        if depth > 8 or visited > 600:
            return {"type": type(value).__name__, "truncated": True}
        if isinstance(value, dict):
            items = list(value.items())[:40]
            return {
                "type": "object",
                "fields": {
                    clean(key): visit(
                        item,
                        depth + 1,
                        key in _PUBLIC_FIELDS
                        or (region and key in {"name", "x", "y", "north", "south", "east", "west"}),
                        region or key == "region",
                    )
                    for key, item in items
                },
                "omitted_fields": max(0, len(value) - len(items)),
            }
        if isinstance(value, list):
            indices = list(range(min(3, len(value))))
            if len(value) > 3:
                indices.append(len(value) - 1)
            return {
                "type": "array",
                "length": len(value),
                "samples": [
                    {"index": index, "shape": visit(value[index], depth + 1, public, region)}
                    for index in indices
                ],
            }
        return {
            "type": type(value).__name__,
            **(
                {"value": clean(value)}
                if public and isinstance(value, (str, int, float)) and not isinstance(value, bool)
                else {}
            ),
        }

    identifiers = []
    searched = 0

    def find_ids(value, path="", depth=0):
        nonlocal searched
        searched += 1
        if depth > 8 or len(identifiers) >= 100 or searched > 4096:
            return
        if isinstance(value, dict):
            for key, item in list(value.items())[:40]:
                if len(identifiers) >= 100:
                    return
                child = f"{path}.{clean(key)}" if path else clean(key)
                if key in ("id", "collectionId") and isinstance(item, str):
                    identifiers.append({"path": clean(child), "value": clean(item)})
                elif isinstance(item, (dict, list)):
                    find_ids(item, child, depth + 1)
        elif isinstance(value, list):
            for index, item in enumerate(value[:100]):
                find_ids(item, f"{path}[{index}]", depth + 1)

    find_ids(payload)
    return {"shape": visit(payload), "identifiers": identifiers}


def _field_values(shape, name):
    """Read only previously sanitized scalar fields from a cached diagnostic."""
    result = []
    if isinstance(shape, dict):
        fields = shape.get("fields", {})
        value = fields.get(name, {}).get("value")
        if isinstance(value, str):
            result.append(value)
        for child in fields.values():
            result.extend(_field_values(child, name))
        for sample in shape.get("samples", []):
            result.extend(_field_values(sample.get("shape"), name))
    return result


def _parameters(items, secrets):
    """Public parameter IDs and units, never locations or forecast values."""
    result = {}
    if not isinstance(items, dict):
        return result
    for key, value in list(items.items())[:100]:
        if not isinstance(key, str) or any(secret and secret in key for secret in secrets):
            continue
        unit = value.get("unit", {}).get("symbol") if isinstance(value, dict) else None
        if isinstance(unit, dict):
            unit = unit.get("value")
        result[key] = (
            unit
            if isinstance(unit, str)
            and len(unit) <= 60
            and not any(secret and secret in unit for secret in secrets)
            else None
        )
    return result


def _metadata_summary(payload, secrets):
    """Compact all collections/instances without spending the bound on the first one."""
    items = (
        payload
        if isinstance(payload, list)
        else payload.get("instances", payload.get("collections", []))
    )
    result = []
    if isinstance(items, list):
        for item in items[:30]:
            if not isinstance(item, dict):
                continue
            identifier = item.get("id")
            if not isinstance(identifier, str) or any(
                secret and secret in identifier for secret in secrets
            ):
                continue
            result.append(
                {
                    "id": identifier,
                    "parameters": _parameters(item.get("parameter_names", {}), secrets),
                    "runDate": item.get("runDate") if timestamp(item.get("runDate")) else None,
                    "datetime": item.get("datetime") if timestamp(item.get("datetime")) else None,
                }
            )
    return result


def _coverage_summary(payload, secrets):
    """Time and dimensional metadata only; omit geographic axes and weather values."""
    result = {"parameters": _parameters(payload.get("parameters", {}), secrets), "coverages": []}
    for item in payload.get("coverages", [])[:20]:
        axes = item.get("domain", {}).get("axes", {})
        ranges = item.get("ranges", {})
        result["coverages"].append(
            {
                "axes": {
                    name: {
                        "length": len(axis.get("values", [])),
                        **(
                            {"first": axis["values"][0], "last": axis["values"][-1]}
                            if name == "t"
                            and axis.get("values")
                            and all(
                                timestamp(value)
                                for value in (axis["values"][0], axis["values"][-1])
                            )
                            else {}
                        ),
                        **(
                            {"values": axis.get("values", [])[:30]}
                            if "percentile" in name.lower()
                            else {}
                        ),
                    }
                    for name, axis in axes.items()
                },
                "ranges": {
                    name: {
                        "axisNames": item.get("axisNames"),
                        "shape": item.get("shape"),
                        "value_count": len(item.get("values", [])),
                    }
                    for name, item in ranges.items()
                },
            }
        )
    return result


def register_capability_action(hass):
    if hass.services.has_service(DOMAIN, "inspect_capabilities"):
        return
    locks = {product: asyncio.Lock() for product in ("bpf", "map_images")}

    async def inspect(call):
        product = call.data["product"]
        stage = call.data["stage"]
        runtime = hass.data.get(DOMAIN, {}).get("runtime")
        if runtime is None or product not in runtime.coordinators:
            raise ServiceValidationError("The requested Met Office product is not configured")
        client = runtime.client
        async with locks[product]:
            cache_key = "capability_probe_" + product + ("" if stage == "catalog" else "_" + stage)
            cached = client.store.cache(cache_key)
            fetched = timestamp((cached or {}).get("fetched_at"))
            if fetched and timedelta(0) <= utcnow() - fetched < timedelta(minutes=15):
                return {**cached, "cached": True}
            attempted = timestamp(client.store.cache(cache_key + "_attempt"))
            if attempted and timedelta(0) <= utcnow() - attempted < timedelta(minutes=15):
                raise ServiceValidationError(
                    "Capability inspection retry is delayed for 15 minutes"
                )
            params = None
            catalog = client.store.cache("capability_probe_" + product) or {}
            if product == "bpf":
                path = "/collections"
                if stage in ("details", "coverage"):
                    if not any(
                        item.get("value") == "uk-spot-percentiles"
                        for item in catalog.get("identifiers", [])
                    ):
                        raise ServiceValidationError(
                            "Inspect the catalog to advertise the UK percentile collection first"
                        )
                    path += "/uk-spot-percentiles/instances"
                if stage == "coverage":
                    detail = client.store.cache("capability_probe_bpf_details") or {}
                    try:
                        instance = select_bpf_instance(detail.get("metadata", []), utcnow())
                    except ValueError:
                        raise ServiceValidationError(
                            "Inspect supported instance metadata before a point coverage probe"
                        ) from None
                    fields = [key for key in BPF_FIELDS if key in instance.get("parameters", {})]
                    if not fields:
                        raise ServiceValidationError(
                            "No supported parameters advertised by the instance"
                        )
                    config = runtime.coordinators[product].config
                    path += "/" + quote(instance["id"], safe="") + "/position"
                    params = {
                        "coords": f"POINT({config['longitude']} {config['latitude']})",
                        "parameter-name": ",".join(fields),
                        "percentiles": "50",
                        "datetime": utcnow().replace(minute=0, second=0, microsecond=0).isoformat()
                        + "/..",
                    }
            else:
                if stage == "coverage":
                    raise ServiceValidationError(
                        "Map inspection supports catalog and details stages only"
                    )
                order_id = runtime.coordinators[product].config.get("map_order_id")
                if not order_id:
                    raise ServiceValidationError("No precipitation map order is configured")
                path = "/orders/" + quote(order_id, safe="") + "/latest"
                if stage == "details":
                    files = _field_values(catalog.get("shape"), "fileId")
                    pinned = [
                        identifier
                        for identifier in files
                        if re.fullmatch(r"total_precipitation_rate_ts\d{1,3}_\d{10}", identifier)
                    ]
                    if not pinned:
                        raise ServiceValidationError(
                            "Inspect the map catalog to advertise a run-pinned precipitation file first"
                        )
                    path += "/" + quote(pinned[0], safe="")
            await client.store.save_cache(cache_key + "_attempt", utcnow().isoformat())
            try:
                payload = await client.get(product, path, params=params)
            except ProviderError as err:
                raise ServiceValidationError(str(err)) from None
            result = {
                "product": product,
                "stage": stage,
                "fetched_at": utcnow().isoformat(),
                "cached": False,
                **(
                    {"coverage": _coverage_summary(payload, client.keys.values())}
                    if stage == "coverage"
                    else describe(payload, client.keys.values())
                ),
                **(
                    {"metadata": _metadata_summary(payload, client.keys.values())}
                    if product == "bpf" and stage != "coverage"
                    else {}
                ),
            }
            await client.store.save_cache(cache_key, result)
            return result

    async_register_admin_service(
        hass,
        DOMAIN,
        "inspect_capabilities",
        inspect,
        vol.Schema(
            {
                vol.Required("product"): vol.In(["bpf", "map_images"]),
                vol.Optional("stage", default="catalog"): vol.In(
                    ["catalog", "details", "coverage"]
                ),
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
