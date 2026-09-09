from datetime import UTC, datetime

import pytest

from rf_sentinel.spectrum import ScanProfile, ScanResult, SpectrumData, SpectrumFrame


@pytest.fixture
def scan_result():
    return ScanResult(
        "fake", ScanProfile(), datetime(2026, 9, 8, tzinfo=UTC), 30.0,
        SpectrumData(
            (88_000_000.0, 98_000_000.0, 108_000_000.0),
            (
                SpectrumFrame(datetime(2026, 9, 8, 0, 0, 10, tzinfo=UTC), (-60.0, -20.0), 20),
                SpectrumFrame(datetime(2026, 9, 8, 0, 0, 30, tzinfo=UTC), (-10.0, -50.0), 40),
            ),
        ),
    )
