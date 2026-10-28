# Met Office Direct

A Home Assistant integration for the Met Office DataHub APIs. Runs inside Home Assistant; no additional server or container is required. API credentials are read from YAML secrets and never enter config entries, frontend configuration, image URLs or diagnostics.

Tested against Home Assistant 2026.10.4 with synthetic responses. Provider schemas are validated before data is published; unsupported metadata remains unavailable. Installation-specific credentials, available data and dashboard behavior require live acceptance.

## Installation

In HACS, add `https://github.com/nathcymru/ha-metoffice-direct` as a custom repository with category **Integration**, then download Met Office Direct. For manual installation, copy `custom_components/metoffice_direct` into Home Assistant's `custom_components` directory.

Add the configuration shown in `configuration.example.yaml`, provide its `!secret` values privately in Home Assistant's `secrets.yaml`, then restart Home Assistant. Remove example lines for optional products you do not subscribe to; an unresolved `!secret` prevents configuration validation. The YAML configuration creates one integration entry automatically. Reloading the integration rereads YAML secrets. Keep `secrets.yaml` out of source control and protect Home Assistant backups that contain it.

Required subscription: Global Spot. Optional: National Severe Weather Warning Service, Land Observations, Blended Probabilistic Forecast v2, Map Images. Atmospheric credentials can be configured for a later bounded order implementation; no atmospheric model downloads are performed.

This is a custom HACS repository. Inclusion in the default HACS catalogue is not implied.

## Weather and sensors

The initial weather entity ID is `weather.metoffice_direct`. Entity IDs can subsequently be renamed in Home Assistant, so verify the registry during migration.

- Current weather uses the latest non-future Global Spot hourly forecast timestep. It is a model forecast at the provider's nearest forecast site, not a measurement at the house.
- `weather.get_forecasts` supports hourly and daily forecasts. Hourly entries preserve same-timestep humidity, dew point, temperature and wind. Home Assistant converts native Celsius, m/s, Pa and millimetres into the entity's selected units.
- Weather attributes `current_time`, `model_run`, `fetched_at` are ISO timestamps. `source` identifies the point forecast; `station_observation` is false. No cloud percentage is invented from weather condition codes.
- Current and forecast records retain the exact official `metoffice_weather_code`; daily records also retain `metoffice_night_weather_code`. Unsupported codes remain absent, so cards do not invent a symbol from Home Assistant's broader condition categories. Current UV and precipitation probability come from the same timestep. `metoffice_daily_fetched_at` and `metoffice_daily_model_run` identify the daily forecast's own provenance.
- Current numeric point fields have enabled sensors, including temperature, dew point, apparent temperature, RH, wind/gust/direction, pressure, visibility, UV, precipitation rate/probability and previous-hour precipitation/snow water equivalent. Missing values make the affected sensor unavailable.
- `sensor.metoffice_direct_uv_index` provides the point forecast UV value.
- Nearby Land Observations are separate `sensor.metoffice_direct_station_*` entities. Temperature, humidity, estimated dew point, pressure, visibility, wind, condition, pressure tendency and data time retain station identity and timestamps. Estimated dew point uses a single observation's temperature/RH pair. Station data never silently replaces point forecast values.
- BPF sensors expose supported median physical quantities only when advertised CoverageJSON dimensions, parameter names and units can be validated. Unknown dimensions/units or metadata are reported as unavailable. The UK percentile collection is discovered from the catalog. Its explicitly advertised `blended` instance is a stream: `instance` identifies it, while `model_run` remains unknown because the API does not advertise a run timestamp for that stream. Freshness uses fetch age and actual forecast times. Selected parameter horizons vary; this does not promise 14 days of hourly weather.

Current point data expires after two hours, station data after three hours, and point forecast model runs after six hours. Cache fetch age is checked independently. API failures immediately mark the affected product unavailable; one optional product failure does not disable other products.

## Official warnings

| Entity | Meaning |
| --- | --- |
| `sensor.metoffice_direct_warning_count` | All issued local warnings, including future warnings |
| `sensor.metoffice_direct_active_warning_count` | Warnings currently in their validity interval |
| `sensor.metoffice_direct_warning_level` | Highest issued local level |
| `sensor.metoffice_direct_active_warning_level` | Highest currently active local level |
| `binary_sensor.metoffice_direct_warning_active` | At least one currently active local warning |
| `binary_sensor.metoffice_direct_warning_upcoming` | At least one issued future local warning |

Warning sensors have a `warnings` attribute containing `id`, `version`, `level`, `headline`, `weather_types`, `valid_from`, `valid_to`, `issued`, `active`, `impact`, `likelihood`, `areas`, `filter_method`, `what_to_expect`, `what_to_do`, and `details`. Levels are `none`, `yellow`, `amber`, `red`; failed or stale data is unavailable, never a confident `none`. Duplicate IDs use the newest issuance timestamp; ambiguous conflicting versions fail validation.

Selection uses the configured home's position within warning polygons, including boundaries and respecting holes. Configured regions are a labelled fallback only when geometry cannot be evaluated. Start and expiry are reevaluated locally every minute. The Atom feed is polled every minute and the snapshot is downloaded only when its URL changes. Data expires after ten minutes without a successful refresh; request failures immediately mark it unavailable. This cadence follows the [NSWWS developer presentation guide](https://www.metoffice.gov.uk/api/assets/file/03115NSWWSStyleGuideupdate_v5pdf). Provider availability, network failure and rate-limit backoff can delay delivery; consult the official Met Office warning service for safety decisions.

