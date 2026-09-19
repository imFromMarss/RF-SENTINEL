# RF Sentinel

RF Sentinel — RX-only станція моніторингу RF-спектра для Raspberry Pi Compute Module 4 / Linux ARM64. Система приймає спектральні sweep-и, зберігає їх локально та формує hourly/daily і on-demand reports. Вона не декодує вміст приватних комунікацій і не є передавачем.

## Current stable baseline

Canonical integrated runtime:

```sh
python -m rf_sentinel station
```

Stable baseline включає continuous RTL-SDR acquisition через supervised `rtl_power`, persistent SQLite sweeps, recovery/health/structured logs, calendar report scheduler, hourly/daily/on-demand report artifacts, і Telegram delivery.

Stable functionality також включає canonical pixel-to-pixel waterfall, canonical time-slot grid із black rows для missing slots, Keenerd-compatible heatmap, Telegram report delivery як document/file, immutable calibration artifact model і bounded RTL-SDR characterization workflows: baseline, CW points, resumable CW matrix та frequency-accuracy/tuner diagnostics.

Hardware-free validation: **577 passed, 2 skipped**. Skipped checks є opt-in:

- real RTL-SDR test requires `RF_SENTINEL_TEST_HARDWARE=1`;
- 30-minute full-range hardware test requires `RF_SENTINEL_TEST_FULL_RANGE=1`.

Calibration artifact model є stable, але actual/reference source measurement, calibration correction application, LibreVNA raw/reference receiver, normalization/interpolation, cable/source corrections та uncertainty/stability work — WIP/roadmap. Characterization не є calibrated measurement system і не повинно трактуватися як absolute-power measurement.

Operational observability реалізована: health/run summary містять resource, cadence, queue, persistence, report/Telegram timing та overlap telemetry; rotated structured logs і bounded incidents доповнюють ці snapshots. Ці метрики дають evidence для окремого capacity decision, але самі по собі не доводять універсальну CM4 capacity і не приймають рішення про process/CPU isolation.

## CLI matrix

| Command | Current meaning |
| --- | --- |
| `python -m rf_sentinel station` | canonical integrated runtime: acquisition + reports + Telegram |
| `python -m rf_sentinel acquire` | standalone acquisition producer (`rtl_power` → SQLite), без report/Telegram |
| `python -m rf_sentinel report-schedule` | standalone calendar report scheduler |
| `python -m rf_sentinel survey` | legacy/compatibility one-shot survey workflow |
| `python -m rf_sentinel schedule` | legacy/compatibility continuous survey workflow |
| `python -m rf_sentinel` | identity-only behavior: друкує `RF Sentinel`, завершується з exit code `0` і не запускає hardware/network runtime |

`acquire` і `report-schedule` не слід запускати паралельно з `station` для того самого SDR/data store.

## Current acquisition

Default acquisition profile: `24 MHz`–`1766 MHz`, `ACQUISITION_BIN_HZ=500000`, cadence budget `60 s`, recovery delay `60 s`. `250 kHz` не є current default. Worker виконує послідовні sweep-и; overlap і catch-up відсутні. Successful attempt має RF payload і стає durable лише після SQLite commit. `ScanError`/timeout/parser/coverage failure persist як `failed` row із requested profile, timestamps, safe error classification, `coverage=none` і без RF payload; correlated incident, health і log лишають operational evidence. Missing slot не має sweep row та відображається report gap/cadence telemetry і black/empty time-grid row. Persistence failure після successful acquisition є окремим storage failure: вона не створює `failed` sweep і не робить RF payload durable; її фіксують sink/persistence telemetry, health, structured log і non-clean run summary. Report renderer не вигадує synthetic RF data і не інтерполює між sweep-ами або missing slots.

Acquisition не генерує reports і не виконує Telegram delivery у своєму path. Report generation/delivery не завершує acquisition. Деталі: [continuous acquisition](docs/CONTINUOUS_ACQUISITION.md).

## Reporting, Telegram і deployment

Scheduler формує completed hourly reports на початку наступної години та daily reports о 00:00 у configured timezone (default `Europe/Kyiv`). Report windows half-open; failed rows присутні в timeline та outcome counts. Деталі storage/report semantics і Telegram authorization: [STATUS](docs/STATUS.md) та [DECISIONS](docs/DECISIONS.md).

Поточний deployment: CM4 / Ubuntu Server із `systemd` service `rf-sentinel.service`, автоматичним стартом при boot і deployed `main` на baseline HEAD. Operational artifacts: `runtime/sweeps.sqlite3`, `runtime/status/health.json`, `runtime/status/run-summary.json`, `runtime/logs/rf-sentinel.log` і `runtime/reports/`. Unit/deployment environment є operational artifact target, а не committed repository unit. Lifecycle policy: усі `.local*` paths — disposable validation area; canonical characterization/calibration datasets є long-lived source records і мають зберігатися поза `.local*`; runtime DB/status/log/report artifacts — operational і керуються deployment/backup/retention policy. Цей cleanup datasets не переміщує. Runbook: [DEPLOYMENT](docs/DEPLOYMENT.md).

## Документація

- [Стан проєкту](docs/STATUS.md)
- [Continuous acquisition](docs/CONTINUOUS_ACQUISITION.md)
- [Legacy/local survey workflow](docs/LOCAL_SURVEY.md)
- [Architecture decisions](docs/DECISIONS.md)
- [Deployment runbook](docs/DEPLOYMENT.md)
- [Requirements](docs/REQUIREMENTS.md) і [початкова архітектура](docs/ARCHITECTURE.md) — контекст та roadmap, не заміна committed implementation.
