"""Schema probes must be administrative, quota-accounted and redacted."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized
from test_api import Response

from custom_components.metoffice_direct.api import Client, StateStore
from custom_components.metoffice_direct.capabilities import describe, register_capability_action


def test_schema_summary_limits_samples_and_omits_non_public_values_and_keys():
    payload = {
        "apikey": "SYNTHETIC_SECRET",
        "private": "PRIVATE_HOME_DATA",
        "collections": [{"id": "uk-temperature", "secret": "SYNTHETIC_SECRET"}] * 101,
        "parameters": ["total_precipitation_rate"],
        "coordinates": [51.234567, -3.234567],
    }
    result = describe(payload, ["SYNTHETIC_SECRET"])
    output = json.dumps(result)
    assert "SYNTHETIC_SECRET" not in output
    assert "PRIVATE_HOME_DATA" not in output
    assert "51.234567" not in output
    assert "uk-temperature" in output
    assert "total_precipitation_rate" in output
    assert len(result["identifiers"]) <= 100
    assert len(result["shape"]["fields"]["collections"]["samples"]) == 4


@pytest.mark.asyncio
async def test_admin_probe_uses_shared_ledger_and_repeated_calls_use_sanitized_cache(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        store = StateStore(hass)
        session = SimpleNamespace(
            get=Mock(
                return_value=Response(
                    200,
                    payload=b'{"collections":[{"id":"uk-temperature"}],"private":"PRIVATE_HOME_DATA"}',
                )
            )
        )
        client = Client(session, store, {"bpf": "SYNTHETIC_SECRET"})
        hass.data["metoffice_direct"] = {
            "runtime": SimpleNamespace(client=client, coordinators={"bpf": Mock()})
        }
        hass.auth = SimpleNamespace(
            async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=False))
        )
        register_capability_action(hass)
        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                "metoffice_direct",
                "inspect_capabilities",
                {"product": "bpf"},
                blocking=True,
                return_response=True,
                context=Context(user_id="synthetic-user"),
            )
        session.get.assert_not_called()
        hass.auth.async_get_user.return_value = SimpleNamespace(is_admin=True)
        first = await hass.services.async_call(
            "metoffice_direct",
            "inspect_capabilities",
            {"product": "bpf"},
            blocking=True,
            return_response=True,
            context=Context(user_id="synthetic-admin"),
        )
        second = await hass.services.async_call(
            "metoffice_direct",
            "inspect_capabilities",
            {"product": "bpf"},
            blocking=True,
            return_response=True,
            context=Context(user_id="synthetic-admin"),
        )
        assert first["cached"] is False and second["cached"] is True
        assert session.get.call_count == 1
        assert store.data["budgets"]["bpf"]["count"] == 1
        assert "SYNTHETIC_SECRET" not in json.dumps(store.data)
        assert "PRIVATE_HOME_DATA" not in json.dumps(store.data)
    finally:
        await hass.async_stop()
