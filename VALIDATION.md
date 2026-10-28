# Local validation — 2026-10-28

Validated using Python 3.14.10 and real Home Assistant Core 2026.10.4.

- 109 tests passed (`python -m pytest`).
- Ruff checks and formatting passed across all 22 Python files.
- Python compilation passed.
- Independent review fixes included stale daily model runs, unknown warning geography, queued requests following HTTP 401, corrupt/empty persistent quota ledgers, exact map precipitation-rate identifiers, and consistent map geographic metadata.
- Real HA tests cover YAML `!secret` resolution and config-entry setup shape, empty persisted entry data, native metric/US weather conversion, availability, the HTTP authentication gate (anonymous 401, authenticated image response), withdrawn-frame rejection and unloaded runtime access.
- Synthetic transport tests cover quota persistence/restart, failed disk writes, 429 backoff, one-time parser upgrade retries without changing budgets/backoff, missing values, warning timing/geometry, named BPF instances, string percentile coordinates with strict weather values, CF units, authenticated bounded capability inspection, image-cache bounds, actual thin map catalog shapes, and provider model-run/geography changes. Interrupted PNG or verification failures discard unpublished files, including restart recovery.
- The map cold-start regression verifies 52 requests for 25 frames and five requests for the next hourly slide with 24 reused PNGs. Primary weather setup completes while an optional map refresh remains blocked.

The test suite emits an upstream aiohttp/Home Assistant application subclass deprecation warning and an aiohttp request-key warning in the real HA authentication regression. No provider credentials, authenticated provider responses, official evaluation samples, or private installation coordinates are included in the repository.

## Live acceptance checkpoint

Live checks on Home Assistant Core 2026.10.4 confirmed current, hourly, daily and three-hourly Global Spot data, official warning snapshots and nearby station observations after restart. All 11 supported BPF physical sensors reported values in their declared units from the advertised `uk-spot-percentiles` / `blended` stream. Its model-run timestamp remains unknown rather than being inferred from fetch time.

The configured precipitation order produced 25 validated hourly PNG frames covering a 24-hour sequence from one model run. The first and last cached images returned authenticated HTTP 200 with distinct content; anonymous requests returned HTTP 401. Playback, pause, next-hour controls, keyboard selection and a phone-size layout were checked in the companion Sky Canvas card. The cache survived a further Core restart without additional map downloads. Full native configuration validation passed with no reported errors or warnings.

These checks validate the tested installation and API shapes, not every subscription, geography, Home Assistant version or future provider change. The following checklist remains applicable to other installations and upgrades.

## Installation acceptance checklist

1. Verify subscribed credentials against each enabled API using server-side access only; determine that BPF credentials authorize v2.
2. Confirm Global Spot hourly/daily/three-hourly field names, units and `modelRunDate`; exercise `weather.get_forecasts` hourly/daily and inspect native sensor units.
3. Confirm nearest-station discovery and observation timestamps/location metadata.
4. Confirm warnings Atom snapshot URL and returned warning property/area shapes. Empty global warnings are valid; failed/stale/unevaluable data must remain unavailable.
5. Inspect BPF `/collections` and the advertised UK percentile `/instances` metadata. The parser deliberately rejects unsupported shapes, accepts only advertised named/datetime instances, and never invents a model-run timestamp or physical units.
6. Confirm the selected precipitation map order's actual file metadata, each dated file's timestep membership and run agreement, full next-24-hour coverage, projection/bounds and image bytes. Verify authenticated image success and unauthenticated rejection through HA HTTP.
7. Verify real entity registry IDs, all migrated dashboard consumers, restart/reload recovery, and absence of old-provider references before removing the superseded integrations.

Private deployment evidence and provider responses are kept outside this public repository. Only synthetic fixtures are distributed. No physical-device actuation or backup restore drill is implied by these integration checks.
