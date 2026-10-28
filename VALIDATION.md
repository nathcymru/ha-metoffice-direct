# Local validation — 2026-10-28

Validated using Python 3.14.10 and real Home Assistant Core 2026.10.4.

- 101 tests passed (`python -m pytest`).
- Ruff checks and formatting passed across all 22 Python files.
- Python compilation passed.
- Independent review fixes included stale daily model runs, unknown warning geography, queued requests following HTTP 401, corrupt/empty persistent quota ledgers, exact map precipitation-rate identifiers, and consistent map geographic metadata.
- Real HA tests cover YAML `!secret` resolution and config-entry setup shape, empty persisted entry data, native metric/US weather conversion, availability, the HTTP authentication gate (anonymous 401, authenticated image response), withdrawn-frame rejection and unloaded runtime access.
- Synthetic transport tests cover quota persistence/restart, failed disk writes, 429 backoff, one-time parser upgrade retries without changing budgets/backoff, missing values, warning timing/geometry, named BPF instances, CF units, authenticated bounded capability inspection, image-cache bounds, actual thin map catalog shapes, and provider model-run/geography changes. Interrupted PNG or verification failures discard unpublished files, including restart recovery.
- The map cold-start regression verifies 52 requests for 25 frames and five requests for the next hourly slide with 24 reused PNGs. Primary weather setup completes while an optional map refresh remains blocked.

The test suite emits an upstream aiohttp/Home Assistant application subclass deprecation warning and an aiohttp request-key warning in the real HA authentication regression. No provider credentials, authenticated provider responses, official evaluation samples, or private installation coordinates are included in the repository.

## Live acceptance checkpoint

The deployment agent confirmed current, hourly, daily and three-hourly Global Spot data, official warning snapshots and nearby station observations after restart. Sanitized authenticated metadata confirmed the UK `uk-spot-percentiles` / `blended` BPF stream and dated precipitation map file identifiers with whole-order timestep sets. The updated production BPF/PNG parser still requires the coordinated acceptance below.

## Installation acceptance checklist

1. Verify subscribed credentials against each enabled API using server-side access only; determine that BPF credentials authorize v2.
2. Confirm Global Spot hourly/daily/three-hourly field names, units and `modelRunDate`; exercise `weather.get_forecasts` hourly/daily and inspect native sensor units.
3. Confirm nearest-station discovery and observation timestamps/location metadata.
4. Confirm warnings Atom snapshot URL and returned warning property/area shapes. Empty global warnings are valid; failed/stale/unevaluable data must remain unavailable.
5. Inspect BPF `/collections` and the advertised UK percentile `/instances` metadata. The parser deliberately rejects unsupported shapes, accepts only advertised named/datetime instances, and never invents a model-run timestamp or physical units.
6. Confirm the selected precipitation map order's actual file metadata, each dated file's timestep membership and run agreement, full next-24-hour coverage, projection/bounds and image bytes. Verify authenticated image success and unauthenticated rejection through HA HTTP.
7. Verify real entity registry IDs, all migrated dashboard consumers, restart/reload recovery, and absence of old-provider references before removing the superseded integrations.

The provider implementation agent did not handle credentials, perform live deployment or publish the repository. Live operations and acceptance are coordinated by the parent and deployment agents; their evidence is kept outside this public repository.
