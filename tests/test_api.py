"""Verify failure containment and persistent quota accounting without network."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.metoffice_direct.api import (
    Client,
    LimitError,
    ProviderError,
    StateStore,
    retry_seconds,
    utcnow,
)
from custom_components.metoffice_direct.const import DAILY_LIMITS, ROOTS


@pytest.mark.asyncio
async def test_reservations_are_shared_atomic_and_survive_restart(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        store = StateStore(hass)
        await store.load()
        await asyncio.gather(*(store.reserve("global_spot", utcnow()) for _ in range(5)))
        restored = StateStore(hass)
        await restored.load()
        assert restored.data["budgets"]["global_spot"]["count"] == 5
        restored.data["budgets"]["global_spot"]["count"] = DAILY_LIMITS["global_spot"]
        with pytest.raises(LimitError):
            await restored.reserve("global_spot", utcnow())
        await restored.reserve("global_spot", utcnow() + timedelta(days=1))
        assert restored.data["budgets"]["global_spot"]["count"] == 1
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_disk_failure_prevents_network_and_key_never_persisted(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        store = StateStore(hass)
        store._save = AsyncMock(side_effect=OSError("disk full"))
        session = Mock()
        client = Client(session, store, {"global_spot": "SYNTHETIC_SECRET"})
        with pytest.raises(OSError):
            await client.get("global_spot", "/point/hourly")
        session.get.assert_not_called()
        assert "SYNTHETIC_SECRET" not in json.dumps(store.data)
    finally:
        await hass.async_stop()


class Response:
    def __init__(self, status, headers=None, payload=b"{}"):
        self.status, self.headers, self.payload = status, headers or {}, payload
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def iter_chunked(self, size):
        yield self.payload


@pytest.mark.asyncio
async def test_429_is_persisted_and_blocks_requests_after_reload(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        store = StateStore(hass)
        session = SimpleNamespace(get=Mock(return_value=Response(429, {"Retry-After": "3600"})))
        client = Client(session, store, {"global_spot": "SYNTHETIC_SECRET"})
        with pytest.raises(LimitError):
            await client.get("global_spot", "/point/hourly")
        restored = StateStore(hass)
        await restored.load()
        second = Client(session, restored, client.keys)
        with pytest.raises(LimitError):
            await second.get("global_spot", "/point/hourly")
        assert session.get.call_count == 1
        assert "SYNTHETIC_SECRET" not in open(store.path).read()
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_401_stops_polling_and_errors_never_echo_response(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        session = SimpleNamespace(get=Mock(return_value=Response(401, payload=b"SYNTHETIC_SECRET")))
        client = Client(session, StateStore(hass), {"warnings": "SYNTHETIC_SECRET"})
        for _ in range(2):
            with pytest.raises(ProviderError) as caught:
                await client.get("warnings", "/objects/feed")
            assert "SYNTHETIC_SECRET" not in str(caught.value)
        assert session.get.call_count == 1
        assert session.get.call_args.kwargs["allow_redirects"] is False
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_queued_requests_stop_after_first_authorization_rejection(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        session = SimpleNamespace(get=Mock(return_value=Response(401)))
        client = Client(session, StateStore(hass), {"global_spot": "SYNTHETIC_SECRET"})
        results = await asyncio.gather(
            *(
                client.get("global_spot", "/point/" + kind)
                for kind in ("hourly", "daily", "three-hourly")
            ),
            return_exceptions=True,
        )
        assert all(isinstance(result, ProviderError) for result in results)
        assert session.get.call_count == 1
        assert client.store.data["budgets"]["global_spot"]["count"] == 1
    finally:
        await hass.async_stop()


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/nswws/v1.1/data",
        "http://data.hub.api.metoffice.gov.uk/nswws/v1.1/data",
        ROOTS["global_spot"] + "/wrong-product",
        "https://user@data.hub.api.metoffice.gov.uk/nswws/v1.1/data",
    ],
)
def test_warning_resource_cannot_exfiltrate_key(url):
    with pytest.raises(ProviderError):
        Client.safe_url("warnings", url)


def test_retry_after_handles_seconds_date_and_invalid():
    now = utcnow().replace(microsecond=0)
    assert retry_seconds("120", now) == 120
    assert (
        retry_seconds((now + timedelta(minutes=2)).strftime("%a, %d %b %Y %H:%M:%S GMT"), now)
        == 120
    )
    assert retry_seconds("nonsense", now) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "",
        "{}",
        "[]",
        "null",
        "{",
        '{"version":1,"budgets":{"global_spot":{"count":false}},"cache":{}}',
    ],
)
async def test_existing_corrupt_or_empty_ledger_never_resets_allowance(tmp_path, content):
    hass = HomeAssistant(str(tmp_path))
    try:
        directory = tmp_path / ".storage"
        directory.mkdir()
        path = directory / "metoffice_direct_runtime.json"
        path.write_text(content)
        store = StateStore(hass)
        with pytest.raises((ProviderError, HomeAssistantError)):
            await store.load()
        assert path.read_text() == content
    finally:
        await hass.async_stop()
