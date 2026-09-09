from datetime import timedelta

from PIL import Image

from rf_sentinel.reporting import (SQLiteReportEngine, render_report_heatmap,
                                   render_report_images, render_report_waterfall)
from tests.test_report_data import START, make_sweep, store


def report_with_gaps(tmp_path):
    path = tmp_path / "sweeps.sqlite3"
    store(path, make_sweep(1, 10), make_sweep(2, 20, status="partial"),
          make_sweep(3, 40, status="failed"))
    return SQLiteReportEngine(path).build(START, START + timedelta(seconds=60))


def test_both_renderers_create_pngs_from_same_report_data(tmp_path):
    report = report_with_gaps(tmp_path)
    waterfall = tmp_path / "waterfall.png"
    heatmap = tmp_path / "heatmap.png"
    assert render_report_images(report, waterfall, heatmap) == (waterfall, heatmap)
    with Image.open(waterfall) as waterfall_image, Image.open(heatmap) as heatmap_image:
        assert waterfall_image.format == "PNG"
        assert heatmap_image.format == "PNG"
        assert waterfall_image.size == heatmap_image.size
        assert waterfall_image.getbbox() == heatmap_image.getbbox()


def test_renderers_keep_report_window_and_gaps_without_interpolation(tmp_path):
    report = report_with_gaps(tmp_path)
    waterfall = tmp_path / "waterfall.png"
    heatmap = tmp_path / "heatmap.png"
    render_report_waterfall(report, waterfall)
    render_report_heatmap(report, heatmap)
    # Both functions consume the same canonical window and do not alter its gap metadata.
    assert report.window_start == START
    assert report.window_end == START + timedelta(seconds=60)
    assert [gap.kind for gap in report.gaps] == ["leading", "between", "between", "trailing"]
    assert report.sweeps[-1].powers == ()


def test_renderers_are_deterministic_for_same_report(tmp_path):
    report = report_with_gaps(tmp_path)
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    render_report_waterfall(report, first)
    render_report_waterfall(report, second)
    assert first.read_bytes() == second.read_bytes()
