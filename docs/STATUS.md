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
