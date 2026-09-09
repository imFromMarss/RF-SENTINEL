# Continuous acquisition

Stage 1 додає незалежний producer: `RTLPowerScanner.acquire` →
`SpectrumAcquisitionWorker` → `MeasurementSink.store_sweep`.
Acquisition не імпортує Telegram або reporting, не будує PNG і не чекає звітів.
`survey` та `schedule` залишаються попередніми workflows; нова цільова точка входу —
`python -m rf_sentinel acquire`. Не запускайте їх одночасно для одного SDR.

`SpectrumSweep` — canonical durable record одного acquisition attempt. Він має
`schema_version`, `sweep_id`, `sequence`, timestamps, optional `correlation_id`,
requested/actual profile, device identity, backend/tool version, coverage/quality
та terminal `status`: `success`, `partial` або `failed`. `error_classification`
зберігає стабільну категорію/код, а не raw exception text.

`success` вимагає повний spectrum payload, complete coverage і valid quality.
`partial` зберігає record із degraded quality та partial coverage; payload може
бути неповним. `failed` є durable attempt metadata contract навіть без payload:
coverage=`none`, quality=`unavailable` і обов’язкова error classification. Це
відрізняє відсутність покриття від тихого спектра. `schema_version` — explicit
compatibility boundary (`spectrum-sweep.v1`); storage engine не є частиною цього
контракту. CSV залишається форматом адаптера `rtl_power`, а не domain contract.

## Прийом і cadence

