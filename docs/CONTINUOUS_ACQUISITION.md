# Continuous acquisition

## Stable boundary

Canonical start:

```sh
python -m rf_sentinel station
```

Flow: `RTLPowerScanner.acquire` → `SpectrumAcquisitionWorker` → bounded `AsyncMeasurementSink` → `SQLiteMeasurementSink`. `station` також запускає незалежний report scheduler і Telegram polling threads. Acquisition path не імпортує report generation як частину sweep processing, не будує PNG і не чекає delivery.

| Variable | Default |
| --- | ---: |
| `RF_SENTINEL_ACQUISITION_START_HZ` | `24000000` |
| `RF_SENTINEL_ACQUISITION_STOP_HZ` | `1766000000` |
| `RF_SENTINEL_ACQUISITION_BIN_HZ` | `500000` |
| `RF_SENTINEL_ACQUISITION_CADENCE_BUDGET_SECONDS` | `60` |
| `RF_SENTINEL_ACQUISITION_RECOVERY_SECONDS` | `60` |
| `RF_SENTINEL_RTL_DEVICE_INDEX` | `0` |

`250 kHz` не є current default. Cadence budget — operational budget між початками проходів, а не гарантія фактичної тривалості sweep. Якщо sweep довший за budget, наступний старт відбувається після його завершення; overlap/catch-up немає.

## Durable sweep semantics

Кожна спроба має `SpectrumSweep` з profile, actual/requested geometry, device/tool provenance, timestamps, coverage, quality та terminal status:

- `success` — повний payload і complete coverage;
- `partial` — persisted attempt із degraded quality/partial coverage;
- `failed` — persisted metadata без payload, `coverage.status=none`, `quality.status=unavailable` та `error_classification`.

Failed acquisition attempts persist у SQLite як rows з outcome `failed`; parser failure і persistence failure також залишаються observable через health counters, incident history та logs. Missing intervals не створюють synthetic SQLite records: вони визначаються/спостерігаються через report gaps, cadence/health counters і logs. Committed SQLite rows є authoritative; bounded queue може втратити queued-but-not-persisted frames після hard crash.

## Recovery і shutdown

Scan/parser failure переводить стан у `recovering` і планує повтор після configurable recovery delay (default `60 s`), без tight retry. Повторні failure alerts пригнічуються; recovery success observable. Ctrl+C/SIGINT і SIGTERM set stop event, переривають wait і active `rtl_power`, не дозволяють новий цикл. Child завершується bounded sequence `terminate` → до 2 s wait → `kill` → `reap`.

У `station` acquisition thread має deadline join до 30 s, report thread — до 10 s; sink drain/close має timeout 10 s і SQLite connections закриваються окремо. Shutdown deterministic/bounded; перевищення deadline логуються.

## Storage/lifecycle boundary

Після PR #10 межа така:

- acquisition має окремий writable `SQLiteMeasurementSink` connection;
- report scheduler має окремий writable SQLite connection для incident/state-related persistence;
- `SQLiteReportEngine` читає через окремий read-only SQLite connection (`mode=ro`, `PRAGMA query_only=ON`);
- report generation не виконується в acquisition path;
- report generation/delivery failure не повинен завершувати acquisition;
- scheduler і acquisition мають окреме bounded shutdown lifecycle.

`WAL`, busy timeout і async bounded queue допомагають співіснуванню writer/readers. Report engine stream-ить rows і тримає numeric payload у temporary snapshot; `to_dict()` — compatibility API, не preferred large-report path.

## Report semantics

Scheduler формує completed hourly window на початку кожної години та completed daily window о 00:00 у `TIMEZONE` (default `Europe/Kyiv`). Вікна half-open: sweep із `started_at >= start` і `started_at < end` належить report; empty window також є валідним report dataset.

`coverage` — persisted-bin coverage: `sum(observed_bins) / sum(expected_bins)`, включно з failed sweeps, якщо для них відомий expected count. `gaps` явно містить window gap для порожнього dataset або `leading`, `between`, `trailing` intervals, де немає persisted sweep start/finish coverage. Failed sweep залишається ordered timeline row і входить у success/partial/failed counts, але має no payload; у `waterfall` та `heatmap` така row позначається окремим сірим hatch-патерном, а не нульовою потужністю. Positive gaps не вигадують RF measurements.

`waterfall.png` і `heatmap.png` будуються з одного `ReportData` dataset; відмінність — renderer palette/layout. Report JSON/text і обидві PNG описують те саме report window та sweep ordering.

## Observability

`DATA_DIR/sweeps.sqlite3` містить persisted sweep rows та bounded incidents. `DATA_DIR/status/health.json` містить counters/status, timestamps і report/acquisition health. `DATA_DIR/logs/rf-sentinel.log` — rotated JSON Lines; secrets, Telegram URL і response body не логуються. Missing intervals представлені report gaps, а не rows. Renderer не створює synthetic missing rows; persisted failed rows можуть бути позначені gray/hatch і не маскуються порожнім «успішним» spectrum.

`LatestSweepSink` — тільки lightweight test implementation, не production history.
