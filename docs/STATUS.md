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

- Реалізований локальний diagnostic survey pipeline: configuration, outbound Telegram,
  `rtl_power` scanner/parser, normalized spectrum, report/PNG та application scheduler.
  Запуск і обмеження: [LOCAL_SURVEY.md](LOCAL_SURVEY.md).
- Звичайні tests hardware-free; реальні SDR/Telegram та Linux ARM64 verification
  залишаються окремими integration/host перевірками.

- Review архітектури SDR-пристроїв (SDR Device Architecture). Проєкт документа підготовлено й розміщено в `docs/design/SDR_DEVICE_ARCHITECTURE.md`; його наявність не означає формального прийняття.

## Наступний крок

- Завершити review архітектури SDR-пристроїв та перевірити FM survey на реальному RTL-SDR.

## Stage 1: незалежний acquisition (2026-09-09)

Локально реалізовано `acquire`, sweep model, MeasurementSink, cadence/recovery,
structured rotation logging і atomic health snapshot. Hardware-free перевірки пройшли.
Server-side RTL2838UHIDIR/R820T benchmark підтвердив coverage 24–1766 МГц і показав,
що target ≤10 с недосяжний через 623+ послідовних tuner hops. Виміряні нижні межі:
>30.22 с для 250 кГц, >40.21 с для 500 кГц, >72 с для 1000 кГц.
Рекомендований full-range budget — 60 с при 250 кГц; точний natural completion
не вимірювався через обмеження тривалості benchmark. Deployment/systemd не змінювалися.
Деталі: [Continuous acquisition](CONTINUOUS_ACQUISITION.md).
