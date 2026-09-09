# RF Sentinel

RF Sentinel — автономний сервер моніторингу RF-спектра, що працює лише на приймання (RX-only), для систем на базі Raspberry Pi Compute Module 4. Він призначений для безперервного спостереження за налаштованими діапазонами спектра, збереження історії спостережень і надання hourly/daily та on-demand звітів через Telegram-бота.

Проєкт зосереджений на спостереженні за спектром і визначенні характеристик сигналів. Він не призначений для розшифрування захищених комунікацій, обходу контролю доступу або перехоплення вмісту приватних комунікацій.

## Стан проєкту

Проєкт перебуває на етапі **LOCAL SURVEY PROTOTYPE / architecture review**. Див. [визначення проєкту](docs/PROJECT.md) і [поточний стан](docs/STATUS.md).

## Межі першого прототипу

Поточний прототип створює безпечну й відтворювану основу для спостереження без постійної участі оператора. Canonical deployment entrypoint — `python -m rf_sentinel station`: один RF Sentinel process об'єднує continuous acquisition, async SQLite persistence, report scheduler і Telegram inbound/outbound та запускає supervised `rtl_power` child processes. Raspberry Pi/systemd deployment ще не завершений, а частина production-рішень і hardware validation залишається предметом review.

## Цільове середовище

- Raspberry Pi Compute Module 4
- Linux ARM64
- Робота лише на приймання (RX-only)
- SDR-пристрої RTL-SDR і HackRF

## Документація

- [Визначення проєкту](docs/PROJECT.md) — місія, межі, те, що не входить до цілей, і початкова термінологія.
- [Системні вимоги](docs/REQUIREMENTS.md) — нормативна поведінка й очікування щодо приймання.
- [Стан проєкту](docs/STATUS.md) — поточний етап і стан review.
- [Мовна політика](docs/LANGUAGE_POLICY.md) — мова документації, технічна термінологія та правила поступового перекладу.
- [Початкова архітектура системи](docs/ARCHITECTURE.md) — прийняті межі відповідальності, компоненти й потоки.
- [Архітектурні рішення](docs/DECISIONS.md) — записи рішень та їхні статуси.
- [Архітектурні питання](docs/ARCHITECTURE_QUESTIONS.md) — невирішені питання й необхідні підтвердження.
- [Архітектура SDR-пристроїв](docs/design/SDR_DEVICE_ARCHITECTURE.md) — детальний дизайн Device Manager і Acquisition Adapter, що наразі проходить review.
- [Дослідження наявних рішень](docs/research/EXISTING_SOLUTIONS.md) — оцінка відповідних SDR-підходів, яка не встановлює обов'язкових рішень.
- [Технологічні кандидати](docs/research/TECHNOLOGY_CANDIDATES.md) — реєстр варіантів і можливих напрямів, який не встановлює обов'язкових рішень.
- [Експерименти з обладнанням CM4](docs/research/EXPERIMENTS.md) — заплановані перевірки для обґрунтування рішень, залежних від обладнання та продуктивності.

## Canonical station runtime

Запуск production-like topology:

```sh
python -m rf_sentinel station
```

`station` утримує process-wide exclusive lock у `DATA_DIR/station.lock`. Другий instance
завершується fail-fast із safe diagnostic і не запускає acquisition, report scheduler або
Telegram polling. Per-SDR lock залишається окремою hardware safety boundary.

## Локальні та diagnostic режими

Доступні standalone/diagnostic контури: `python -m rf_sentinel acquire`
(`rtl_power → SQLite`) та `python -m rf_sentinel report-schedule`
(`SQLite → report.json/report.txt/waterfall.png/heatmap.png → Telegram`).
Вони не є цільовою production deployment topology.
`python -m rf_sentinel` зберігає попередню identity-only поведінку; режими `survey`
і `schedule` запускають одноразовий або continuous application workflow. Налаштування,
історичні артефакти, logging, recovery, локальні команди та hardware-free tests описані
в [LOCAL_SURVEY.md](docs/LOCAL_SURVEY.md).

## Continuous acquisition

`python -m rf_sentinel station` запускає canonical unified runtime. `python -m rf_sentinel acquire`
залишається незалежним RX producer без Telegram і report generation для standalone/diagnostic use.
Hardware benchmark показав, що full-range cadence ≤10 с через `rtl_power` недосяжний:
practical full-range baseline — 24–1766 МГц із cadence budget 60 с.
Sweep-и та bounded incident history зберігаються persistent у SQLite, а logs і atomic health snapshot доповнюють observability.
Конфігурація, діагностика та обмеження: [Continuous acquisition](docs/CONTINUOUS_ACQUISITION.md).