Backend запускає RX-only `rtl_power -f START:STOP:BIN -i 1 -1 -d INDEX -`.
Не застосовуються direct sampling, offset tuning, bias-T або experimental options.
Одноразовий процес передає лише один завершений CSV frame; parser відхиляє
неузгоджені комірки, неповне покриття, NaN та декілька frames.
`-i 1` задає мінімальний інтервал накопичення, а не обіцянку проходу за секунду.
За [реалізацією Osmocom](https://github.com/osmocom/rtl-sdr/blob/master/src/rtl_power.c)
single-shot завершується після циклу сканування й виведення frame.

`ACQUISITION_CADENCE_BUDGET_SECONDS` — configured operational budget між початками
проходів; default 60 секунд для full-range baseline. Це не прогноз фактичної тривалості
sweep: worker враховує весь цикл. Якщо sweep триває довше budget, наступний прохід
починається відразу після нього.
Overlap та catch-up відсутні. `flock` на локальному lock-файлі для device index
також виключає другий scanner RF Sentinel; сторонні SDR-програми цим lock не керуються.
Пристрій відкривається заново для кожного проходу; цей overhead входить у benchmark.
Safety timeout одноразового subprocess — 90 секунд, CSV ≤64 MiB, stderr ≤1 MiB.

Runtime sink має швидко приймати frame без rendering/reporting. Production `acquire`
використовує persistent `SQLiteMeasurementSink` у `runtime/sweeps.sqlite3`: один
SQLite row на sweep, payload і metadata з reopen support. `SQLiteReportEngine` читає
це сховище окремо, тому generation reports не зупиняє acquisition.
`AsyncMeasurementSink` має
bounded in-process queue і один non-daemon writer: acquisition чекає лише bounded
`enqueue_timeout`, а не latency storage. Результат hand-off має чотири стани:
`accepted` означає ownership queue (не durability), `persisted` — downstream підтвердив
запис, `rejected` — queue/стан sink не прийняв frame, `failed` — downstream не зміг
записати frame. Queue-full збільшує explicit rejection counter; writer failure є
sticky і піднімається через `flush`/`close`, тому не стає тихою втратою.

`flush` чекає завершення всіх accepted frames і помиляється, якщо будь-який не став
`persisted`. `close` спочатку drains queue, закриває downstream і зупиняє writer;
після process crash queued-but-not-persisted frames не вважаються durable й можуть
бути втрачені. `LatestSweepSink` залишається lightweight RAM implementation для
hardware-free tests, але не production storage.

## Відмови та завершення

SDR/parse failure збільшує counters, записує безпечний reason/exit code та переводить
стан у `recovering`. Повтор — через configurable delay (default 60 с), без tight retry.
Перший успіх скидає consecutive counter і створює `recovery_success` event.
Ctrl+C/SIGINT та SIGTERM переривають також recovery/cadence wait, забороняють новий
прохід і завершують child: terminate → до 2 с очікування → kill → reap.
Обидва сигнали повертають exit code 130; звичайне завершення — 0, відмова — 1.
Обробники сигналів відновлюються, logging закривається.

## Конфігурація

Environment читає тільки `Settings`; `.env` автоматично не завантажується.
Безпечний перелік змінних є в `.env.example`. Режим `acquire` ігнорує Telegram,
report та legacy survey configuration, включно з некоректними credentials.

| Змінна `RF_SENTINEL_…` | Default |
|---|---:|
| `ACQUISITION_START_HZ` | 24000000 |
| `ACQUISITION_STOP_HZ` | 1766000000 |
| `ACQUISITION_BIN_HZ` | 500000, application default |
| `ACQUISITION_CADENCE_BUDGET_SECONDS` | 60 |
| `ACQUISITION_RECOVERY_SECONDS` | 60 |
| `DATA_DIR` | runtime |
| `LOG_MAX_BYTES` | 5000000 |
| `LOG_BACKUPS` | 3 |

Також застосовуються `RTL_DEVICE_INDEX`, `RTL_GAIN`.
Межі 24–1766 МГц підтверджені benchmark для під’єднаного RTL2838UHIDIR/R820T.
Практичний current full-range baseline використовує 250 kHz requested bin і
60-секундний cadence budget.

## Observability

`runtime/logs/rf-sentinel.log` — JSON Lines з українськими повідомленнями,
англійськими keys/events, часом, duration, bins, effective range, metadata,
startup/configuration/backend, recovery та shutdown. Size rotation: 5 000 000 bytes,
3 backups (приблизно 20 MB загалом). Журнал не містить raw power arrays, credentials,
довільних текстів exception або external URLs. Scanner передає child тільки PATH,
TZ та LC_ALL. Нові acquire events використовують явний whitelist контексту.

`runtime/status/health.json` містить application status, started_at, timestamps
останнього початку/успішного завершення, останню duration, total_sweeps (успішні плюс
невдалі завершені спроби), failed_sweeps, consecutive_sweep_failures, backend,
configured/actual range та bin width, cadence budget/recovery intervals, bin count і безпечну
останню помилку. Перерваний сигналом прохід не рахується завершеним.
`cadence_budget_seconds` — configured budget; `last_sweep_duration_seconds` — фактична
тривалість останньої спроби; `last_sweep_cadence_seconds` — фактично виміряний
інтервал між початками поточного та попереднього проходу.
Поточну тривалість читач оцінює від `last_sweep_started_at`, коли status=`acquiring`.
Оновлення: temporary file у тому самому каталозі → atomic replace; це захист від
half-written JSON, а не гарантія durability після втрати живлення.

Health, JSON-lines logs і bounded SQLite incident history (default 1000 records)
утворюють поточний observability baseline. Runtime artifacts Git ignored.

## Короткий benchmark і поточне обмеження

Команда після hardware-free checks: `.venv/bin/python scripts/benchmark_acquisition.py`.
Три послідовні candidates: 250, 500, 1000 кГц; жодних довгих continuous tests.
Кожен subprocess має safety timeout. CSV, stderr і `results.json` залишаються в
`runtime/benchmarks/<UTC>/`. Nonzero exit benchmark означає невдалі trials.

2026-09-08/09 benchmark виконано на Raspberry Pi ARM64 з RTL2838UHIDIR/R820T та
Ubuntu `rtl-sdr`/`librtlsdr2` 2.0.1. Усі проходи були послідовними; overlap і сторонніх
SDR-процесів не було. Один початковий `PLL not locked` виникав у кожному запуску,
після чого tuner працював у всьому requested range без повторних PLL warnings.

| Range | Requested bin | Effective bin | Hops | Bins | Elapsed | CSV | Exit |
|---|---:|---:|---:|---:|---:|---:|---:|
| 24–1766 МГц | 250 кГц | 174.76 кГц | 623 | 9968 | >30.22 с | 108546 B | 124 |
| 24–1766 МГц | 500 кГц | 349.52 кГц | 623 | 4984 | >40.21 с | 79045 B | 124 |
| 24–1766 МГц | 1000 кГц | 699.04 кГц | 1742 | 1742 | >72.00 с | 0 B | 137 |

250 і 500 кГц записали complete-range frame від 24 МГц до 1765.99958 МГц під час
timeout shutdown, але subprocess не завершився природно. Тому elapsed — нижня межа,
а не фактична стала cadence. 1000 кГц не завершив scan pass і не записав frame.
Machine-readable результати та raw diagnostics: `runtime/benchmarks/20260908T191649Z/`.

Target ≤10 с для одного full-range RTL-SDR через `rtl_power` недосяжний у перевіреній
конфігурації. Bottleneck — послідовне перестроювання тюнера: 250/500 кГц потребують
623 hops. Для 1000 кГц внутрішня евристика зменшує dongle bandwidth до 1 МГц і
збільшує план до 1742 hops, тому грубіший requested bin тут повільніший.

Практичний full-range компроміс — 250 кГц і operational cadence budget не менше
60 с: він дає вдвічі більше bins за той самий hop count, що й 500 кГц. Це budget,
підтриманий виміряними нижніми межами та попереднім 60-секундним режимом, але не
новий вимір природного завершення. Поточний code default cadence budget — 60 с.
Worker не створює overlap; фактичний cadence визначається тривалістю sweep та
overhead sink/health і записується окремо від configured budget.

Для cadence близько 10 с можна сканувати послідовні вікна приблизно 300–400 МГц;
повне покриття тоді оновлюватиметься раз на 5–6 вікон. Persistent in-process
`librtlsdr` backend прибере process startup, але не усуне сотні tuner retunes та
instantaneous-bandwidth limit. Фундаментально менше hops потребує кількох RTL-SDR
або ширшосмугового hardware backend, наприклад майбутнього HackRF.

SSH використовувався лише для benchmark; deployment та systemd не змінювалися.
Report scheduler і report handler працюють як окремий downstream consumer: hourly report
доставляється на початку кожної години, daily report — о 00:00 у configured timezone
(default `Europe/Kyiv`). Acquisition під час report generation не зупиняється.
