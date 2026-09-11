# Local survey — legacy/local workflow

> Цей документ описує legacy/local workflows та історичні операційні результати. Він **не є current source of truth для station architecture**. Для stable behavior використовуйте [STATUS.md](STATUS.md), [CONTINUOUS_ACQUISITION.md](CONTINUOUS_ACQUISITION.md) і [DEPLOYMENT.md](DEPLOYMENT.md).

## Legacy commands

`python -m rf_sentinel survey` запускає one-shot `SurveyWorkflow`; `python -m rf_sentinel schedule` — старий continuous workflow. Вони збережені для compatibility. Current canonical runtime — `python -m rf_sentinel station`; current standalone acquisition — `python -m rf_sentinel acquire`.

Legacy profile має `24–1766 MHz`, requested bin `500 kHz`, integration `60 s` і duration `1800 s`. Це не означає current station cadence/default beyond the shared default bin setting; `250 kHz` не є current acquisition default.

## Historical artifacts

Legacy run створює каталог `DATA_DIR/surveys/<timestamp>-<id>/` з `request.json`, raw `spectrum.csv` (якщо доступний), `report.json`, `report.txt`, `spectrum.json`/`heatmap.png` для успішного scan, bounded stderr/diagnostics і `delivery.json`. Каталоги не перезаписуються. Failed scan зберігає diagnostics і failed report; Telegram failure не видаляє локальні artifacts.

## Historical visualization semantics

Legacy `heatmap.png` використовує frequency на X, local time на Y і rows завершених frames; дані не інтерполюються. Colors базуються на P2/P98 конкретного dataset; peak обчислюється з raw values. `rtl_power` levels не calibrated absolute dBm і придатні лише для relative comparison у compatible setup.

Current report renderer має інші durable input semantics: `waterfall` і `heatmap` будуються з одного `ReportData`, який включає failed rows та gaps. Не переносіть assumptions про legacy `spectrum.json`/30-minute workflow на `station`.

## Legacy Telegram retry note

У legacy `SurveyWorkflow` кожна окрема message/photo delivery має до `RF_SENTINEL_TELEGRAM_ATTEMPTS` спроб із `RF_SENTINEL_TELEGRAM_BACKOFF_SECONDS` (default `3` і `5 s`). Це legacy behavior. У current scheduled report path package retry configurable attempts виконується без 5-секундної паузи; current on-demand last-hour request викликає delivery один раз. Telegram polling має окремі transport retries, але це не report-delivery retry.

## Local configuration and checks

`.env` автоматично не завантажується:

```sh
set -a
source .env
set +a
```

Hardware-free validation:

```sh
.venv/bin/python -m pytest -q
```

Expected stable baseline: `186 passed, 2 skipped`. Real RTL-SDR і 30-minute hardware test запускаються лише explicit opt-in; див. [DEPLOYMENT.md](DEPLOYMENT.md).
