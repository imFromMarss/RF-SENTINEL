"""Application use case: acquisition -> local artifacts -> optional notification."""

import json
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from rf_sentinel.errors import NotificationError, ScanError, SentinelError
from rf_sentinel.reporting import failed_report, make_report, render_waterfall
from rf_sentinel.spectrum import ScanProfile, SpectrumScanner
from rf_sentinel.telegram import Notifier


@dataclass(frozen=True)
class SurveyOutcome:
    scan_status: str
    notification_status: str


class SurveyWorkflow:
    def __init__(self, scanner: SpectrumScanner, profile: ScanProfile, data_dir: Path,
                 identity: str, notifier: Notifier | None = None,
                 renderer=render_waterfall):
        self.scanner = scanner
        self.profile = profile
        self.data_dir = data_dir
        self.identity = identity
        self.notifier = notifier
        self.renderer = renderer

    def run(self) -> SurveyOutcome:
        started = datetime.now(UTC)
        clock = time.monotonic()
        result = None
        try:
            result = self.scanner.scan(self.profile)
            report = make_report(result, self.identity)
        except ScanError:
            report = failed_report(self.profile, self.identity, started, time.monotonic() - clock)
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            # Reserve these names for this application; retain only the latest survey.
            with tempfile.TemporaryDirectory(prefix=".survey-", dir=self.data_dir) as staging:
                stage = Path(staging)
                (stage / "report.json").write_text(
                    json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8"
                )
                (stage / "report.txt").write_text(report.to_text() + "\n", encoding="utf-8")
                if result is not None:
                    (stage / "spectrum.json").write_text(
                        json.dumps(asdict(result), default=lambda value: value.isoformat()) + "\n",
                        encoding="utf-8",
                    )
                    (stage / "spectrum.json").replace(self.data_dir / "spectrum.json")
                else:
                    # Never attach stale successful artifacts to a failed survey.
                    for name in ("spectrum.json", "waterfall.png"):
                        (self.data_dir / name).unlink(missing_ok=True)
                for name in ("report.txt", "report.json"):
                    (stage / name).replace(self.data_dir / name)
                if result is not None:
                    # Preserve source facts if derived rendering fails, and do not
                    # leave the previous survey's image beside the current report.
                    (self.data_dir / "waterfall.png").unlink(missing_ok=True)
                    self.renderer(result, stage / "waterfall.png")
                    (stage / "waterfall.png").replace(self.data_dir / "waterfall.png")
        except (OSError, ValueError, RuntimeError):
            raise SentinelError("Cannot persist or render survey artifacts") from None
        notification_status = "disabled"
        if self.notifier is not None:
            try:
                self.notifier.send_message(report.to_text())
                if result is not None:
                    self.notifier.send_photo(self.data_dir / "waterfall.png")
                notification_status = "sent"
            except NotificationError:
                notification_status = "failed"
        return SurveyOutcome(report.status, notification_status)
