# RF Sentinel — поточний стан

## Implemented / stable

Canonical runtime — `python -m rf_sentinel station`. Baseline: `main` at `6db0a6637688bbaf030214412e972d9e3fb524d3`. Один process запускає acquisition, report scheduler і, за наявності credentials, Telegram polling; station lock у `DATA_DIR/station.lock` не дозволяє другий instance.

Stable behavior включає:

- continuous RTL-SDR acquisition через supervised `rtl_power` з default `ACQUISITION_BIN_HZ=500000`, persistent SQLite sweeps і bounded recovery;
- окремі acquisition/reporting storage boundaries та bounded deterministic shutdown;
- hourly/daily calendar reports, half-open windows, persisted-bin coverage, gaps і failed rows у timeline;
- спільний `ReportData` для `report.json`, `report.txt`, `waterfall.png` і `heatmap.png`; canonical pixel-to-pixel waterfall, canonical time-slot grid і black missing rows; Keenerd-compatible heatmap;
- allowlisted Telegram read-only report request із delivery як document/file, health snapshot, structured logs і bounded incident history;
- bounded RTL-SDR baseline characterization, CW point characterization, resumable CW matrix та frequency-accuracy/tuner diagnostics.

### Characterization persistence / resume

`results.json` є canonical checkpoint; logical point identities lossless/stable для current schema. Incompatible або corrupted checkpoints fail-fast до preflight/SCPI/capture/cleanup, тоді як valid partial runs resumable. Complete resume відновлює `results.csv` і `summary.md` із canonical `results.json`; regeneration derived artifacts не запускає новий RF capture. Calibration artifact model — immutable/self-identifying JSON artifact із versioned schema, receiver/profile/provenance, raw/reference dataset links, point-level corrections і забороненими interpolation/extrapolation; model описує artifact contract, а не завершену absolute calibration.

### Validation baseline

Committed hardware-free suite: **387 passed, 2 skipped**. Skips:

- real RTL-SDR requires explicit opt-in `RF_SENTINEL_TEST_HARDWARE=1`;
- 30-minute full-range hardware test requires explicit opt-in `RF_SENTINEL_TEST_FULL_RANGE=1`.

CM4 deployment/acquisition/reporting validation була виконана зовнішньо для цього baseline. Deployed target — CM4 / Ubuntu Server, `rf-sentinel.service`, automatic boot start, current `main` at the baseline HEAD. Repository не містить unit як reproducible deployment artifact; це не скасовує факт operational deployment.

## Validated externally

Практична CM4 перевірка підтвердила запуск foreground station topology, довготривалу acquisition/report coexistence та bounded-memory report path. Під час великого report generation acquisition продовжується; temporary storage може бути суттєвим, тому потрібен disk headroom.

Точні hardware-specific throughput/temperature/endurance межі не є універсальним stable contract. Їх слід читати як recorded validation для конкретного host, SDR, tool і profile.

## Legacy / compatibility

`survey` і `schedule` — старий workflow, залишений для сумісності. `acquire` і `report-schedule` — standalone/diagnostic контури; canonical topology — `station`. [LOCAL_SURVEY.md](LOCAL_SURVEY.md) зберігає корисні операційні деталі, але явно не є current source of truth.

## WIP

Наступний великий milestone — **Observability & CM4 Capacity Preparation**. Він охоплює CPU/RSS/I/O/temperature telemetry, sweep duration/cadence/jitter, вплив reporting і Telegram на acquisition, storage/queue behavior, визначення необхідності process/CPU isolation та evidence-based рішення щодо CM4 capacity і можливої зміни/розділення платформи. Ці можливості ще не реалізовані.

## Future roadmap

RFEvent detection, calibration/reference characterization, IQ capture/classification, multi-SDR/HackRF, retention/production hardening, а також actual/reference source measurement, LibreVNA raw/reference receiver, correction application, normalization/interpolation, cable/source corrections та uncertainty/stability work. Characterization не є calibrated measurement system.

Lifecycle policy: `.local*` / `.local-validation/` — disposable local validation; runtime data — operational; canonical characterization/calibration datasets — long-lived і не disposable.

## CLI

`station` — canonical integrated runtime; `acquire` — standalone acquisition; `report-schedule` — standalone report scheduling; `survey`/`schedule` — legacy/compatibility. Без mode `python -m rf_sentinel` має identity-only behavior.
