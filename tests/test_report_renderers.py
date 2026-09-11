from datetime import timedelta

from PIL import Image

from rf_sentinel.reporting import (SQLiteReportEngine, render_report_heatmap,
                                   render_report_images, render_report_waterfall)
from tests.test_report_data import START, make_sweep, store


def test_external_percentiles_match_numpy_across_merge_passes(monkeypatch):
    from dataclasses import replace
    import numpy as np
    import pytest
    from rf_sentinel import reporting
    from tests.test_report_package import report

    monkeypatch.setattr(reporting, "_SORT_RUN_VALUES", 7)
    monkeypatch.setattr(reporting, "_MERGE_FAN_IN", 3)
    base = report()
    for powers in (tuple(np.random.default_rng(42).normal(-40, 12, 401)),
                   (-20.0,) * 100, (-30.0,), ()):
        data = replace(base, sweeps=(replace(base.sweeps[0], powers=powers),))
        expected = reporting.color_limits(powers) if powers else (-1.0, 1.0)
        assert reporting._report_color_limits(data) == pytest.approx(expected)
    data = replace(base, sweeps=(replace(base.sweeps[0], powers=(float("nan"),)),))
    with pytest.raises(ValueError):
        reporting._report_color_limits(data)


def test_streamed_drawing_matches_original_pixels_and_retains_no_meshes(tmp_path, monkeypatch):
    from dataclasses import replace
    import numpy as np
    from matplotlib.axes import Axes
    from matplotlib.patches import Rectangle
    from rf_sentinel import reporting
    from tests.test_report_package import report

    base = report()
    first = base.sweeps[0]
    data = replace(base, sweeps=(
        first,
        replace(first, started_at=START + timedelta(seconds=10.5),
                finished_at=START + timedelta(seconds=15), powers=(-10.0, -60.0)),
        replace(first, started_at=START + timedelta(seconds=20),
                finished_at=START + timedelta(seconds=30), frequencies_hz=(120.0,),
                powers=(-35.0,), bin_width_hz=40),
        replace(first, outcome="failed", powers=(), frequencies_hz=(),
                started_at=START + timedelta(seconds=25),
                finished_at=START + timedelta(seconds=35)),
    ))
    original_mesh = Axes.pcolormesh
    seen = []

    def checked_mesh(axes, *args, **kwargs):
        # Colorbar has its own axes; report axes must never retain prior meshes.
        if axes.get_ylabel().startswith("Час"):
            assert len(axes.collections) == 0
            seen.append(axes)
        return original_mesh(axes, *args, **kwargs)

    monkeypatch.setattr(Axes, "pcolormesh", checked_mesh)
    streamed = tmp_path / "streamed.png"
    reporting.render_report_waterfall(data, streamed, "UTC")
    assert len(seen) == 3
    assert all(len(axes.collections) == 0 for axes in seen)
    monkeypatch.setattr(Axes, "pcolormesh", original_mesh)

    def reference(axes, data, *, cmap, norm, failed_color):
        low, high = reporting._report_limits(data)
        for sweep in data.sweeps:
            y0, y1 = (sweep.started_at.timestamp() / 86400,
                      sweep.finished_at.timestamp() / 86400)
            if sweep.powers:
                edges = reporting._frequency_edges(sweep.frequencies_hz, low, high,
                                                   sweep.bin_width_hz) / 1e6
                axes.pcolormesh(edges, [y0, y1], np.asarray([sweep.powers]),
                                shading="flat", antialiased=False, rasterized=True,
                                cmap=cmap, norm=norm)
            elif sweep.outcome == "failed":
                axes.add_patch(Rectangle((low / 1e6, y0), (high - low) / 1e6, y1 - y0,
                                         facecolor=failed_color, edgecolor=failed_color,
                                         hatch="///", linewidth=0, alpha=0.38, zorder=3))

    monkeypatch.setattr(reporting, "_report_meshes", reference)
    expected = tmp_path / "expected.png"
    reporting.render_report_waterfall(data, expected, "UTC")
    with Image.open(streamed) as actual, Image.open(expected) as baseline:
        np.testing.assert_array_equal(np.asarray(actual), np.asarray(baseline))


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
