"""Composition root; the no-argument identity entry point remains unchanged."""

import argparse
import logging
import socket
import sys
from threading import Event

from rf_sentinel.config import Settings
from rf_sentinel.errors import SentinelError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rf_sentinel")
    parser.add_argument("mode", nargs="?", choices=("survey", "schedule"))
    # main() remains callable without consuming pytest/host-process arguments.
    args = parser.parse_args([] if argv is None else argv)
    if args.mode is None:
        print("RF Sentinel")
        return 0
    try:
        settings = Settings.from_env()
        from rf_sentinel.rtl_power import RTLPowerScanner
        from rf_sentinel.scheduler import run_schedule
        from rf_sentinel.spectrum import ScanProfile
        from rf_sentinel.telegram import TelegramNotifier
        from rf_sentinel.workflow import SurveyWorkflow

        notifier = None
        if settings.telegram_enabled:
            notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        workflow = SurveyWorkflow(
            RTLPowerScanner(settings.rtl_device_index, settings.rtl_gain),
            ScanProfile(), settings.data_dir, f"RF Sentinel / {socket.gethostname()}", notifier,
        )
        if args.mode == "survey":
            outcome = workflow.run()
            print(f"Survey: {outcome.scan_status}; Telegram: {outcome.notification_status}")
            return 0 if outcome.scan_status == "success" else 1
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
        run_schedule(workflow.run, settings.report_interval_minutes * 60, Event())
        return 0
    except KeyboardInterrupt:
        return 130
    except SentinelError:
        print("RF Sentinel: configuration or survey failed; check local setup", file=sys.stderr)
        return 1
