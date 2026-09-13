# RF Sentinel — поточний стан

## Implemented / stable

Canonical runtime — `python -m rf_sentinel station`. Один process запускає acquisition, report scheduler і, за наявності credentials, Telegram polling; station lock у `DATA_DIR/station.lock` не дозволяє другий instance.

Stable behavior включає:

- `rtl_power` RX acquisition з default `ACQUISITION_BIN_HZ=500000`, continuous lifecycle, persistent SQLite sweeps і bounded recovery;
- окремі acquisition/reporting storage boundaries та bounded deterministic shutdown;
- hourly/daily calendar reports, half-open windows, persisted-bin coverage, gaps і failed rows у timeline;
- спільний `ReportData` для `report.json`, `report.txt`, `waterfall.png` і `heatmap.png`;
- allowlisted Telegram read-only report request, health snapshot, structured logs і bounded incident history.
- bounded RTL-SDR baseline characterization, CW point characterization, resumable CW matrix та frequency-accuracy/tuner diagnostics.

### Characterization persistence / resume

`results.json` є canonical checkpoint; logical point identities lossless/stable для current schema. Incompatible або corrupted checkpoints fail-fast до preflight/SCPI/capture/cleanup, тоді як valid partial runs resumable. Complete resume відновлює `results.csv` і `summary.md` із canonical `results.json`; regeneration derived artifacts не запускає новий RF capture.

### Validation baseline

Committed hardware-free suite: **299 passed, 2 skipped**. Skips:

- real RTL-SDR requires explicit opt-in `RF_SENTINEL_TEST_HARDWARE=1`;
- 30-minute full-range hardware test requires explicit opt-in `RF_SENTINEL_TEST_FULL_RANGE=1`.

CM4 deployment/acquisition/reporting validation була виконана зовнішньо для цього baseline. Це external validation, а не твердження, що production `systemd` deployment повністю відтворюється з repository.

## Validated externally

Практична CM4 перевірка підтвердила запуск foreground station topology, довготривалу acquisition/report coexistence та bounded-memory report path. Під час великого report generation acquisition продовжується; temporary storage може бути суттєвим, тому потрібен disk headroom.

Точні hardware-specific throughput/temperature/endurance межі не є універсальним stable contract. Їх слід читати як recorded validation для конкретного host, SDR, tool і profile.

## Legacy / compatibility

`survey` і `schedule` — старий workflow, залишений для сумісності. `acquire` і `report-schedule` — standalone/diagnostic контури; canonical topology — `station`. [LOCAL_SURVEY.md](LOCAL_SURVEY.md) зберігає корисні операційні деталі, але явно не є current source of truth.

## WIP / roadmap

Absolute amplitude calibration, actual/reference source measurement, LibreVNA raw/reference receiver, calibration correction map, normalization/interpolation, cable/source corrections та uncertainty/stability work залишаються calibration/reference roadmap. RF event detection, IQ capture, classification, multi-SDR deployment policy та production retention policy також не є реалізованою stable functionality у цьому baseline. Characterization не є calibrated measurement system.

Повністю відтворюваний production `systemd` unit і boot-time deployment recipe також залишаються deployment work; див. [DEPLOYMENT.md](DEPLOYMENT.md).

## CLI

`station` — canonical integrated runtime; `acquire` — standalone acquisition; `report-schedule` — standalone report scheduling; `survey`/`schedule` — legacy/compatibility. Без mode `python -m rf_sentinel` має identity-only behavior.
