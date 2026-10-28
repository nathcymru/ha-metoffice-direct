"""Pure parsing; forecasts, observations and warnings retain their provenance."""

import math
from datetime import UTC, datetime, timedelta

from .const import BPF_FIELDS, CONDITIONS


def number(value):
    """Return only finite numeric values; unknown never becomes zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def condition(code):
    value = number(code)
    return CONDITIONS.get(int(value)) if value is not None and value.is_integer() else None


def dew_point(temperature, humidity):
    """Magnus estimate over water, based on a single temperature/RH tuple."""
    temp, rh = number(temperature), number(humidity)
    if temp is None or rh is None or not 0 < rh <= 100 or not -80 <= temp <= 60:
        return None
    gamma = math.log(rh / 100) + 17.625 * temp / (243.04 + temp)
    return round(243.04 * gamma / (17.625 - gamma), 3)


def parse_spot(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
        raise ValueError("Invalid forecast collection")
    if not payload["features"]:
        raise ValueError("Forecast collection has no location")
    properties = payload["features"][0].get("properties", {})
    rows = properties.get("timeSeries")
    if not isinstance(rows, list):
        raise ValueError("Forecast time series is missing")
    rows = [row for row in rows if isinstance(row, dict) and timestamp(row.get("time"))]
    rows.sort(key=lambda row: timestamp(row["time"]))
    if not rows:
        raise ValueError("Forecast time series has no valid timestamps")
    return {
        "rows": rows,
        "model_run": properties.get("modelRunDate"),
        "location": properties.get("location", {}),
        "distance_m": properties.get("requestPointDistance"),
    }


def current_row(data, now, *, key="time", maximum_age=timedelta(hours=2)):
    """Choose the latest non-future timestep; never mistake tomorrow for current."""
    rows = data.get("rows", []) if data else []
    eligible = [row for row in rows if (stamp := timestamp(row.get(key))) and stamp <= now]
    if not eligible:
        return None
    row = max(eligible, key=lambda item: timestamp(item[key]))
    return row if now - timestamp(row[key]) < maximum_age else None


def hourly_forecasts(data, now):
    result = []
    floor = now.replace(minute=0, second=0, microsecond=0)
    mapping = {
        "screenTemperature": "native_temperature",
        "screenDewPointTemperature": "native_dew_point",
        "screenRelativeHumidity": "humidity",
        "feelsLikeTemperature": "native_apparent_temperature",
        "mslp": "native_pressure",
        "windSpeed10m": "native_wind_speed",
        "windGustSpeed10m": "native_wind_gust_speed",
        "windDirectionFrom10m": "wind_bearing",
        "uvIndex": "uv_index",
        "totalPrecipAmount": "native_precipitation",
        "probOfPrecipitation": "precipitation_probability",
    }
    for row in data.get("rows", []):
        stamp = timestamp(row.get("time"))
        if stamp is None or stamp < floor:
            continue
        item = {"datetime": stamp.isoformat()}
        for source, target in mapping.items():
            if (value := number(row.get(source))) is not None:
                item[target] = value
        if (value := condition(row.get("significantWeatherCode"))) is not None:
            item["condition"] = value
        if "native_temperature" in item:
            result.append(item)
    return result


def daily_forecasts(data, now, timezone):
    result = []
    for row in data.get("rows", []):
        stamp = timestamp(row.get("time"))
        if stamp is None or stamp.astimezone(timezone).date() < now.astimezone(timezone).date():
            continue
        item = {"datetime": stamp.isoformat()}
        mapping = {
            "dayMaxScreenTemperature": "native_temperature",
            "nightMinScreenTemperature": "native_templow",
            "midday10MWindSpeed": "native_wind_speed",
            "midday10MWindDirection": "wind_bearing",
            "midday10MWindGust": "native_wind_gust_speed",
            "dayProbabilityOfPrecipitation": "precipitation_probability",
            "maxUvIndex": "uv_index",
            "middayRelativeHumidity": "humidity",
        }
        for source, target in mapping.items():
            if (value := number(row.get(source))) is not None:
                item[target] = value
        if (value := condition(row.get("daySignificantWeatherCode"))) is not None:
            item["condition"] = value
        if "native_temperature" in item:
            result.append(item)
    return result


def parse_observations(payload):
    if not isinstance(payload, list):
        raise ValueError("Invalid observation series")
    rows = []
    for item in payload:
        if isinstance(item, dict) and timestamp(item.get("datetime")):
            row = dict(item)
            row["derived_dew_point"] = dew_point(row.get("temperature"), row.get("humidity"))
            rows.append(row)
    if not rows:
        raise ValueError("No timestamped observations")
    return {"rows": sorted(rows, key=lambda row: timestamp(row["datetime"]))}


def _on_segment(point, a, b):
    x, y = point
    cross = (y - a[1]) * (b[0] - a[0]) - (x - a[0]) * (b[1] - a[1])
    return (
        abs(cross) <= 1e-10
        and min(a[0], b[0]) <= x <= max(a[0], b[0])
        and min(a[1], b[1]) <= y <= max(a[1], b[1])
    )


def _ring_contains(point, ring):
    """Return inside and boundary separately, including warning boundaries."""
    if not isinstance(ring, list) or len(ring) < 4:
        raise ValueError("Invalid warning ring")
    if any(
        not isinstance(p, list)
        or len(p) < 2
        or number(p[0]) is None
        or number(p[1]) is None
        or not -180 <= p[0] <= 180
        or not -90 <= p[1] <= 90
        for p in ring
    ):
        raise ValueError("Invalid warning point")
    if ring[0][:2] != ring[-1][:2]:
        raise ValueError("Warning ring is not closed")
    if len({tuple(point[:2]) for point in ring}) < 3:
        raise ValueError("Warning ring has fewer than three distinct points")
    inside = False
    x, y = point
    for a, b in zip(ring, ring[1:]):
        if _on_segment(point, a, b):
            return True, True
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
            inside = not inside
    return inside, False


def geometry_contains(geometry, longitude, latitude):
    """None indicates unsupported/malformed geometry, distinct from outside."""
    if not isinstance(geometry, dict):
        return None
    coordinates = geometry.get("coordinates")
    polygons = [coordinates] if geometry.get("type") == "Polygon" else coordinates
    if (
        geometry.get("type") not in ("Polygon", "MultiPolygon")
        or not isinstance(polygons, list)
        or not polygons
    ):
        return None
    try:
        for polygon in polygons:
            if not isinstance(polygon, list) or not polygon:
                return None
            inside, boundary = _ring_contains((longitude, latitude), polygon[0])
            if boundary:
                return True
            if not inside:
                continue
            in_hole = False
            for ring in polygon[1:]:
                hole, boundary = _ring_contains((longitude, latitude), ring)
                if boundary:
                    return True
                in_hole |= hole
            if not in_hole:
                return True
        return False
    except TypeError, ValueError, IndexError:
        return None


def warning_summary(payload, now, latitude, longitude, regions):
    if (
        not isinstance(payload, dict)
        or payload.get("type") != "FeatureCollection"
        or not isinstance(payload.get("features"), list)
    ):
        raise ValueError("Invalid warning collection")
    result = []
    malformed = 0
    latest = {}
    anonymous = []
    for feature in payload["features"]:
        props = feature.get("properties", {}) if isinstance(feature, dict) else {}
        if not isinstance(props, dict):
            anonymous.append(feature)
            continue
        identifier = props.get("warningId")
        if not identifier:
            anonymous.append(feature)
            continue
        previous = latest.get(str(identifier))
        if previous is not None and previous != feature:
            issued = timestamp(props.get("issuedDate"))
            prior_issued = timestamp(previous.get("properties", {}).get("issuedDate"))
            if issued is None or prior_issued is None or issued == prior_issued:
                raise ValueError("Conflicting warning versions have no ordering timestamp")
            if issued < prior_issued:
                continue
        latest[str(identifier)] = feature
    for feature in [*latest.values(), *anonymous]:
        if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
            malformed += 1
            continue
        props = feature["properties"]
        start, end = timestamp(props.get("validFromDate")), timestamp(props.get("validToDate"))
        level = str(props.get("warningLevel", "")).lower()
        if not start or not end or end <= start or level not in ("yellow", "amber", "red"):
            malformed += 1
            continue
        if end <= now or str(props.get("warningStatus", "")).upper() in ("CANCELLED", "EXPIRED"):
            continue
        areas = props.get("affectedAreas", [])
        area_names = []
        if isinstance(areas, list):
            for area in areas:
                if isinstance(area, dict):
                    area_names.extend(
                        area[k].strip()
                        for k in ("regionName", "regionCode")
                        if isinstance(area.get(k), str) and area[k].strip()
                    )
                    subregions = area.get("subRegions", [])
                    if isinstance(subregions, list):
                        area_names.extend(
                            name.strip()
                            for name in subregions
                            if isinstance(name, str) and name.strip()
                        )
        match = geometry_contains(feature.get("geometry"), longitude, latitude)
        method = "polygon"
        if match is None:
            method = "region_fallback"
            configured = {
                region.strip().casefold()
                for region in regions
                if isinstance(region, str) and region.strip()
            }
            if not configured or not area_names:
                malformed += 1
                continue
            match = bool(configured.intersection(name.casefold() for name in area_names))
        if not match:
            continue
        result.append(
            {
                "id": str(props.get("warningId", feature.get("id", ""))),
                "version": str(props.get("warningVersion", "")),
                "level": level,
                "headline": str(props.get("warningHeadline", "")),
                "weather_types": props.get("weatherType", []),
                "valid_from": start.isoformat(),
                "valid_to": end.isoformat(),
                "issued": props.get("issuedDate"),
                "active": start <= now < end,
                "impact": number(props.get("warningImpact")),
                "likelihood": number(props.get("warningLikelihood")),
                "areas": area_names,
                "filter_method": method,
                "what_to_expect": props.get("whatToExpect", []),
                "what_to_do": props.get("whatShouldIDo", []),
                "details": props.get("furtherDetails", ""),
            }
        )
    # Malformed warnings are never silently transformed into a confident empty list.
    if malformed:
        raise ValueError("Some warning records could not be evaluated")
    order = {"red": 3, "amber": 2, "yellow": 1}
    result.sort(
        key=lambda w: (
            -order[w["level"]],
            -(w["impact"] or 0),
            -(w["likelihood"] or 0),
            w["valid_from"],
        )
    )
    active = [item for item in result if item["active"]]
    return {
        "warnings": result,
        "active": active,
        "upcoming": [w for w in result if not w["active"]],
        "level": result[0]["level"] if result else "none",
        "active_level": active[0]["level"] if active else "none",
    }


def select_bpf_instance(instances, now):
    """Select an explicitly advertised instance, including the named blended stream."""
    if (
        not isinstance(instances, list)
        or not instances
        or any(
            not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in instances
        )
    ):
        raise ValueError("BPF instance metadata is invalid")
    if len(instances) == 1 and instances[0]["id"] == "blended":
        return instances[0]
    candidates = []
    for item in instances:
        date = (
            timestamp(item["id"])
            or timestamp(item.get("runDate"))
            or timestamp(item.get("datetime"))
        )
        if date and date <= now:
            candidates.append((date, item))
    if not candidates:
        raise ValueError("No supported advertised BPF instance")
    return max(candidates, key=lambda item: item[0])[1]


def parse_bpf(payload, parameter_metadata=None):
    """Decode selected physical parameters without assuming dimension ordering."""
    if not isinstance(payload, dict) or not isinstance(payload.get("coverages"), list):
        raise ValueError("Invalid BPF coverage collection")
    parameters = payload.get("parameters", {})
    rows = {}
    for coverage in payload["coverages"]:
        axes = coverage.get("domain", {}).get("axes", {})
        for key, values in coverage.get("ranges", {}).items():
            if key not in BPF_FIELDS:
                continue
            names, shape = values.get("axisNames", []), values.get("shape", [])
            flattened = values.get("values", [])
            if len(names) != len(shape) or "t" not in names or math.prod(shape) != len(flattened):
                raise ValueError("Invalid BPF array dimensions")
            indices = {}
            for name, size in zip(names, shape):
                axis = axes.get(name, {}).get("values", [])
                if len(axis) != size:
                    raise ValueError("Invalid BPF axis coordinates")
                if name == "t":
                    continue
                if "percentile" in name.lower():
                    matches = [i for i, v in enumerate(axis) if number(v) == 50]
                    if not matches:
                        raise ValueError("BPF median unavailable")
                    indices[name] = matches[0]
                elif size == 1:
                    indices[name] = 0
                else:
                    raise ValueError("Unsupported BPF non-time dimension")
            unit = (
                parameters.get(key, {})
                .get("unit", (parameter_metadata or {}).get(key, {}).get("unit", {}))
                .get("symbol")
            )
            if isinstance(unit, dict):
                unit = unit.get("value")
            for t_index, time in enumerate(axes["t"]["values"]):
                if not timestamp(time):
                    continue
                offset = 0
                for name, size in zip(names, shape):
                    offset = offset * size + (t_index if name == "t" else indices[name])
                value = number(flattened[offset])
                if value is None:
                    continue
                target = BPF_FIELDS[key][1]
                if target == "m/s" and unit == "m s-1":
                    pass
                elif target == "°C" and unit in ("K", "kelvin"):
                    value -= 273.15
                elif target == "%" and unit in ("1", "fraction"):
                    value *= 100
                elif target == "%" and unit == "%":
                    pass
                elif target == "°C" and unit in ("Cel", "degC", "°C"):
                    pass
                elif unit not in (
                    target,
                    "degrees" if target == "°" else target,
                    "1" if target == "UV index" else target,
                ):
                    raise ValueError(f"Unsupported BPF unit for {key}")
                rows.setdefault(time, {"time": time})[key] = value
    if not rows:
        raise ValueError("No supported BPF values returned")
    return {"rows": sorted(rows.values(), key=lambda row: timestamp(row["time"]))}
