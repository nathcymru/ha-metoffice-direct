"""One bounded transport and persistent request ledger shared by all entities."""

import asyncio
import copy
import json
import math
import os
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import aiohttp
from homeassistant.helpers.json import save_json
from homeassistant.util.json import load_json

from .const import DAILY_LIMITS, ROOTS


class ProviderError(Exception):
    """Sanitized provider failure, safe for HA's logs."""


class LimitError(ProviderError):
    """Request blocked by local or provider budget."""


def utcnow():
    return datetime.now(UTC)


def retry_seconds(value, now):
    if not value:
        return None
    try:
        seconds = float(value)
    except TypeError, ValueError:
        try:
            date = parsedate_to_datetime(str(value))
            seconds = (date.astimezone(UTC) - now).total_seconds()
        except TypeError, ValueError, OverflowError:
            return None
    return max(1, min(172800, seconds)) if 0 <= seconds < float("inf") else None


class StateStore:
    """Persist counters before requests; write failures stop network use."""

    def __init__(self, hass):
        self.hass = hass
        self.path = hass.config.path(".storage", "metoffice_direct_runtime.json")
        self.lock = asyncio.Lock()
        self.data = {"version": 1, "budgets": {}, "cache": {}}

    async def load(self):
        def read():
            return os.path.exists(self.path), load_json(self.path)

        exists, data = await self.hass.async_add_executor_job(read)
        if exists:
            if (
                not isinstance(data, dict)
                or data.get("version") != 1
                or not isinstance(data.get("budgets"), dict)
                or not isinstance(data.get("cache"), dict)
            ):
                raise ProviderError(
                    "Persistent request ledger is invalid; refusing to reset limits"
                )
            for product, budget in data["budgets"].items():
                if (
                    product not in DAILY_LIMITS
                    or not isinstance(budget, dict)
                    or type(budget.get("count")) is not int
                    or budget.get("count", 0) < 0
                    or not isinstance(budget.get("day"), str)
                    or type(budget.get("blocked_until", 0)) not in (int, float)
                    or not math.isfinite(budget.get("blocked_until", 0))
                ):
                    raise ProviderError(
                        "Persistent request counters are invalid; refusing to reset limits"
                    )
                try:
                    if date.fromisoformat(budget["day"]).isoformat() != budget["day"]:
                        raise ValueError
                except ValueError:
                    raise ProviderError("Persistent request counter date is invalid") from None
            self.data = data

    def _write(self, data):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        save_json(self.path, data, private=True, atomic_writes=True)

    async def _save(self):
        await self.hass.async_add_executor_job(self._write, copy.deepcopy(self.data))

    async def reserve(self, product, now):
        async with self.lock:
            day = now.astimezone(UTC).date().isoformat()
            budget = self.data["budgets"].setdefault(product, {})
            if budget.get("day", "") < day:
                budget.update(day=day, count=0)
            if budget.get("blocked_until", 0) > now.timestamp():
                raise LimitError(f"{product}: provider backoff is active")
            if budget.get("count", 0) >= DAILY_LIMITS[product]:
                raise LimitError(f"{product}: daily request reserve reached")
            budget["count"] = budget.get("count", 0) + 1
            # This is deliberately awaited, so restart/crash cannot reset a used allowance.
            await self._save()

    async def block(self, product, until):
        async with self.lock:
            budget = self.data["budgets"].setdefault(product, {})
            budget["blocked_until"] = max(budget.get("blocked_until", 0), until.timestamp())
            await self._save()

    def cache(self, key):
        return copy.deepcopy(self.data["cache"].get(key))

    async def save_cache(self, key, value):
        async with self.lock:
            self.data["cache"][key] = copy.deepcopy(value)
            await self._save()


class Client:
    """Keys live only in runtime memory; no automatic retries or redirects."""

    def __init__(self, session, store, keys):
        self.session, self.store, self.keys = session, store, keys
        self.locks = {product: asyncio.Lock() for product in keys}
        self.auth_failed = set()
        self.failures = {}
        self.last_request = {}

    @staticmethod
    def safe_url(product, url):
        parsed, root = urlparse(url), urlparse(ROOTS[product])
        if (
            parsed.scheme != "https"
            or parsed.hostname != root.hostname
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
            or not parsed.path.startswith(root.path + "/")
        ):
            raise ProviderError("Provider supplied an unexpected resource URL")
        return url

    async def get(self, product, path, *, params=None, text=False, binary=False):
        if product not in self.keys or not self.keys[product]:
            raise ProviderError(f"{product}: API key is not configured")
        if product in self.auth_failed:
            raise ProviderError(f"{product}: credentials rejected; update YAML secret and reload")
        url = self.safe_url(product, path if path.startswith("https://") else ROOTS[product] + path)
        async with self.locks[product]:
            if product in self.auth_failed:
                raise ProviderError(
                    f"{product}: credentials rejected; update YAML secret and reload"
                )
            loop = asyncio.get_running_loop()
            remaining = self.last_request.get(product, 0) + 1 - loop.time()
            if remaining > 0:
                await asyncio.sleep(remaining)
            await self.store.reserve(product, utcnow())
            self.last_request[product] = loop.time()
            try:
                async with self.session.get(
                    url,
                    params=params,
                    headers={
                        "apikey": self.keys[product],
                        "Accept": "image/png"
                        if binary
                        else "application/atom+xml"
                        if text
                        else "application/json",
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                    allow_redirects=False,
                ) as response:
                    if response.status in (401, 403):
                        self.auth_failed.add(product)
                        raise ProviderError(
                            f"{product}: authorization rejected ({response.status})"
                        )
                    if response.status == 429:
                        now = utcnow()
                        seconds = retry_seconds(response.headers.get("Retry-After"), now)
                        # Without explicit retry time, conservatively wait until next UTC day.
                        until = (
                            now + timedelta(seconds=seconds)
                            if seconds
                            else (now + timedelta(days=1)).replace(
                                hour=0, minute=0, second=5, microsecond=0
                            )
                        )
                        await self.store.block(product, until)
                        raise LimitError(f"{product}: provider quota reached")
                    if response.status != 200:
                        if response.status >= 500:
                            await self._backoff(product)
                        raise ProviderError(f"{product}: HTTP {response.status}")
                    chunks = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        chunks.extend(chunk)
                        if len(chunks) > 8 * 1024 * 1024:
                            raise ProviderError(
                                f"{product}: response exceeds configured size bound"
                            )
                    try:
                        result = (
                            bytes(chunks)
                            if binary
                            else chunks.decode("utf-8")
                            if text
                            else json.loads(chunks)
                        )
                    except UnicodeDecodeError, ValueError:
                        raise ProviderError(f"{product}: malformed response") from None
                    self.failures[product] = 0
                    return result
            except aiohttp.ClientError, TimeoutError:
                await self._backoff(product)
                raise ProviderError(f"{product}: connection failed") from None

    async def _backoff(self, product):
        attempt = self.failures.get(product, 0) + 1
        self.failures[product] = attempt
        seconds = min(3600, 60 * 2 ** min(attempt - 1, 6))
        await self.store.block(product, utcnow() + timedelta(seconds=seconds))
