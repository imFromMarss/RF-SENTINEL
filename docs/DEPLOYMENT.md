# Deployment runbook — current state

Це practical runbook для target Raspberry Pi Compute Module 4 / Linux ARM64. Він описує current implementation і manual foreground operation. Repository **не містить повністю відтворюваного production `systemd` deployment/unit**; systemd unit у цьому runbook не додається.

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

## Start and stop

Canonical foreground start:

```sh
.venv/bin/python -m rf_sentinel station
```

Keep this process under an operator terminal during manual validation. Do not claim unattended boot/restart until an external supervisor is configured and tested. Stop with Ctrl+C or SIGTERM. The runtime sets a stop event, interrupts active `rtl_power`, drains bounded acquisition work, joins components within deadlines and closes SQLite connections. Logs report if a component exceeds its shutdown deadline.

## Logs, health and artifacts

Inspect:

```sh
find "${RF_SENTINEL_DATA_DIR:-runtime}" -maxdepth 3 -type f -print
tail -f "${RF_SENTINEL_DATA_DIR:-runtime}/logs/rf-sentinel.log"
```

Key artifacts are `sweeps.sqlite3`, `status/health.json`, JSONL logs, `reports/scheduled/` and `reports/last-hour/`. Failed sweeps and report failures remain observable in health/logs/incidents; do not delete them while diagnosing. Log rotation defaults to 5,000,000 bytes with 3 backups.

## Storage and operations

Reserve disk headroom for SQLite WAL/activity, report artifacts and temporary report payload/render work. Large reports can use temporary disk substantially; retention/cleanup policy is not a fully implemented production policy. Monitor free space and SQLite size, back up artifacts before maintenance, and avoid placing `DATA_DIR` on an unreliable or nearly full filesystem.

## Validation

Hardware-free suite:

```sh
.venv/bin/python -m pytest -q
```

Expected: `186 passed, 2 skipped`. Explicit hardware checks:

```sh
RF_SENTINEL_TEST_HARDWARE=1 .venv/bin/python -m pytest -q -m hardware
RF_SENTINEL_TEST_FULL_RANGE=1 .venv/bin/python -m pytest -q tests/test_hardware.py::test_real_full_range_survey
```

The first opt-in uses real RTL-SDR; the second is the 30-minute test. A passing test validates the selected host/device/profile only, not a universal calibrated measurement or production boot deployment.
