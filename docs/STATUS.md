# RF Sentinel — поточний стан

## Implemented / stable

Canonical runtime — `python -m rf_sentinel station`. Один process запускає acquisition, report scheduler і, за наявності credentials, Telegram polling; station lock у `DATA_DIR/station.lock` не дозволяє другий instance.

Stable behavior включає:

- continuous RTL-SDR acquisition через supervised `rtl_power` з default `ACQUISITION_BIN_HZ=500000`, persistent SQLite sweeps і bounded recovery;
- окремі acquisition/reporting storage boundaries та bounded deterministic shutdown;
- hourly/daily calendar reports, half-open windows, persisted-bin coverage, gaps і failed rows у timeline;
- спільний `ReportData` для `report.json`, `report.txt`, `waterfall.png` і `heatmap.png`; canonical pixel-to-pixel waterfall, canonical time-slot grid і black missing rows; Keenerd-compatible heatmap;
- allowlisted Telegram read-only report request із delivery як document/file, health snapshot, structured logs і bounded incident history;
- bounded RTL-SDR baseline characterization, CW point characterization, resumable CW matrix та frequency-accuracy/tuner diagnostics;
- resource sampling/aggregates, cadence/jitter/missed-slot counters, queue/persistence telemetry, report/Telegram phase timing and overlap metrics у health/run summary;
- one shared bounded shutdown deadline, non-clean exit code `1` on component/finalization failure, and station-lock retention while any unresolved critical runtime survives.

Software checkpoint: generic Architecture & Code Health cleanup завершений у HEAD `740ca6c191c6ac7d31c0ee6fab737562d4bf72e6`. Це завершений software state; подальший generic cleanup не продовжується без нового evidence.

Terminal acquisition semantics are explicit: successful sweeps carry measured RF payload; retryable acquisition failures persist as correlated `failed` sweep metadata with no RF payload; missing slots have no sweep row; persistence failure is a separate storage/lifecycle failure and never manufactures a failed RF attempt. Reports therefore distinguish failed attempts by failed rows and missing slots by absent rows/gaps.

### Characterization persistence / resume

`results.json` є canonical checkpoint; logical point identities lossless/stable для current schema. Incompatible або corrupted checkpoints fail-fast до preflight/SCPI/capture/cleanup, тоді як valid partial runs resumable. Complete resume відновлює `results.csv` і `summary.md` із canonical `results.json`; regeneration derived artifacts не запускає новий RF capture. Calibration artifact model — immutable/self-identifying JSON artifact із versioned schema, receiver/profile/provenance, raw/reference dataset links, point-level corrections і забороненими interpolation/extrapolation; model описує artifact contract, а не завершену absolute calibration.

### Validation baseline

Current working-tree hardware-free suite: **577 passed, 2 skipped**. Skips:

- real RTL-SDR requires explicit opt-in `RF_SENTINEL_TEST_HARDWARE=1`;
- 30-minute full-range hardware test requires explicit opt-in `RF_SENTINEL_TEST_FULL_RANGE=1`.

CM4 deployment/acquisition/reporting validation описана як external operational context для цього baseline. Цей audit не підтверджує, що HEAD `740ca6c` уже deployed на CM4. Repository не містить unit як reproducible deployment artifact.

## Validated externally

Практична CM4 перевірка підтвердила запуск foreground station topology, довготривалу acquisition/report coexistence та bounded-memory report path. Під час великого report generation acquisition продовжується; temporary storage може бути суттєвим, тому потрібен disk headroom.

Точні hardware-specific throughput/temperature/endurance межі не є універсальним stable contract. Їх слід читати як recorded validation для конкретного host, SDR, tool і profile.

## Legacy / compatibility

`survey` і `schedule` — старий workflow, залишений для сумісності. `acquire` і `report-schedule` — standalone/diagnostic контури; canonical topology — `station`. [LOCAL_SURVEY.md](LOCAL_SURVEY.md) зберігає корисні операційні деталі, але явно не є current source of truth.

## Capacity work still open

Observability instrumentation вже реалізована. Відкритим лишається evidence-based capacity verdict на representative CM4 workload і подальше рішення щодо process/CPU isolation чи platform split. Hardware-specific limits не є universal contract.

## Open hardware evidence

RTL-SDR/`rtl_power` reliability incident — **OPEN / DEFERRED**, pending new hardware evidence. Incident не resolved і не закривається hardware-free tests. Він не є blocker для завершеного generic software cleanup; подальші generic architecture changes не починаються без нового evidence.

## Canonical ordered roadmap

1. Measurement Runtime Architecture
2. ReceiverSlot + Device Manager
3. Hotplug / reconnect / hot-replace
4. Critical / non-critical service split
5. Mixed-criticality temporal scheduling
6. CM4 capacity / resource validation
7. RTL-SDR response characterization
8. Relative correction / calibration semantics
9. RF frontend architecture
10. Optional dual-SDR pilot
11. Measurement Contract Freeze
12. Baseline
13. ActivitySegment
14. RFEvent

## Future roadmap

RFEvent detection, calibration/reference characterization, IQ capture/classification, multi-SDR/HackRF, retention/production hardening, а також actual/reference source measurement, LibreVNA raw/reference receiver, correction application, normalization/interpolation, cable/source corrections та uncertainty/stability work remain roadmap/evidence-gated. RFEvent не є найближчим roadmap item. Characterization не є calibrated measurement system.

Lifecycle policy: усі `.local*` paths — disposable local validation. Canonical characterization/calibration datasets є long-lived source records і мають зберігатися поза `.local*`; runtime DB/status/log/report artifacts — operational і підлягають deployment backup/retention. Existing datasets у цьому cleanup не переміщувалися.

## CLI

`station` — canonical integrated runtime; `acquire` — standalone acquisition; `report-schedule` — standalone report scheduling; `survey`/`schedule` — legacy/compatibility. Без mode `python -m rf_sentinel` має identity-only behavior.

`RF_SENTINEL_REPORT_INTERVAL_MINUTES`, `HealthState`/`write_status`, `SQLiteSweepStore` та renderer/schema aliases збережені як compatibility surfaces. Fixed report interval не керує current calendar scheduler. Unreferenced internal `scheduler.run_schedule` видалено; legacy CLI продовжує використовувати `run_continuous`.
