from PIL import Image

from rf_sentinel.reporting import make_report, render_waterfall


def test_report(scan_result):
    report = make_report(scan_result, "RF Sentinel / test-host")
    assert report.status == "success"
    assert report.peak_frequency_hz == 93_000_000
    assert report.peak_power_db == -10
    assert report.power_values == 4
    assert report.accumulated_samples == 60
    assert report.bins_per_sweep == 2
    assert "test-host" in report.to_text()
    assert "uncalibrated" in report.to_text()
    assert report.to_dict()["backend"] == "fake"


def test_real_waterfall_render(scan_result, tmp_path):
    path = tmp_path / "waterfall.png"
    render_waterfall(scan_result, path)
    with Image.open(path) as image:
        assert image.format == "PNG"
        assert image.width > 800 and image.height > 300
        assert image.convert("L").getextrema()[0] < image.convert("L").getextrema()[1]
