# Deployment runbook — current state

Це practical runbook для deployed Raspberry Pi Compute Module 4 / Ubuntu Server. Поточний target працює з `rf-sentinel.service`, автоматично стартує при boot і розгорнутий із `main` на baseline HEAD `6db0a6637688bbaf030214412e972d9e3fb524d3`. Repository не містить unit як повністю відтворюваний deployment artifact; наведені нижче команди також придатні для foreground validation.

## Prerequisites

- CM4 із Linux ARM64, достатнім RAM і writable storage;
- Python 3.12 (або версія, сумісна з `pyproject.toml`), `venv` і build/runtime dependencies;
- RTL-SDR hardware, USB cable/power та installed `rtl_power`/`librtlsdr`;
- доступ до Telegram Bot API лише якщо потрібні scheduled або on-demand reports.

Після checkout:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Перевірте `rtl_power --version` або фактичний executable у PATH. Non-root user має бачити USB device; налаштуйте distribution udev rule/group permissions і перелогіньтеся. Не запускайте station як workaround із ширшими permissions, якщо це не потрібно.

## Configuration

`.env` не завантажується автоматично:

```sh
set -a
source .env
set +a
```

Основні settings:

- `RF_SENTINEL_DATA_DIR` — writable root for `sweeps.sqlite3`, `status/`, `logs/` і `reports/`; default `runtime`;
- `RF_SENTINEL_ACQUISITION_START_HZ=24000000`;
- `RF_SENTINEL_ACQUISITION_STOP_HZ=1766000000`;
- `RF_SENTINEL_ACQUISITION_BIN_HZ=500000`;
- `RF_SENTINEL_ACQUISITION_CADENCE_BUDGET_SECONDS=60`;
- `RF_SENTINEL_ACQUISITION_RECOVERY_SECONDS=60`;
- `RF_SENTINEL_RTL_DEVICE_INDEX=0`, optional `RF_SENTINEL_RTL_GAIN`;
- `RF_SENTINEL_TIMEZONE=Europe/Kyiv`.

Telegram requires both `RF_SENTINEL_TELEGRAM_BOT_TOKEN` and `RF_SENTINEL_TELEGRAM_CHAT_ID`. Restrict inbound report requests with numeric `RF_SENTINEL_TELEGRAM_ALLOWED_CHAT_IDS`; optionally set numeric `RF_SENTINEL_TELEGRAM_ALLOWED_USER_IDS`. `TELEGRAM_ALLOWED_CHAT_IDS` defaults to configured `TELEGRAM_CHAT_ID` when omitted. Telegram is read-only: it can request the last-hour report, not start/stop/configure SDR or execute shell commands. Unauthorized updates are rejected before report generation; monitoring remains independent of Telegram polling.

## Service operation

На deployed CM4 primary lifecycle належить systemd:

```sh
sudo systemctl status rf-sentinel.service
sudo systemctl restart rf-sentinel.service
sudo journalctl -u rf-sentinel.service -f
```

Service enabled для автоматичного запуску при boot. Не запускайте паралельний foreground `station` на тому самому `DATA_DIR`: station lock захищає від другого instance.

Для локальної validation без systemd:

Canonical foreground start:

```sh
.venv/bin/python -m rf_sentinel station
```

Keep this process under an operator terminal during manual validation. Stop with Ctrl+C or SIGTERM. The runtime sets a stop event, interrupts active `rtl_power`, drains bounded acquisition work, joins components within deadlines and closes SQLite connections. Logs report if a component exceeds its shutdown deadline.

## Logs, health and artifacts

Inspect:

```sh
find "${RF_SENTINEL_DATA_DIR:-runtime}" -maxdepth 3 -type f -print
tail -f "${RF_SENTINEL_DATA_DIR:-runtime}/logs/rf-sentinel.log"
```

On the deployed target the primary artifacts are `runtime/sweeps.sqlite3`, `runtime/status/health.json` and `runtime/logs/rf-sentinel.log`; reports are under `runtime/reports/`. Failed sweeps and report failures remain observable in health/logs/incidents; do not delete them while diagnosing. Log rotation defaults to 5,000,000 bytes with 3 backups.

## Storage and operations

Reserve disk headroom for SQLite WAL/activity, report artifacts and temporary report payload/render work. Large reports can use temporary disk substantially; retention/cleanup policy remains production-hardening work. `.local*` / `.local-validation/` are disposable local validation only; runtime data is operational; canonical characterization/calibration datasets are long-lived and must not be treated as disposable. Monitor free space and SQLite size, back up artifacts before maintenance, and avoid placing `DATA_DIR` on an unreliable or nearly full filesystem.

## Validation

Hardware-free suite:

```sh
.venv/bin/python -m pytest -q
```

Expected: `387 passed, 2 skipped`. Explicit hardware checks:

```sh
RF_SENTINEL_TEST_HARDWARE=1 .venv/bin/python -m pytest -q -m hardware
RF_SENTINEL_TEST_FULL_RANGE=1 .venv/bin/python -m pytest -q tests/test_hardware.py::test_real_full_range_survey
```

The first opt-in uses real RTL-SDR; the second is the 30-minute test. A passing test validates the selected host/device/profile only, not a universal calibrated measurement. Do not run hardware tests as part of the canonical hardware-free validation.
