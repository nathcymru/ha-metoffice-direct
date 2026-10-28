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
        "id": "prefix-SYNTHETIC_SECRET-suffix",
    }
    result = describe(payload, ["SYNTHETIC_SECRET"])
    output = json.dumps(result)
    assert "SYNTHETIC_SECRET" not in output
    assert "PRIVATE_HOME_DATA" not in output
    assert "51.234567" not in output
    assert "uk-temperature" in output
    assert "total_precipitation_rate" in output
    assert "prefix-[redacted]-suffix" in output
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


def test_compact_bpf_metadata_keeps_all_collections_and_coverage_omits_location_values():
    from custom_components.metoffice_direct.capabilities import _coverage_summary, _metadata_summary

    metadata = _metadata_summary(
        {
            "collections": [
                {"id": name, "parameter_names": {"airTemperature1p5m": {"unit": {"symbol": "K"}}}}
                for name in (
                    "global-spot-percentiles",
                    "global-spot-probabilities",
                    "uk-spot-percentiles",
                    "uk-spot-probabilities",
                )
            ]
        },
        [],
    )
    assert len(metadata) == 4
    assert metadata[2]["parameters"] == {"airTemperature1p5m": "K"}
    coverage = _coverage_summary(
        {
            "parameters": {},
            "coverages": [
                {
                    "domain": {
                        "axes": {
                            "x": {"values": [-3.123456]},
                            "y": {"values": [51.123456]},
                            "t": {"values": ["2026-01-01T00:00:00Z"]},
                            "percentile": {"values": [10, 50, 90]},
                        }
                    },
                    "ranges": {
                        "airTemperature1p5m": {
                            "axisNames": ["t", "percentile"],
                            "shape": [1, 3],
                            "values": [271.123456, 272, 273],
                        }
                    },
                }
            ],
        },
        [],
    )
    assert coverage["coverages"][0]["axes"]["percentile"]["values"] == [10, 50, 90]
    assert coverage["coverages"][0]["ranges"]["airTemperature1p5m"]["value_count"] == 3
    assert "123456" not in json.dumps(coverage)


@pytest.mark.asyncio
async def test_details_reject_unadvertised_selection_and_use_only_pinned_map_file(tmp_path):
    from homeassistant.exceptions import ServiceValidationError

    hass = HomeAssistant(str(tmp_path))
    try:
        store = StateStore(hass)
        session = SimpleNamespace(
            get=Mock(
                return_value=Response(
                    200,
                    payload=b'{"fileDetails":{"file":{"fileId":"total_precipitation_rate_ts0_2026010100"}}}',
                )
            )
        )
        client = Client(session, store, {"map_images": "SYNTHETIC_SECRET"})
        hass.data["metoffice_direct"] = {
            "runtime": SimpleNamespace(
                client=client,
                coordinators={
                    "map_images": SimpleNamespace(config={"map_order_id": "synthetic-order"})
                },
            )
        }
        hass.auth = SimpleNamespace(
            async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=True))
        )
        register_capability_action(hass)

        async def details():
            return await hass.services.async_call(
                "metoffice_direct",
                "inspect_capabilities",
                {"product": "map_images", "stage": "details"},
                blocking=True,
                return_response=True,
                context=Context(user_id="synthetic-admin"),
            )

        with pytest.raises(ServiceValidationError, match="advertise"):
            await details()
        session.get.assert_not_called()
        await store.save_cache(
            "capability_probe_map_images",
            describe(
                {
                    "orderDetails": {
                        "files": [
                            {"fileId": "total_precipitation_rate_ts0_+00"},
                            {"fileId": "total_precipitation_rate_ts0_2026010100"},
                            {"fileId": "https://evil.invalid/credential"},
                        ]
                    }
                },
                [],
            ),
        )
        result = await details()
        assert result["stage"] == "details"
        assert session.get.call_args.args[0].endswith(
            "/orders/synthetic-order/latest/total_precipitation_rate_ts0_2026010100"
        )
        assert store.data["budgets"]["map_images"]["count"] == 1
        await details()
        assert session.get.call_count == 1
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_named_blended_coverage_probe_requests_only_advertised_median_fields(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        store = StateStore(hass)
        await store.save_cache(
            "capability_probe_bpf", {"identifiers": [{"value": "uk-spot-percentiles"}]}
        )
        await store.save_cache(
            "capability_probe_bpf_details",
            {
                "metadata": [
                    {
                        "id": "blended",
                        "parameters": {"windSpeed10m": "m s-1", "unrelatedField": "K"},
                    }
                ]
            },
        )
        session = SimpleNamespace(
            get=Mock(return_value=Response(200, payload=b'{"coverages":[],"parameters":{}}'))
        )
        client = Client(session, store, {"bpf": "SYNTHETIC_SECRET"})
        hass.data["metoffice_direct"] = {
            "runtime": SimpleNamespace(
                client=client,
                coordinators={
                    "bpf": SimpleNamespace(config={"longitude": -3.123456, "latitude": 53.123456})
                },
            )
        }
        hass.auth = SimpleNamespace(
            async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=True))
        )
        register_capability_action(hass)
        result = await hass.services.async_call(
            "metoffice_direct",
            "inspect_capabilities",
            {"product": "bpf", "stage": "coverage"},
            blocking=True,
            return_response=True,
            context=Context(user_id="synthetic-admin"),
        )
        assert session.get.call_args.args[0].endswith(
            "/collections/uk-spot-percentiles/instances/blended/position"
        )
        params = session.get.call_args.kwargs["params"]
        assert params["percentiles"] == "50"
        assert params["parameter-name"] == "windSpeed10m"
        assert "123456" not in json.dumps(result)
        assert "123456" not in json.dumps(store.data)
        assert store.data["budgets"]["bpf"]["count"] == 1
    finally:
        await hass.async_stop()
