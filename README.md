# RF Sentinel

RF Sentinel — RX-only станція моніторингу RF-спектра для Raspberry Pi Compute Module 4 / Linux ARM64. Система приймає спектральні sweep-и, зберігає їх локально та формує hourly/daily і on-demand reports. Вона не декодує вміст приватних комунікацій і не є передавачем.

## Current stable baseline

Canonical integrated runtime:

```sh
python -m rf_sentinel station
```

Stable baseline включає continuous acquisition через supervised `rtl_power`, persistent SQLite sweeps, calendar report scheduler, report artifacts, Telegram boundary, health snapshots, structured logs і bounded recovery/shutdown.

Hardware-free validation: **186 passed, 2 skipped**. Skipped checks є opt-in:

- real RTL-SDR test requires `RF_SENTINEL_TEST_HARDWARE=1`;
- 30-minute full-range hardware test requires `RF_SENTINEL_TEST_FULL_RANGE=1`.

Characterization/calibration — WIP/roadmap. Це не stable functionality і не повинно трактуватися як реалізована ідентифікація, класифікація або absolute-power calibration.

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

Default acquisition profile: `24 MHz`–`1766 MHz`, `ACQUISITION_BIN_HZ=500000`, cadence budget `60 s`, recovery delay `60 s`. `250 kHz` не є current default. Worker виконує послідовні sweep-и; overlap і catch-up відсутні. Failed acquisition attempts persist у SQLite як rows з terminal status `failed`; missing intervals не створюють synthetic SQLite records. Missing intervals спостерігаються через report gaps, cadence/health counters і logs. Failed persisted rows можуть відображатися renderer-ом gray/hatch; renderer не створює synthetic missing rows.

Acquisition не генерує reports і не виконує Telegram delivery у своєму path. Report generation/delivery не завершує acquisition. Деталі: [continuous acquisition](docs/CONTINUOUS_ACQUISITION.md).

## Reporting, Telegram і deployment

Scheduler формує completed hourly reports на початку наступної години та daily reports о 00:00 у configured timezone (default `Europe/Kyiv`). Report windows half-open; failed rows присутні в timeline та outcome counts. Деталі storage/report semantics і Telegram authorization: [STATUS](docs/STATUS.md) та [DECISIONS](docs/DECISIONS.md).

Практичний current-state runbook для CM4: [DEPLOYMENT](docs/DEPLOYMENT.md). Repository не містить повністю відтворюваного production `systemd` unit.

## Документація

- [Стан проєкту](docs/STATUS.md)
- [Continuous acquisition](docs/CONTINUOUS_ACQUISITION.md)
- [Legacy/local survey workflow](docs/LOCAL_SURVEY.md)
- [Architecture decisions](docs/DECISIONS.md)
- [Deployment runbook](docs/DEPLOYMENT.md)
- [Requirements](docs/REQUIREMENTS.md) і [початкова архітектура](docs/ARCHITECTURE.md) — контекст та roadmap, не заміна committed implementation.
