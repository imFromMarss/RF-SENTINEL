# RF Sentinel

RF Sentinel — RX-only станція моніторингу RF-спектра для Raspberry Pi Compute Module 4 / Linux ARM64. Система приймає спектральні sweep-и, зберігає їх локально та формує hourly/daily і on-demand reports. Вона не декодує вміст приватних комунікацій і не є передавачем.

## Current stable baseline

Canonical integrated runtime:

```sh
python -m rf_sentinel station
```

Stable baseline включає continuous RTL-SDR acquisition через supervised `rtl_power`, persistent SQLite sweeps, recovery/health/structured logs, calendar report scheduler, hourly/daily/on-demand report artifacts, і Telegram delivery.

Stable functionality також включає canonical pixel-to-pixel waterfall, canonical time-slot grid із black rows для missing slots, Keenerd-compatible heatmap, Telegram report delivery як document/file, immutable calibration artifact model і bounded RTL-SDR characterization workflows: baseline, CW points, resumable CW matrix та frequency-accuracy/tuner diagnostics.

Hardware-free validation: **387 passed, 2 skipped**. Skipped checks є opt-in:

- real RTL-SDR test requires `RF_SENTINEL_TEST_HARDWARE=1`;
- 30-minute full-range hardware test requires `RF_SENTINEL_TEST_FULL_RANGE=1`.

Calibration artifact model є stable, але actual/reference source measurement, calibration correction application, LibreVNA raw/reference receiver, normalization/interpolation, cable/source corrections та uncertainty/stability work — WIP/roadmap. Characterization не є calibrated measurement system і не повинно трактуватися як absolute-power measurement.

Наступний великий milestone — **Observability & CM4 Capacity Preparation**: CPU/RSS/I/O/temperature telemetry, sweep duration/cadence/jitter, вплив reporting і Telegram на acquisition, storage/queue behavior, evidence-based рішення щодо process/CPU isolation та CM4 capacity/можливої зміни або розділення платформи. Ці можливості ще не заявляються як реалізовані.

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

Default acquisition profile: `24 MHz`–`1766 MHz`, `ACQUISITION_BIN_HZ=500000`, cadence budget `60 s`, recovery delay `60 s`. `250 kHz` не є current default. Worker виконує послідовні sweep-и; overlap і catch-up відсутні. Failed acquisition attempts persist у SQLite як rows з terminal status `failed`; missing intervals не створюють synthetic SQLite records. Missing intervals спостерігаються через report gaps, cadence/health counters і logs. Report renderer не вигадує synthetic RF data: canonical time grid містить окремі missing slots, які raster-яться як black/empty rows, без RF interpolation між sweep-ами або missing slots. Normal report використовує canonical pixel-to-pixel waterfall і Keenerd-compatible heatmap; Matplotlib renderer залишається fallback лише для no-data/empty path.

Acquisition не генерує reports і не виконує Telegram delivery у своєму path. Report generation/delivery не завершує acquisition. Деталі: [continuous acquisition](docs/CONTINUOUS_ACQUISITION.md).

## Reporting, Telegram і deployment

Scheduler формує completed hourly reports на початку наступної години та daily reports о 00:00 у configured timezone (default `Europe/Kyiv`). Report windows half-open; failed rows присутні в timeline та outcome counts. Деталі storage/report semantics і Telegram authorization: [STATUS](docs/STATUS.md) та [DECISIONS](docs/DECISIONS.md).

Поточний deployment: CM4 / Ubuntu Server із `systemd` service `rf-sentinel.service`, автоматичним стартом при boot і deployed `main` на baseline HEAD. Operational artifacts: `runtime/sweeps.sqlite3`, `runtime/status/health.json`, `runtime/logs/rf-sentinel.log`. Unit/deployment environment є operational artifact target, а не committed repository unit. Lifecycle policy: `.local*` і `.local-validation/` — disposable local validation; runtime data — operational; canonical characterization/calibration datasets — довгоживучі й не повинні випадково трактуватися як disposable. Runbook: [DEPLOYMENT](docs/DEPLOYMENT.md).

## Документація

- [Стан проєкту](docs/STATUS.md)
- [Continuous acquisition](docs/CONTINUOUS_ACQUISITION.md)
- [Legacy/local survey workflow](docs/LOCAL_SURVEY.md)
- [Architecture decisions](docs/DECISIONS.md)
- [Deployment runbook](docs/DEPLOYMENT.md)
- [Requirements](docs/REQUIREMENTS.md) і [початкова архітектура](docs/ARCHITECTURE.md) — контекст та roadmap, не заміна committed implementation.
