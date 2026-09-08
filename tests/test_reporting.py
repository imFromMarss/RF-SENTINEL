from datetime import timedelta

import pytest
from PIL import Image

from rf_sentinel.reporting import color_limits, heatmap_grid, make_report, render_heatmap
from rf_sentinel.spectrum import ScanResult, SpectrumData, SpectrumFrame


def test_report_is_compact_and_ukrainian(scan_result):
    report = make_report(scan_result, "RF Sentinel / test-host")
    text = report.to_text()
    assert report.peak_frequency_hz == 93_000_000
    assert report.peak_power_db == -10
    assert report.power_values == 4
    assert report.accumulated_samples == 60
    assert report.bins_per_sweep == 2
    assert "RF Sentinel — звіт моніторингу спектра" in text
    assert "Початок сканування:" in text
    assert "Завершення:" in text
    assert "Підсилення: автоматичне" in text
    assert "Кількість проходів: 2" in text
    assert "Частота: 93.000000 МГц" in text
    assert "Рівень: -10.00 dB" in text
    assert "Рівні потужності некалібровані" in text
    assert "power_values" not in text
    assert report.to_dict()["backend"] == "fake"


def test_robust_color_limits_ignore_single_outlier():
    ordinary = list(range(100))
    baseline = color_limits(ordinary)
    outlier = color_limits(ordinary + [1_000_000])
    assert baseline[0] == pytest.approx(1.98)
    assert outlier[1] < 1_000_000
    assert outlier[1] < 200


def test_constant_color_limits_have_visible_span():
    assert color_limits([[-42, -42], [-42, -42]]) == (-42.5, -41.5)


def test_heatmap_geometry_requires_ordered_frequency_and_time(scan_result):
    frequencies, times, powers = heatmap_grid(scan_result)
    assert list(frequencies) == sorted(frequencies)
    assert list(times) == sorted(times)
    assert powers.shape == (2, 2)
    bad_frequency = ScanResult(
        scan_result.backend, scan_result.profile, scan_result.started_at,
        scan_result.duration_seconds,
        SpectrumData(tuple(reversed(scan_result.spectrum.edges_hz)), scan_result.spectrum.frames),
    )
    with pytest.raises(ValueError):
        heatmap_grid(bad_frequency)
    bad_time = ScanResult(
        scan_result.backend, scan_result.profile, scan_result.started_at,
        scan_result.duration_seconds,
        SpectrumData(scan_result.spectrum.edges_hz, tuple(reversed(scan_result.spectrum.frames))),
    )
    with pytest.raises(ValueError):
        heatmap_grid(bad_time)


def test_multi_sweep_heatmap_dimensions(scan_result, tmp_path):
    start = scan_result.started_at
    frames = tuple(
        SpectrumFrame(start + timedelta(seconds=10 * (index + 1)),
                      (-60.0 + index, -20.0 - index), 20)
        for index in range(8)
    )
    result = ScanResult(
        "fake", scan_result.profile, start, 80,
        SpectrumData(scan_result.spectrum.edges_hz, frames),
    )
    path = tmp_path / "heatmap.png"
    render_heatmap(result, path)
    with Image.open(path) as image:
        assert image.format == "PNG"
        assert image.width >= 1800
        assert image.height >= 1000
        assert image.convert("L").getextrema()[0] < image.convert("L").getextrema()[1]
