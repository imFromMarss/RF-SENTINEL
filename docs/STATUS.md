# RF Sentinel — Стан проєкту

**Поточний етап:** LOCAL SURVEY PROTOTYPE / architecture review

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

- Review архітектури SDR-пристроїв (SDR Device Architecture). Проєкт документа підготовлено й розміщено в `docs/design/SDR_DEVICE_ARCHITECTURE.md`; його наявність не означає формального прийняття.

## Наступний крок

- Final verification → PR → Raspberry Pi/systemd deployment → soak test.

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
зупиняється під час report generation. Raspberry Pi/systemd deployment ще не завершений.
Деталі: [Continuous acquisition](CONTINUOUS_ACQUISITION.md).
