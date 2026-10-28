"""Synthetic records, not redistributable Met Office evaluation samples."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.metoffice_direct.model import (
    condition,
    current_row,
    daily_forecasts,
    dew_point,
    geometry_contains,
    hourly_forecasts,
    number,
    parse_bpf,
    parse_observations,
    parse_spot,
    timestamp,
    warning_summary,
)

NOW = datetime(2026, 12, 1, 18, 30, tzinfo=UTC)


@pytest.mark.parametrize("value", [None, "0", "unknown", float("nan"), float("inf"), True])
def test_invalid_numeric_values_never_become_zero(value):
    assert number(value) is None


def test_current_timestep_cannot_be_future_or_stale():
    rows = {
        "rows": [
            {"time": (NOW + timedelta(minutes=30)).isoformat()},
            {"time": (NOW - timedelta(minutes=30)).isoformat()},
        ]
    }
    assert current_row(rows, NOW)["time"] == rows["rows"][1]["time"]
    assert current_row(rows, NOW + timedelta(hours=3)) is None
    assert current_row({"rows": rows["rows"][:1]}, NOW) is None
    assert timestamp("2026-12-01T10:00:00") is None


def test_forecast_carries_same_hour_humidity_dewpoint_and_si_wind():
    payload = {
        "features": [
            {
                "properties": {
                    "modelRunDate": "2026-12-01T18:00:00Z",
                    "timeSeries": [
                        {
                            "time": "2026-12-01T18:00:00Z",
                            "screenTemperature": 3,
                            "screenRelativeHumidity": 90,
                            "screenDewPointTemperature": 1.5,
                            "windSpeed10m": 2,
                            "significantWeatherCode": 0,
                            "mslp": 101300,
                        },
                        {"time": "2026-12-01T19:00:00Z", "screenTemperature": None},
                    ],
                }
            }
        ]
    }
    data = parse_spot(payload)
    assert hourly_forecasts(data, NOW) == [
        {
            "datetime": "2026-12-01T18:00:00+00:00",
            "native_temperature": 3,
            "humidity": 90,
            "native_dew_point": 1.5,
            "native_wind_speed": 2,
            "native_pressure": 101300,
            "condition": "clear-night",
            "metoffice_weather_code": 0,
        }
    ]
    assert "cloud_coverage" not in hourly_forecasts(data, NOW)[0]
    assert condition(4) is None
    assert condition(1.5) is None


def test_daily_forecast_uses_local_date_and_does_not_invent_missing_values():
    data = {
        "rows": [
            {
                "time": "2026-12-01T00:00:00Z",
                "dayMaxScreenTemperature": 4,
                "nightMinScreenTemperature": -1,
                "daySignificantWeatherCode": 1,
            },
            {"time": "2026-11-30T00:00:00Z", "dayMaxScreenTemperature": 2},
        ]
    }
    assert len(daily_forecasts(data, NOW, ZoneInfo("Europe/London"))) == 1
    assert daily_forecasts(data, NOW, ZoneInfo("Europe/London"))[0]["native_templow"] == -1


def test_station_dewpoint_uses_same_observation_tuple_and_null_stays_null():
    data = parse_observations(
        [
            {"datetime": "2026-12-01T18:00:00Z", "temperature": 3, "humidity": 90},
            {"datetime": "2026-12-01T17:00:00Z", "temperature": 3, "humidity": None},
        ]
    )
    assert data["rows"][0]["derived_dew_point"] is None
    assert 1 < data["rows"][1]["derived_dew_point"] < 2
    assert dew_point(3, 0) is None


POLYGON = {
    "type": "Polygon",
    "coordinates": [
        [[-4, 52], [-2, 52], [-2, 54], [-4, 54], [-4, 52]],
        [[-3.5, 52.5], [-2.5, 52.5], [-2.5, 53.5], [-3.5, 53.5], [-3.5, 52.5]],
    ],
}


def test_geometry_checks_holes_boundaries_and_malformed_distinctly():
    assert geometry_contains(POLYGON, -3.8, 53) is True
    assert geometry_contains(POLYGON, -3, 53) is False
    assert geometry_contains(POLYGON, -4, 53) is True
    assert geometry_contains(POLYGON, -3.5, 53) is True
    assert geometry_contains({"type": "Point", "coordinates": [-3, 53]}, -3, 53) is None


def warning(start, end, level="yellow", geometry=POLYGON):
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "warningId": str(start),
            "warningVersion": "1.2",
            "warningLevel": level,
            "warningStatus": "ISSUED",
            "validFromDate": start.isoformat(),
            "validToDate": end.isoformat(),
            "affectedAreas": [{"regionName": "Wales", "subRegions": ["Flintshire"]}],
            "warningHeadline": "Synthetic warning",
        },
    }


def test_warning_future_active_and_expiry_evaluate_against_now():
    payload = {
        "type": "FeatureCollection",
        "features": [
            warning(NOW - timedelta(hours=1), NOW + timedelta(hours=1)),
            warning(NOW + timedelta(hours=1), NOW + timedelta(hours=3), "amber"),
        ],
    }
    first = warning_summary(payload, NOW, 53, -3.8, [])
    assert first["level"] == "amber"
    assert first["active_level"] == "yellow"
    assert len(first["upcoming"]) == 1
    second = warning_summary(payload, NOW + timedelta(hours=1), 53, -3.8, [])
    assert second["active_level"] == "amber"
    assert len(second["warnings"]) == 1
    assert warning_summary(payload, NOW + timedelta(hours=3), 53, -3.8, [])["level"] == "none"


def test_region_fallback_only_when_geometry_unavailable_and_never_hides_bad_dates():
    feature = warning(NOW, NOW + timedelta(hours=1), geometry=None)
    payload = {"type": "FeatureCollection", "features": [feature]}
    assert (
        warning_summary(payload, NOW, 53, -3, ["Wales"])["warnings"][0]["filter_method"]
        == "region_fallback"
    )
    with pytest.raises(ValueError, match="could not be evaluated"):
        warning_summary(payload, NOW, 53, -3, [])
    feature["properties"]["validToDate"] = "invalid"
    with pytest.raises(ValueError):
        warning_summary(payload, NOW, 53, -3, ["Wales"])


def test_bpf_dimension_order_and_kelvin_conversion():
    payload = {
        "parameters": {"airTemperature1p5m": {"unit": {"symbol": "K"}}},
        "coverages": [
            {
                "domain": {
                    "axes": {
                        "t": {"values": ["2026-12-01T18:00:00Z", "2026-12-01T19:00:00Z"]},
                        "percentile": {"values": [10, 50, 90]},
                    }
                },
                "ranges": {
                    "airTemperature1p5m": {
                        "axisNames": ["percentile", "t"],
                        "shape": [3, 2],
                        "values": [270, 271, 274.15, 275.15, 278, 279],
                    }
                },
            }
        ],
    }
    result = parse_bpf(payload)
    assert result["rows"][0]["airTemperature1p5m"] == pytest.approx(1)
    assert result["rows"][1]["airTemperature1p5m"] == pytest.approx(2)
    payload["parameters"]["airTemperature1p5m"]["unit"]["symbol"] = "unknown"
    with pytest.raises(ValueError, match="unit"):
        parse_bpf(payload)


def test_named_bpf_instance_is_advertised_not_an_invented_model_timestamp():
    from custom_components.metoffice_direct.model import select_bpf_instance

    item = {"id": "blended", "parameter_names": {"windSpeed10m": {"unit": {"symbol": "m s-1"}}}}
    assert select_bpf_instance([item], NOW) is item
    with pytest.raises(ValueError):
        select_bpf_instance([{"id": "latest"}], NOW)
    with pytest.raises(ValueError):
        select_bpf_instance([item, {"id": "some-other-stream"}], NOW)
    with pytest.raises(ValueError):
        select_bpf_instance([{"id": (NOW + timedelta(hours=1)).isoformat()}], NOW)


def test_bpf_cf_wind_units_from_advertised_metadata_are_supported_without_overriding_explicit_units():
    payload = {
        "coverages": [
            {
                "domain": {
                    "axes": {"t": {"values": [NOW.isoformat()]}, "percentile": {"values": [50]}}
                },
                "ranges": {
                    "windSpeed10m": {
                        "axisNames": ["t", "percentile"],
                        "shape": [1, 1],
                        "values": [2.5],
                    }
                },
            }
        ]
    }
    metadata = {"windSpeed10m": {"unit": {"symbol": "m s-1"}}}
    assert parse_bpf(payload, metadata)["rows"][0]["windSpeed10m"] == 2.5
    payload["parameters"] = {"windSpeed10m": {"unit": {"symbol": "knots"}}}
    with pytest.raises(ValueError, match="unit"):
        parse_bpf(payload, metadata)


def bpf_string_percentile_payload(percentiles, values):
    return {
        "parameters": {},
        "coverages": [
            {
                "domain": {
                    "axes": {
                        "locationId": {"values": ["synthetic-site"]},
                        "percentiles": {"values": percentiles},
                        "t": {"values": [NOW.isoformat()]},
                        "x": {"values": [0]},
                        "y": {"values": [0]},
                        "z": {"values": [0]},
                    }
                },
                "ranges": {
                    "windSpeed10m": {
                        "axisNames": ["percentiles", "t"],
                        "shape": [len(percentiles), 1],
                        "values": values,
                    }
                },
            }
        ],
    }


def test_live_bpf_string_percentile_axis_selects_median_using_advertised_units():
    metadata = {"windSpeed10m": {"unit": {"symbol": "m s-1"}}}
    payload = bpf_string_percentile_payload(["10", "50", "90"], [1, 2.5, 5])
    assert parse_bpf(payload, metadata)["rows"] == [{"time": NOW.isoformat(), "windSpeed10m": 2.5}]
    payload = bpf_string_percentile_payload(["50"], ["2.5"])
    with pytest.raises(ValueError, match="No supported BPF values"):
        parse_bpf(payload, metadata)


@pytest.mark.parametrize(
    "coordinates", [["NaN"], ["Infinity"], [True], ["-1", "50"], ["101", "50"], ["50", 50], ["50%"]]
)
def test_bpf_percentile_metadata_rejects_invalid_or_ambiguous_coordinates(coordinates):
    metadata = {"windSpeed10m": {"unit": {"symbol": "m s-1"}}}
    with pytest.raises(ValueError, match="percentile|median"):
        parse_bpf(bpf_string_percentile_payload(coordinates, [1] * len(coordinates)), metadata)
