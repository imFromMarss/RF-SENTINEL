# Локальний survey prototype

Реалізований pipeline: `RTL-SDR → rtl_power → normalized spectrum → report/PNG → Telegram`.
Це diagnostic survey, без RFEvent, detection, inbound commands чи SDR control через Telegram.
Майбутній backend реалізує `SpectrumScanner` і повертає `ScanResult`: reporting та transport
не залежать від CSV або конкретного SDR.

## Встановлення та запуск

Потрібен Python 3.12+. Встановлення Python dependencies:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m rf_sentinel
```

Остання команда, як і раніше, друкує `RF Sentinel` та завершується без hardware/network.
`rtl_power` встановлюється окремо як host dependency, наприклад із пакета RTL-SDR.
Наявність executable не означає доступності USB-пристрою.

Application читає лише environment; `.env` автоматично не завантажується.
`.env.example` містить безпечний приклад. Для локального запуску без Telegram:

```sh
export RF_SENTINEL_TELEGRAM_BOT_TOKEN=
export RF_SENTINEL_TELEGRAM_CHAT_ID=
export RF_SENTINEL_REPORT_INTERVAL_MINUTES=30
export RF_SENTINEL_RTL_DEVICE_INDEX=0
export RF_SENTINEL_RTL_GAIN=auto
export RF_SENTINEL_DATA_DIR=runtime
.venv/bin/python -m rf_sentinel survey
.venv/bin/python -m rf_sentinel schedule
```

`survey` виконує один scan. `schedule` виконує перший scan одразу, потім чекає задану
кількість хвилин **після завершення** попередньої спроби. Scans послідовні, без overlap
або catch-up. Ctrl-C завершує application; активний child process примусово зупиняється
і reap-иться. Systemd/timers поки не інтегровані.

Для Telegram заповніть обидві credentials через локальне захищене environment.
Не записуйте справжні значення в документацію, shell-команди для спільного review або Git.
Порожні обидва поля вимикають outbound transport; лише одне заповнене поле — configuration error.
Підтримуються числовий chat ID або `@channel_username`.

| Параметр | Default | Межі |
| --- | --- | --- |
| `RF_SENTINEL_REPORT_INTERVAL_MINUTES` | `30` | Ціле 1–1440 |
| `RF_SENTINEL_RTL_DEVICE_INDEX` | `0` | Ціле 0–255 |
| `RF_SENTINEL_RTL_GAIN` | `auto` | `auto`, порожнє або finite 0–50 dB |
| `RF_SENTINEL_DATA_DIR` | `runtime` | Непорожній шлях; custom directory потребує власного ignore rule |

Requested gain може бути округлений драйвером до підтримуваного значення.
FM profile — `88–108 MHz`, максимальна ширина bin `125 kHz`, integration `10 s`,
scan duration `30 s`. Параметри — immutable `ScanProfile` application layer.
Backend має додатковий timeout `15 s`, CSV limit `16 MiB` і parser limit `250000` values.
Він не приймає command, executable, додаткові flags чи shell text із configuration.
Child environment містить тільки `PATH`, `TZ=UTC`, `LC_ALL=C`; stderr відкидається.

## Артефакти та помилки

У data directory зарезервовані чотири application-owned файли:

- `spectrum.json` — останній normalized scan, profile і timestamps;
- `report.json` — структурований diagnostic report;
- `report.txt` — текст для локального читання і notification;
- `waterfall.png` — frequency/time plot із power intensity.

Наступний survey замінює ці файли: довгострокового архіву тут немає.
Failed scan записує report зі статусом `scan_failed`, без peak та старого waterfall.
Неповні, неузгоджені або non-finite CSV measurements відхиляються; це не «тихий спектр».
Parser об'єднує tuning hops за UTC timestamp, перевіряє geometry і враховує дубль
останнього bin у CSV Osmocom. Peak frequency — центр bin. Power — **некалібровані dB**;
це не підтверджене абсолютне значення dBm. Sample count — сума поля samples CSV rows,
а не кількість power values або незалежних фізичних IQ samples.

Waterfall використовує фактичні timestamps integration ends; Y axis — секунди від початку
scan. Для першого рядка початок інтервалу оцінюється через requested integration time.
PNG працює через headless Agg backend; graphical desktop не потрібен.

Transport використовує standard-library HTTPS із timeout `15 s`, обмеженим response size
та без redirects/retries. Non-2xx, Telegram `ok=false`, invalid JSON та network errors
перетворюються на фіксовані safe errors без URL/token/body. Реалізовані тільки `send_message`
і `send_photo`; scanner imports у transport відсутні. Локальні artifacts записуються перед
notification. Delivery failure повертає `notification_status=failed`, не зупиняє scheduler
і не видаляє локальний scan. Автоматичної черги повторного надсилання поки немає.

`survey` повертає 0 після успішного scan навіть при Telegram failure, 1 при scan/configuration/
artifact error, 130 при Ctrl-C. Підсумок stdout окремо показує стан Telegram.
Scheduler записує безпечні статуси та повторює спробу після інтервалу.

## Перевірки

```sh
.venv/bin/python -m pytest -q
```

За замовчуванням tests не використовують SDR або Telegram network.
Hardware test запускається **окремо**, коли під'єднаний вільний RTL-SDR:

```sh
RF_SENTINEL_TEST_HARDWARE=1 .venv/bin/python -m pytest -q -m hardware
```

Цей test виконує реальний FM scan default device 0, без Telegram.
Для не-0 device використовуйте `survey` з відповідним environment.

## Межі prototype

Поки запускайте лише один RF Sentinel process для одного SDR. Повний Device Manager,
міжпроцесні reservations, hotplug recovery, production retention, транзакційне збереження
групи artifacts та Linux ARM64 endurance verification — наступні етапи. Окремі файли
замінюються атомарно, але вся група не є транзакцією при process/power failure.
Історичні architecture documents описують цільову систему, а не повністю реалізовані capabilities.

Єдина пряма runtime Python dependency — `matplotlib`: потрібні headless PNG, підписані осі,
frequency/time geometry та colorbar. HTTP/config/scheduler використовують standard library.

Формати звірені з [Osmocom rtl_power source](https://github.com/osmocom/rtl-sdr/blob/master/src/rtl_power.c)
та [Telegram Bot API](https://core.telegram.org/bots/api).
