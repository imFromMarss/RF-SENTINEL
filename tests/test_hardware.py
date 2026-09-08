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