## Precipitation playback

Configure `map_order_id` for a single-parameter total precipitation rate PNG order with hourly steps covering the next 24 hours. The catalog supplies thin file records. Each selected record is checked against the documented single-file metadata endpoint for its exact `total_precipitation_rate` parameter, run and nested geographic extent.

The observed API convention identifies a frame as `total_precipitation_rate_tsN_YYYYMMDDHH`. Its metadata supplies the whole order's allowed timestep set, not a single timestep. The integration checks that `N` belongs to that set and that the dated suffix agrees with `runDateTime`; valid time is then run plus N hours. Moving `+00`/`+12` aliases are excluded. One consistent model run supplies 25 hourly frames from the current hour through the next 24 hours. Every selected frame must carry identical region and extent metadata. Incomplete, inconsistent or unsupported orders remain unavailable.

`sensor.metoffice_direct_forecast_map` is `ready` when usable. Attributes:

- `frames`: ordered `{id, valid_time, model_run}` objects, with 64-character lowercase hexadecimal IDs.
- `fetched_at`, `model_run`, `coverage_start`, `coverage_end`.
- `source`: `Met Office forecast`; `attribution`: `Powered by Met Office data`.
- `region`: provider region metadata; `bounds`: provider `extent` object when available.

Authenticated clients request `GET /api/metoffice_direct/map/{id}`. The response is a cached PNG; no provider credentials or provider image URLs are sent to the browser. Unknown IDs and stale/unavailable maps return 404. Authentication is enforced by Home Assistant.

[HA-Sky-Canvas](https://github.com/nathcymru/HA-Sky-Canvas) supplies the companion forecast playback card and optional dashboard house scenes.

Only missing images are downloaded. A fresh anchor metadata request checks the order's geography each hour; other dated file metadata can be reused for up to six hours when its full geography and parameter match that anchor. Cache IDs include parameter and geographic metadata. A second anchor check after downloads detects a change during the batch. Interrupted or unverified downloads are removed; restart only trusts images listed in a successfully published manifest.

A cold 25-frame load uses 52 requests: one catalog, 25 file metadata, 25 PNGs and one final metadata check. A normal hourly slide uses five requests and reuses 24 PNGs. Model-run changes, metadata expiry or changed geography require additional validated downloads, all under the same 900-call ceiling. Maps load in the background so their paced downloads do not delay primary weather startup. Images persist across restart and are bounded to 64 MiB, 8 MiB per image, and 25 published frames. Refresh is hourly; actual coverage timestamps remain visible between refreshes. Viewer count and playback do not cause provider requests.

## Request limits and recovery

| Product | Normal polling | Local daily ceiling |
| --- | --- | ---: |
| Global Spot | Hourly series every 20 min; daily and three-hourly series every hour | 330 shared calls |
| BPF v2 | Selected UK percentile series every 2 hours, including discovery/instance requests | 50 shared calls |
| Warnings | Feed every minute; changed snapshots only | 19,000 calls |
| Land Observations | Selected station every 15 minutes; station discovery cached | 330 calls |
| Map Images | One order hourly; missing frames only; metadata recheck when images change | 900 calls |
| Atmospheric | No automatic download | No consumption |

The local ledger counts requests from this installation, not calls made by other clients using the same account. It is persisted before requests, survives restart, and blocks network access if the ledger cannot be written. Attempts are persisted to prevent reload bursts. A parser schema upgrade may permit one immediate revalidation; its version marker is persisted, and all request limits and provider backoff remain in force. No automatic request retries occur. HTTP 429 respects `Retry-After`; without it, calls wait until the next UTC day. Server/network failures use persistent exponential backoff. HTTP 401/403 stops that product until reload. API keys are never placed in logs or returned response errors.

Runtime state lives in `.storage/metoffice_direct_runtime.json`; map images live in `.storage/metoffice_direct_maps`. These are private local caches, not release assets. Do not delete the quota ledger to work around limits. Rotating a key does not reset the provider's account allowance.

Administrators can use `metoffice_direct.inspect_capabilities` for a bounded schema summary. `catalog` and `details` stages select only fixed endpoints and previously advertised identifiers; BPF `coverage` returns median parameter units, array dimensions and forecast time coverage without location coordinates or weather values. Each stage uses at most one shared-budget request and caches the sanitized result for 15 minutes.

## Development and validation

Use a Python environment containing Home Assistant 2026.10.4 and pytest/pytest-asyncio. Run `python -m pytest`, `ruff check .`, and `ruff format --check .`. Tests use invented values and images; official evaluation sample payloads are not redistributed.

Before live cutover, verify authenticated hourly/daily field names and units; station selection; BPF v2 collection/instance metadata; warning feed and geometry; map metadata and PNGs; entity registry IDs; forecast actions; dashboard subscriptions; and restart/reload recovery. Keep the previous integrations available until their replacements pass these checks.

## Data and attribution

Powered by Met Office data. Met Office data remains subject to the terms of the user's subscriptions and applicable attribution requirements. This repository's MIT license covers its original code, not provider data.

Primary API documentation: [Global Spot and BPF](https://datahub.metoffice.gov.uk/docs/f/category/site-specific/overview), [warnings](https://datahub.metoffice.gov.uk/docs/f/category/warnings/overview), [observations](https://datahub.metoffice.gov.uk/docs/f/category/observations/overview), [map images](https://datahub.metoffice.gov.uk/docs/f/category/map-images/overview), [atmospheric models](https://datahub.metoffice.gov.uk/docs/f/category/atmospheric/overview).
