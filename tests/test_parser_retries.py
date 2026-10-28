"""Schema upgrades may retry once, but cannot reset quotas or provider backoff."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.core import HomeAssistant
from test_api import Response
from test_homeassistant import entry

from custom_components.metoffice_direct.api import Client, StateStore, utcnow
from custom_components.metoffice_direct.coordinator import ProviderCoordinator
from custom_components.metoffice_direct.maps import MapCoordinator


@pytest.mark.asyncio
@pytest.mark.parametrize("product", ["bpf", "map_images"])
@pytest.mark.parametrize("blocked", [False, True])
async def test_new_parser_one_retry_persists_across_restart_without_resetting_budget_or_backoff(
    tmp_path, product, blocked
):
    hass = HomeAssistant(str(tmp_path))
    try:
        store = StateStore(hass)
        now = utcnow()
        until = (now + timedelta(hours=1)).timestamp() if blocked else 0
        store.data["budgets"][product] = {
            "day": now.date().isoformat(),
            "count": 5,
            "blocked_until": until,
        }
        await store.save_cache("attempt_" + product, now.isoformat())
        session = SimpleNamespace(get=Mock(return_value=Response(400)))
        client = Client(session, store, {product: "SYNTHETIC_SECRET"})
        config = {
            "latitude": 53,
            "longitude": -3,
            "warning_regions": [],
            "map_order_id": "synthetic-order",
        }

        def make(active_client):
            return (
                MapCoordinator(hass, entry(), active_client, config)
                if product == "map_images"
                else ProviderCoordinator(hass, entry(), active_client, "bpf", config)
            )

        first = make(client)
        await first.async_refresh()
        assert not first.fresh
        assert session.get.call_count == (0 if blocked else 1)
        persisted = StateStore(hass)
        await persisted.load()
        assert persisted.data["budgets"][product]["count"] == (5 if blocked else 6)
        assert persisted.data["budgets"][product]["blocked_until"] == until
        restarted = make(Client(session, persisted, {product: "SYNTHETIC_SECRET"}))
        await restarted.async_refresh()
        assert not restarted.fresh
        assert session.get.call_count == (0 if blocked else 1)
    finally:
        await hass.async_stop()
