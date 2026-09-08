# Локальний огляд спектра

RF Sentinel виконує RX-only pipeline:

`RTL-SDR → rtl_power → parser → звіт → heatmap → Telegram → наступний огляд`.

Це діагностичне широкосмугове спостереження. Воно не визначає `RFEvent`, не декодує
вміст і не приймає команди з Telegram. Transport залишається лише вихідним.

## Робочий профіль

Для підключеного `RTL2838UHIDIR` з tuner `R820T` локальна `librtlsdr 2.0.3`
підтвердила налаштування від `24 MHz` до `1766 MHz` без direct sampling. Перевірка
включала обидві межі та точки через 10 MHz; driver не повернув помилок або PLL warning
після початкового калібрування.

Default profile:

| Параметр | Значення |
| --- | --- |
| Початок | `24 000 000 Hz` |
| Кінець | `1 766 000 000 Hz` |
| Максимальна запитана ширина комірки | `500 000 Hz` |
| Інтервал накопичення | `60 s` |
| Тривалість огляду | `1800 s` |

На локальному hardware один контрольний full-range sweep тривав приблизно `23,4 s`.
Поточний `rtl_power` обрав близько 4984 комірок по `349,518 kHz`. За 30 хвилин
очікується приблизно 30 часових зрізів. Фактичні межі, ширина комірки й кількість
проходів записуються в кожному `report.json`; requested values — у `request.json`.

Ці межі є перевіреним профілем поточного tuner, але вони не зашиті в reporting layer.
Інший backend або діапазон передається через `ScanProfile` та environment configuration.

## Heatmap

`heatmap.png` відображає:

- X — частоту в МГц;
- Y — локальний час у `RF_SENTINEL_TIMEZONE`;
- один горизонтальний рядок — один завершений sweep/time slice;
- колір — виміряний рівень потужності.

Слабкі рівні темні, далі йдуть фіолетовий, червоний, помаранчевий, жовтий і світлий.
Візуалізація не інтерполює комірки й не створює проміжні RF-дані. Межі кольорів
визначаються детермінованими percentile P2/P98 для конкретного огляду: одиничний
сильний transmitter не приховує решту спектра. Це змінює лише кольори. Peak у звіті
завжди обчислюється з raw values.

Рівні dB від `rtl_power` не калібровані й придатні для відносного порівняння в межах
узгодженого setup. Вони не є підтвердженим абсолютним dBm.

## Історичні артефакти

Кожен запуск створює окремий каталог:

```text
runtime/surveys/YYYYMMDDTHHMMSS.ffffffZ-xxxxxxxx/
├── request.json
├── spectrum.csv
├── spectrum.json
├── report.json
├── report.txt
├── heatmap.png
├── rtl_power.stderr.txt
├── scan-diagnostics.json
└── delivery.json
```

Каталоги не перезаписуються. `spectrum.csv` — raw output `rtl_power`; `report.json`
має стабільні machine-readable keys; `report.txt` — український текст для людини;
`delivery.json` зберігає статус і Telegram message IDs. При помилці acquisition
залишаються request, доступний raw CSV, bounded stderr, machine-readable diagnostics та
failed report. `scan-diagnostics.json` фіксує reason і subprocess return code. При
помилці Telegram локальні дані й heatmap зберігаються.

`runtime/status.json` містить поточний process state та лічильники. Operational log:
`runtime/logs/rf-sentinel.log`. Увесь `runtime/` і `.env` ігноруються Git.

## Continuous operation і відмови

Режим `schedule` запускає перший 30-хвилинний survey одразу. Після успішного report і
delivery наступний survey починається одразу; scans не перекриваються. Отже нормальний
темп — приблизно два звіти на годину плюс час rendering/network.

Telegram message та heatmap мають окремий bounded retry: default три спроби з паузою
5 секунд. Після вичерпання спроб monitoring продовжується. Failed SDR/parser/artifact
cycle збільшує health counters і використовує 60-секундний recovery delay, щоб уникнути
tight retry loop. Один failed cycle не завершує process.

Перший failed cycle у послідовності надсилає один Telegram alert про автоматичне
відновлення. Однакові alerts для наступних consecutive failures пригнічуються й
залишаються лише в локальному log. Перший успішний survey після failure state надсилає
окреме повідомлення про відновлення, після чого normal report delivery продовжується.

`status.json` показує application start, останній start survey, останній успішний survey,
останню успішну Telegram delivery, послідовні помилки та загальні лічильники.
Логи мають timestamp, severity і component name; credentials, Telegram URL та response
body не логуються.

Ctrl+C і `SIGTERM` переривають активний `rtl_power`; child process kill/reap виконується
в `finally`. Новий cycle після сигналу не запускається. Exit status — 130.

## Configuration

Application читає environment. `.env.example` — безпечний tracked приклад; `.env` не
завантажується автоматично, тому локально його потрібно експортувати в process:

```sh
set -a
source .env
set +a
```

Основні поля:

- `RF_SENTINEL_SURVEY_START_HZ`, `RF_SENTINEL_SURVEY_STOP_HZ`;
- `RF_SENTINEL_SURVEY_BIN_HZ`, `RF_SENTINEL_SURVEY_INTEGRATION_SECONDS`;
- `RF_SENTINEL_SURVEY_DURATION_SECONDS`;
- `RF_SENTINEL_RTL_DEVICE_INDEX`, `RF_SENTINEL_RTL_GAIN`;
- `RF_SENTINEL_TELEGRAM_BOT_TOKEN`, `RF_SENTINEL_TELEGRAM_CHAT_ID`;
- `RF_SENTINEL_TELEGRAM_ATTEMPTS`, `RF_SENTINEL_TELEGRAM_BACKOFF_SECONDS`;
- `RF_SENTINEL_SURVEY_RECOVERY_DELAY_SECONDS`, `RF_SENTINEL_TIMEZONE`;
- `RF_SENTINEL_DATA_DIR`.

`RF_SENTINEL_REPORT_INTERVAL_MINUTES=30` збережено для сумісності з раннім prototype;
continuous runner визначає cadence тривалістю survey й запускає наступний успішний cycle
одразу.

## Запуск і перевірки

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m rf_sentinel survey
.venv/bin/python -m rf_sentinel schedule
```

Без аргументів `python -m rf_sentinel` зберігає identity-only поведінку й не торкається
hardware або network. Default pytest hardware-free. Окремі opt-in перевірки:

```sh
RF_SENTINEL_TEST_HARDWARE=1 .venv/bin/python -m pytest -q -m hardware
RF_SENTINEL_TEST_FULL_RANGE=1 .venv/bin/python -m pytest -q tests/test_hardware.py::test_real_full_range_survey
```

Другий command займає 30 хвилин і не надсилає Telegram. Для повного operational test
краще використовувати `python -m rf_sentinel schedule`, бо він перевіряє artifacts,
delivery, retries, health та logging разом.

Локальний foreground process надалі інтегрується з production supervision через
`systemd` на Raspberry Pi CM4. Deployment, `systemd` і будь-які дії на Raspberry Pi
до цього етапу не входять.

Формат CSV і поведінку sweep звірено з
[Osmocom rtl_power](https://github.com/osmocom/rtl-sdr/blob/master/src/rtl_power.c).
Візуальну концепцію порівняно з
[keenerd heatmap](https://github.com/keenerd/rtl-sdr-misc/tree/master/heatmap), але код
RF Sentinel реалізовано самостійно. Delivery відповідає
[Telegram Bot API](https://core.telegram.org/bots/api).
