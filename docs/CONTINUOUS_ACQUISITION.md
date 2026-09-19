# Continuous acquisition

## Stable boundary

Canonical start:

```sh
python -m rf_sentinel station
```

Flow: `RTLPowerScanner.acquire` → `SpectrumAcquisitionWorker` → bounded `AsyncMeasurementSink` → `SQLiteMeasurementSink`. `station` також запускає незалежний report scheduler і Telegram polling threads. Acquisition path не імпортує report generation як частину sweep processing, не будує PNG і не чекає delivery. Це current stable continuous RTL-SDR path.

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

Canonical terminal-attempt contract:

- successful acquisition створює `success` sweep із RF payload; enqueue/receipt `accepted` ще не є durability claim, authoritative є лише committed SQLite row;
- `ScanError`, timeout, parser або coverage failure створює `failed` sweep із requested profile, attempt timestamps, safe `error_classification`, `coverage.status=none`, `quality.status=unavailable` і порожніми frequency/power arrays. Це metadata outcome, не synthetic RF data. Correlation ID зв'язує row з acquisition incident; health і structured log містять operational state;
- missing slot не створює sweep. Reporting відрізняє його як відсутність row у canonical time slot/report gap; cadence telemetry окремо рахує missed slots;
- persistence failure після successful acquisition не змінює acquisition result на `failed` і не створює інший sweep. Непідтверджений payload не є durable; failure належить persistence boundary і залишається у queue/persistence telemetry, health, structured log та non-clean run summary. Якщо не вдалося persist саме failed-attempt row, correlated acquisition incident/health усе ще описують attempt, а persistence failure окремо робить lifecycle non-clean.

Таким чином failed-attempt row і missing slot однозначно різні, а жоден failure path не вигадує RF bins. Committed SQLite rows є authoritative; bounded queue може втратити queued-but-not-persisted frames після hard crash.

## Recovery і shutdown

Scan/parser failure переводить стан у `recovering` і планує повтор після configurable recovery delay (default `60 s`), без tight retry. Повторні failure alerts пригнічуються; recovery success observable. Ctrl+C/SIGINT і SIGTERM set stop event, переривають wait і active `rtl_power`, не дозволяють новий цикл. Child завершується bounded sequence `terminate` → до 2 s wait → `kill` → `reap`.

У `station` всі producer joins, active `rtl_power` cleanup, sink drain/close, Telegram/report/resource helpers і окремі SQLite closes ділять один absolute 30-second shutdown deadline. Clean coordinated stop повертає exit code `0`; component, persistence, finalization або deadline failure записує non-clean health/run summary і повертає `1`. Critical runtime threads мають daemon fallback, але station lock не звільняється, доки unresolved station-owned survivor фактично не завершився; це не дозволяє overlapping replacement instance.

## Storage/lifecycle boundary

Поточна межа ownership така:

- acquisition має окремий writable `SQLiteMeasurementSink` connection; async writer серіалізує sweep commits, а connection-scoped lock також захищає incident/telemetry operations на цьому connection;
- report scheduler має окремий writable SQLite connection для incident/state-related persistence;
- `SQLiteReportEngine` читає через окремий read-only SQLite connection (`mode=ro`, `PRAGMA query_only=ON`);
- report generation не виконується в acquisition path;
- report generation/delivery failure не повинен завершувати acquisition;
- `station` є єдиним lifecycle owner: sink володіє acquisition connection, station окремо закриває report connection; standalone `acquire` зберігає власний bounded lifecycle.

`WAL`, busy timeout і async bounded queue допомагають співіснуванню writer/readers. Report engine stream-ить rows і тримає numeric payload у temporary snapshot; `to_dict()` — compatibility API, не preferred large-report path.

## Report semantics

Scheduler формує completed hourly window на початку кожної години та completed daily window о 00:00 у `TIMEZONE` (default `Europe/Kyiv`). Вікна half-open: sweep із `started_at >= start` і `started_at < end` належить report; empty window також є валідним report dataset.

`coverage` — persisted-bin coverage: `sum(observed_bins) / sum(expected_bins)`, включно з failed sweeps, якщо для них відомий expected count. `gaps` явно містить window gap для порожнього dataset або `leading`, `between`, `trailing` intervals, де немає persisted sweep start/finish coverage. Failed sweep залишається ordered timeline row і входить у success/partial/failed counts, але має no payload. Canonical time grid містить окремі missing slots; normal native `waterfall` і Keenerd-compatible `heatmap` raster-ять такі slots як black/empty rows, без synthetic RF data або RF interpolation між sweep-ами чи missing slots. Positive gaps не вигадують RF measurements.

`waterfall.png` і `heatmap.png` будуються з одного `ReportData` dataset; waterfall — canonical pixel-to-pixel renderer, heatmap — Keenerd-compatible renderer. Обидва використовують canonical frequency vector і canonical time-slot grid; missing slots стають black/empty rows. Report JSON/text і обидві PNG описують те саме report window та sweep ordering. Telegram відправляє report artifacts як documents/files.

## Observability

`DATA_DIR/sweeps.sqlite3` містить persisted sweep rows та bounded incidents. `DATA_DIR/status/health.json` містить counters/status, resource samples/aggregates, cadence/jitter, queue/persistence, report/Telegram timing і overlap telemetry. `DATA_DIR/status/run-summary.json` фіксує terminal lifecycle, bounded incident tail, component failures і detached final sink snapshot. `DATA_DIR/logs/rf-sentinel.log` — rotated JSON Lines; secrets, Telegram URL і response body не логуються. Missing intervals представлені report gaps і окремими black/empty raster slots, а не synthetic RF rows. Це operational observability, а не завершений hardware capacity verdict.

`LatestSweepSink` — тільки lightweight test implementation, не production history.
