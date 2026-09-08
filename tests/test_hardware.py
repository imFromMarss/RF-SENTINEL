"""Explicit opt-in only; never called by the default hardware-free suite."""

import os

import pytest

from rf_sentinel.rtl_power import RTLPowerScanner
from rf_sentinel.spectrum import ScanProfile


@pytest.mark.hardware
@pytest.mark.skipif(os.environ.get("RF_SENTINEL_TEST_HARDWARE") != "1",
                    reason="Real RTL-SDR requires explicit opt-in")
def test_real_fm_survey():
    result = RTLPowerScanner().scan(ScanProfile())
    assert result.spectrum.frames
    assert result.spectrum.edges_hz[0] <= 88_000_000
    assert result.spectrum.edges_hz[-1] >= 108_000_000


@pytest.mark.hardware
@pytest.mark.skipif(os.environ.get("RF_SENTINEL_TEST_FULL_RANGE") != "1",
                    reason="30-хвилинний hardware test потребує окремого opt-in")
def test_real_full_range_survey(tmp_path):
    result = RTLPowerScanner().scan(
        ScanProfile.full_range(24_000_000, 1_766_000_000),
        tmp_path / "spectrum.csv",
    )
    assert len(result.spectrum.frames) >= 20
    assert result.spectrum.edges_hz[0] <= 24_500_000
    assert result.spectrum.edges_hz[-1] >= 1_765_500_000
