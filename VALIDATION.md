# Local validation — 2026-10-28

Validated using Python 3.14.10 and real Home Assistant Core 2026.10.4.

- 71 tests passed (`python -m pytest`).
- Ruff checks and formatting passed across all 18 Python files.
- Python compilation passed.
- Independent review fixes included stale daily model runs, unknown warning geography, queued requests following HTTP 401, corrupt/empty persistent quota ledgers, exact map precipitation-rate identifiers, and consistent map geographic metadata.
- Real HA tests cover YAML `!secret` resolution and config-entry setup shape, empty persisted entry data, native metric/US weather conversion, availability, the HTTP authentication gate (anonymous 401, authenticated image response), withdrawn-frame rejection and unloaded runtime access.
- Synthetic transport tests cover quota persistence/restart, failed disk writes, 429 backoff, missing values, warning timing/geometry, BPF dimensional/unit validation, image-cache bounds and provider model-run changes during downloads.

The test suite emits an upstream aiohttp/Home Assistant application subclass deprecation warning and an aiohttp request-key warning in the real HA authentication regression. No provider credentials, authenticated provider responses, official evaluation samples, or private installation coordinates are included in the repository.

## Live acceptance still required

1. Verify subscribed credentials against each enabled API using server-side access only; determine that BPF credentials authorize v2.
2. Confirm Global Spot hourly/daily/three-hourly field names, units and `modelRunDate`; exercise `weather.get_forecasts` hourly/daily and inspect native sensor units.
3. Confirm nearest-station discovery and observation timestamps/location metadata.
4. Confirm warnings Atom snapshot URL and returned warning property/area shapes. Empty global warnings are valid; failed/stale/unevaluable data must remain unavailable.
5. Inspect BPF `/collections` and the advertised UK percentile `/instances` metadata. The parser deliberately rejects unsupported shapes and never invents a `latest` instance or physical units.
6. Confirm the selected precipitation map order's actual file metadata, one timestep per PNG, full next-24-hour coverage, projection/bounds and image bytes. Verify authenticated image success and unauthenticated rejection through HA HTTP.
7. Verify real entity registry IDs, all migrated dashboard consumers, restart/reload recovery, and absence of old-provider references before removing the superseded integrations.

No live deployment, key rotation, authenticated API calls or repository publication was performed by the provider implementation agent. The staged code is ready for those coordinated acceptance steps.
