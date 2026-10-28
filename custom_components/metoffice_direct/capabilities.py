"""Admin-only bounded schema inspection through the shared request ledger."""

import asyncio
from datetime import timedelta
from urllib.parse import quote

import probatio as vol
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.service import async_register_admin_service

from .api import ProviderError, utcnow
from .const import DOMAIN
from .model import timestamp

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

    def visit(value, depth=0, public=False):
        nonlocal visited
        visited += 1
        if depth > 8 or visited > 600:
            return {"type": type(value).__name__, "truncated": True}
        if isinstance(value, dict):
            items = list(value.items())[:40]
            return {
                "type": "object",
                "fields": {
                    clean(key): visit(item, depth + 1, key in _PUBLIC_FIELDS) for key, item in items
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
                    {"index": index, "shape": visit(value[index], depth + 1, public)}
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


def register_capability_action(hass):
    if hass.services.has_service(DOMAIN, "inspect_capabilities"):
        return
    locks = {product: asyncio.Lock() for product in ("bpf", "map_images")}

    async def inspect(call):
        product = call.data["product"]
        runtime = hass.data.get(DOMAIN, {}).get("runtime")
        if runtime is None or product not in runtime.coordinators:
            raise ServiceValidationError("The requested Met Office product is not configured")
        client = runtime.client
        async with locks[product]:
            cache_key = "capability_probe_" + product
            cached = client.store.cache(cache_key)
            fetched = timestamp((cached or {}).get("fetched_at"))
            if fetched and timedelta(0) <= utcnow() - fetched < timedelta(minutes=15):
                return {**cached, "cached": True}
            attempted = timestamp(client.store.cache(cache_key + "_attempt"))
            if attempted and timedelta(0) <= utcnow() - attempted < timedelta(minutes=15):
                raise ServiceValidationError(
                    "Capability inspection retry is delayed for 15 minutes"
                )
            if product == "bpf":
                path = "/collections"
            else:
                order_id = runtime.coordinators[product].config.get("map_order_id")
                if not order_id:
                    raise ServiceValidationError("No precipitation map order is configured")
                path = "/orders/" + quote(order_id, safe="") + "/latest"
            await client.store.save_cache(cache_key + "_attempt", utcnow().isoformat())
            try:
                payload = await client.get(product, path)
            except ProviderError as err:
                raise ServiceValidationError(str(err)) from None
            result = {
                "product": product,
                "fetched_at": utcnow().isoformat(),
                "cached": False,
                **describe(payload, client.keys.values()),
            }
            await client.store.save_cache(cache_key, result)
            return result

    async_register_admin_service(
        hass,
        DOMAIN,
        "inspect_capabilities",
        inspect,
        vol.Schema({vol.Required("product"): vol.In(["bpf", "map_images"])}),
        supports_response=SupportsResponse.ONLY,
    )
