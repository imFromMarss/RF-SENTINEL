# RF Sentinel — Стан проєкту

**Поточний етап:** PRODUCTION-LIKE OPERATIONAL BASELINE / release checkpoint

## Завершено

- Ініціалізація репозиторію
- Визначення проєкту
- Розроблення вимог
- Дослідження наявних рішень
- Дослідження технологічних кандидатів
- Початкова архітектура системи

## Поточна робота

- Реалізовані локальні контури: незалежний continuous acquisition, `rtl_power`
  scanner/parser, normalized spectrum, persistent SQLite storage, report/PNG,
  calendar report scheduler та outbound/inbound Telegram boundary.
  Запуск і обмеження: [LOCAL_SURVEY.md](LOCAL_SURVEY.md).
- Stage 6A–6C сформували canonical `station` runtime: один process із internal
  acquisition, async SQLite persistence, report scheduler, Telegram boundary та
  process-wide lock `DATA_DIR/station.lock`. Standalone `acquire` і
  `report-schedule` залишаються diagnostic modes.
- Звичайні tests hardware-free; реальні SDR/Telegram та Linux ARM64 verification
  залишаються окремими integration/host перевірками.

## Production-like operational baseline (2026-09-11)

Canonical runtime — `python -m rf_sentinel station`. CM4/systemd deployment,
boot lifecycle, unattended acquisition понад 24 години, стабільність SQLite
persistence/geometry, reporting і Telegram delivery validated. Acquisition
продовжує 60-секундний cadence незалежно від report generation.

Початковий baseline: **PASS**.

Під час 24h report validation старий reporting path мав GB-scale memory
amplification і спричиняв OOM на CM4: великі datasets materialize-ились у SQLite,
Python, JSON та Matplotlib одночасно. Reporting path переведено на streaming
SQLite reads, compact payloads, temporary snapshots, streaming JSON і bounded
percentile/render processing. На CM4 після fix report успішно обробив 1392 sweeps
із піковим RSS приблизно 90 MiB, без OOM; concurrent smoke test підтвердив, що
acquisition продовжується під час report generation.

Operational trade-off: 24h report на CM4 може формуватися приблизно 12–13 хвилин
і використовувати близько 714 MiB temporary disk space. Це прийнятно, оскільки
acquisition не блокується; deployment потребує достатнього disk headroom.

- Review архітектури SDR-пристроїв (SDR Device Architecture). Проєкт документа підготовлено й розміщено в `docs/design/SDR_DEVICE_ARCHITECTURE.md`; його наявність не означає формального прийняття.

## Наступний крок

- Окремо: PR/review та подальші deployment milestones. Цей release checkpoint
  не створює PR і не виконує deployment.

## Stage 1: незалежний acquisition (2026-09-09)

Локально реалізовано `acquire`, sweep model, SQLite MeasurementSink, cadence/recovery,
logs, bounded incident history і atomic health snapshot. Hardware-free перевірки пройшли.
Server-side RTL2838UHIDIR/R820T benchmark підтвердив coverage 24–1766 МГц і показав,
що target ≤10 с недосяжний через 623+ послідовних tuner hops. Operational cadence
budget встановлено 60 с. Виміряні нижні межі:
>30.22 с для 250 кГц, >40.21 с для 500 кГц, >72 с для 1000 кГц.
Рекомендований full-range budget — 60 с при 250 кГц; точний natural completion
не вимірювався через обмеження тривалості benchmark. Hourly report запускається на
початку кожної години, daily report — о 00:00; default timezone — `Europe/Kyiv`.
Telegram доставляє автоматичні reports і дві PNG (`waterfall.png`, `heatmap.png`),
а authorized users можуть запросити `📊 Звіт за останню годину`. Acquisition не
зупиняється під час report generation. Raspberry Pi/systemd operational validation
пройдено для цього baseline.
Деталі: [Continuous acquisition](CONTINUOUS_ACQUISITION.md).
