from dataclasses import replace
from datetime import UTC, datetime, timedelta

from PIL import Image, ImageFont
import pytest

from rf_sentinel.reporting import (SQLiteReportEngine, _closest_frequency_index,
                                   _keenerd_color_limits,
                                   _keenerd_frequency_tape,
                                   _canonical_time_grid, _row_quantum_seconds,
                                   _canonical_time_ticks,
                                   generate_report_package,
                                   render_report_heatmap,
                                   render_report_images, render_report_waterfall)
from tests.test_report_data import START, make_sweep, store


def report_with_powers(powers):
    from tests.test_report_package import report

    base = report()
    return replace(base, sweeps=(replace(base.sweeps[0], powers=tuple(powers)),))


def test_keenerd_color_limits_use_observed_negative_range_without_percentiles():
    assert _keenerd_color_limits(report_with_powers((-80.0, -35.0, -62.0))) == (
        -80.0, -35.0)


def test_keenerd_color_limits_give_constant_negative_values_a_minimum_span():
    assert _keenerd_color_limits(report_with_powers((-42.0,) * 3)) == (-42.0, -41.0)


def test_keenerd_color_limits_preserve_negative_values_below_default_floor():
    assert _keenerd_color_limits(report_with_powers((-130.0, -120.0))) == (
        -130.0, -100.0)


@pytest.mark.parametrize("powers, expected", [
    ((5.0, 10.0), (0.0, 10.0)),
    ((-20.0, 5.0), (-20.0, 5.0)),
])
def test_keenerd_color_limits_handle_positive_and_mixed_values(powers, expected):
    assert _keenerd_color_limits(report_with_powers(powers)) == expected


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


def test_native_waterfall_uses_one_row_per_canonical_time_slot(tmp_path):
    report = report_with_gaps(tmp_path)
    waterfall = tmp_path / "waterfall.png"
    render_report_waterfall(report, waterfall, "UTC")
    with Image.open(waterfall) as image:
        assert image.format == "PNG"
        assert image.size == (73, 29)


def report_with_gaps(tmp_path):
    path = tmp_path / "sweeps.sqlite3"
    store(path, make_sweep(1, 10), make_sweep(2, 70, status="partial"),
          make_sweep(3, 130, status="failed"))
    return SQLiteReportEngine(path).build(START, START + timedelta(seconds=180))


def test_both_renderers_create_pngs_from_same_report_data(tmp_path):
    report = report_with_gaps(tmp_path)
    waterfall = tmp_path / "waterfall.png"
    heatmap = tmp_path / "heatmap.png"
    assert render_report_images(report, waterfall, heatmap) == (waterfall, heatmap)
    with Image.open(waterfall) as waterfall_image, Image.open(heatmap) as heatmap_image:
        assert waterfall_image.format == "PNG"
        assert heatmap_image.format == "PNG"
        assert waterfall_image.size == (73, 29)
        assert waterfall_image.getbbox() is not None


def test_report_package_heatmap_uses_keenerd_path_not_matplotlib(monkeypatch, tmp_path):
    report = report_with_gaps(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy matplotlib heatmap path was used")

    monkeypatch.setattr("rf_sentinel.reporting.render_report_matplotlib_heatmap", forbidden)
    waterfall = tmp_path / "waterfall.png"
    heatmap = tmp_path / "keenerd.png"
    render_report_images(report, waterfall, heatmap, "UTC")

    with Image.open(heatmap) as image:
        assert image.mode == "RGB"
        assert image.size[0] == len(report.sweeps[0].powers)
        assert image.size[1] == 26 + len(_canonical_time_grid(
            report, tuple(sweep for sweep in report.sweeps if sweep.powers))[1])
        assert image.getpixel((0, 0)) == (255, 255, 0)


@pytest.mark.parametrize("report_data", [
    "empty",
    "failed",
])
def test_no_data_report_uses_matplotlib_fallback_without_keenerd(
        monkeypatch, tmp_path, report_data):
    from rf_sentinel import reporting
    from tests.test_report_package import report

    base = report()
    if report_data == "empty":
        data = replace(base, sweep_count=0, success_count=0, partial_count=0,
                       failed_count=0, frequency_range_hz=None,
                       time_ordering=(), peak_frequency_hz=None,
                       peak_power_db=None, sweeps=(), gaps=())
    else:
        data = report(outcome="failed", with_data=False)

    fallback_calls = []
    real_fallback = reporting.render_report_matplotlib_heatmap

    def fallback(report_value, destination, timezone):
        fallback_calls.append((report_value, timezone))
        return real_fallback(report_value, destination, timezone)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("keenerd renderer must not run for no-data reports")

    monkeypatch.setattr(reporting, "render_report_matplotlib_heatmap", fallback)
    monkeypatch.setattr(reporting, "render_report_keenerd_heatmap", forbidden)
    destination = tmp_path / f"{report_data}.png"

    reporting.render_report_heatmap(data, destination, "UTC")

    assert destination.exists()
    assert fallback_calls == [(data, "UTC")]


def test_frequency_tape_reuses_keenerd_adaptive_labels_without_duplicates(monkeypatch):
    from PIL import ImageDraw

    frequencies = tuple(24_000_000 + index * 174_759.18739967898
                        for index in range(9968))
    labels = []
    original = ImageDraw.ImageDraw.text

    def capture(draw, position, text, *args, **kwargs):
        labels.append(text)
        return original(draw, position, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    tape = _keenerd_frequency_tape(frequencies)
    assert tape.size == (9968, 26)
    assert tape.getpixel((0, 0)) == (255, 255, 0)
    assert "Частота" not in labels
    assert labels[:3] == ["100M", "200M", "300M"]
    assert len(labels) == len(set(labels))


def test_top_left_is_blank_yellow_and_frequency_tape_starts_at_raster_x(tmp_path):
    report = report_with_gaps(tmp_path)
    waterfall = tmp_path / "waterfall.png"
    render_report_waterfall(report, waterfall, "UTC")
    sweep = report.sweeps[0]
    tape_frequencies = tuple(sweep.frequencies_hz)
    expected_tape = _keenerd_frequency_tape(tape_frequencies)
    with Image.open(waterfall) as image:
        left = image.width - len(report.sweeps[0].powers)
        assert image.crop((0, 0, left, 26)).getextrema() == (
            (255, 255), (255, 255), (0, 0))
        assert image.crop((left, 0, image.width, 26)).tobytes() == expected_tape.tobytes()


def test_frequency_tape_uses_canonical_frequency_vector(tmp_path, monkeypatch):
    import rf_sentinel.reporting as reporting

    base = report_with_gaps(tmp_path)
    canonical = (100.5, 150.25)
    sweeps = tuple(replace(sweep, frequencies_hz=canonical,
                           frequency_start_hz=1.0, bin_width_hz=1.0)
                   if sweep.powers else sweep for sweep in base.sweeps)
    data = replace(base, sweeps=sweeps)
    captured = []

    def capture(frequencies):
        captured.append(tuple(frequencies))
        return Image.new("RGB", (len(frequencies), 26), (255, 255, 0))

    monkeypatch.setattr(reporting, "_keenerd_frequency_tape", capture)
    render_report_waterfall(data, tmp_path / "canonical.png", "UTC")
    assert captured == [canonical]


def test_canonical_frequency_tick_search_stays_within_half_local_bin():
    frequencies = (24_087_379.5625, 24_262_138.6875, 24_436_898.3125,
                   24_611_657.4375)
    spacing = [b - a for a, b in zip(frequencies, frequencies[1:])]
    for target in (24_000_000, 24_300_000, 24_500_000):
        index = _closest_frequency_index(target, frequencies)
        local = spacing[index - 1] if index else spacing[0]
        assert abs(target - frequencies[index]) <= local / 2


def test_canonical_grid_assigns_each_sweep_once_and_renders_black_missing_rows(tmp_path):
    report = report_with_gaps(tmp_path)
    rows = tuple(sweep for sweep in report.sweeps if sweep.powers)
    slots, assigned, cadence = _canonical_time_grid(report, rows)
    assert cadence == 60
    assert len(slots) == 3
    assert [sweep.sweep_id if sweep else None for sweep in assigned] == [
        "sweep-1", "sweep-2", None]
    assert [sweep.sweep_id for sweep in assigned if sweep] == ["sweep-1", "sweep-2"]

    waterfall = tmp_path / "missing-slots.png"
    render_report_waterfall(report, waterfall, "UTC")
    with Image.open(waterfall) as image:
        left, top = image.width - 2, 26
        assert image.getpixel((left, top)) != (0, 0, 0)
        assert image.getpixel((left, top + 1)) != (0, 0, 0)
        assert image.getpixel((left, top + 2)) == (0, 0, 0)


def test_canonical_grid_assigns_in_window_final_sweep_to_final_slot(tmp_path):
    from tests.test_report_package import report

    start = datetime(2026, 9, 17, 17, tzinfo=UTC)
    end = start + timedelta(hours=1)
    base = report()
    sweeps = tuple(
        replace(
            base.sweeps[0],
            sweep_id=f"hourly-{index}",
            started_at=start + timedelta(seconds=35.797321 + index * 60),
            finished_at=start + timedelta(seconds=76.172548 + index * 60),
        )
        for index in range(60)
    )
    data = replace(
        base,
        window_start=start,
        window_end=end,
        sweep_count=60,
        success_count=60,
        time_ordering=tuple(sweep.sweep_id for sweep in sweeps),
        sweeps=sweeps,
        gaps=(),
    )

    slots, assigned, quantum = _canonical_time_grid(data, sweeps)

    assert quantum == 60
    assert len(slots) == 60
    assert assigned[-1] is sweeps[-1]
    package = generate_report_package(data, tmp_path / "hourly", "UTC")
    assert package.waterfall.exists()
    assert package.heatmap.exists()


def test_row_quantum_rounding_ignores_boundary_jitter():
    from tests.test_report_package import report

    seed = report().sweeps[0]
    def quantum(duration):
        row = replace(seed, duration_seconds=duration)
        return _row_quantum_seconds((row,), 60)

    assert quantum(59.9) == 60
    assert quantum(60.003) == 60
    assert quantum(61) == 120
    assert quantum(121) == 180


def test_long_elapsed_gap_gets_proportional_missing_rows(tmp_path):
    from tests.test_report_package import report

    base = report()
    seed = base.sweeps[0]
    starts = (START, START + timedelta(seconds=60),
              START + timedelta(seconds=120), START + timedelta(seconds=600))
    sweeps = tuple(replace(seed, sweep_id=f"sweep-{index}", started_at=start,
                           finished_at=start + timedelta(seconds=1), duration_seconds=1)
                   for index, start in enumerate(starts))
    data = replace(base, window_start=START, window_end=START + timedelta(seconds=660),
                   sweep_count=4, success_count=4, partial_count=0, failed_count=0,
                   time_ordering=tuple(sweep.sweep_id for sweep in sweeps),
                   sweeps=sweeps, gaps=())
    slots, assigned, quantum = _canonical_time_grid(data, sweeps)
    assert quantum == 60
    assert len(slots) == 11
    assert [index for index, sweep in enumerate(assigned) if sweep is None] == list(range(3, 10))

    waterfall = tmp_path / "long-gap.png"
    render_report_waterfall(data, waterfall, "UTC")
    with Image.open(waterfall) as image:
        assert image.height == 37
        left, top = image.width - 2, 26
        assert image.getpixel((left, top + 3)) == (0, 0, 0)
        assert image.getpixel((left, top + 9)) == (0, 0, 0)


def test_time_ticks_are_inside_window_and_use_canonical_slots():
    start = datetime(2026, 9, 9, 22, 1, 47, tzinfo=UTC)
    end = start + timedelta(hours=24)
    from tests.test_report_package import report

    base = report()
    data = replace(
        base,
        window_start=start,
        window_end=end,
        time_ordering=("sweep-1",),
        sweeps=(replace(base.sweeps[0], started_at=start,
                        finished_at=start + timedelta(seconds=1)),),
    )
    slots, _rows, quantum = _canonical_time_grid(data, data.sweeps)
    ticks = _canonical_time_ticks(data, slots, quantum, "Europe/Kyiv")
    timestamps = tuple(tick.timestamp for tick in ticks)
    assert timestamps[0].isoformat() == "2026-09-10T01:10:00+03:00"
    assert timestamps[-1].isoformat() == "2026-09-11T01:00:00+03:00"
    assert all(start <= timestamp < end for timestamp in timestamps)


def test_daily_canonical_axis_is_half_open_and_replaces_midnight_label():
    from dataclasses import replace
    from zoneinfo import ZoneInfo
    from tests.test_report_package import report

    zone = ZoneInfo("Europe/Kyiv")
    start = datetime(2026, 9, 10, tzinfo=zone)
    end = datetime(2026, 9, 11, tzinfo=zone)
    base = report()
    data = replace(base, window_start=start, window_end=end,
                   sweeps=(replace(base.sweeps[0], started_at=start,
                                   finished_at=start + timedelta(seconds=1)),))
    slots, _rows, quantum = _canonical_time_grid(data, data.sweeps)
    ticks = _canonical_time_ticks(data, slots, quantum, "Europe/Kyiv")
    majors = [tick for tick in ticks if tick.kind == "major"]
    assert [tick.label for tick in majors] == ["10.09"] + [
        f"{hour:02d}:00" for hour in range(1, 24)]
    assert all(tick.label is None for tick in ticks if tick.kind == "minor")
    assert all(tick.timestamp < end for tick in ticks)
    assert "11.09" not in [tick.label for tick in majors]


def test_hourly_canonical_axis_has_no_terminal_tick_and_is_shared_by_renderers():
    from dataclasses import replace
    from zoneinfo import ZoneInfo
    from tests.test_report_package import report

    zone = ZoneInfo("Europe/Kyiv")
    start = datetime(2026, 9, 10, 12, tzinfo=zone)
    end = datetime(2026, 9, 10, 13, tzinfo=zone)
    base = report()
    data = replace(base, window_start=start, window_end=end,
                   sweeps=(replace(base.sweeps[0], started_at=start,
                                   finished_at=start + timedelta(seconds=1)),))
    slots, _rows, quantum = _canonical_time_grid(data, data.sweeps)
    waterfall_ticks = _canonical_time_ticks(data, slots, quantum, "Europe/Kyiv")
    heatmap_ticks = _canonical_time_ticks(data, slots, quantum, "Europe/Kyiv")
    waterfall_presentation_ticks = _canonical_time_ticks(
        data, slots, quantum, "Europe/Kyiv", label_minor_ticks=True)
    assert [(tick.timestamp, tick.row_index) for tick in waterfall_ticks] == [
        (tick.timestamp, tick.row_index) for tick in heatmap_ticks]
    assert [tick.timestamp for tick in waterfall_ticks] == [
        start + timedelta(minutes=minute) for minute in (0, 10, 20, 30, 40, 50)]
    assert [tick.kind for tick in waterfall_ticks] == ["major"] + ["minor"] * 5
    assert [tick.label for tick in waterfall_presentation_ticks] == [
        "12:00", "12:10", "12:20", "12:30", "12:40", "12:50"]
    assert waterfall_ticks[0].label == "12:00"
    assert waterfall_ticks[-1].label is None
    assert all(tick.timestamp != end for tick in waterfall_ticks)


def test_midnight_tick_uses_hourly_font_left_offset_and_longer_tick(
        tmp_path, monkeypatch):
    from PIL import ImageDraw
    from rf_sentinel.reporting import (_WATERFALL_TIME_DATE_TICK_LENGTH,
                                       _WATERFALL_TIME_DATE_X_OFFSET,
                                       _WATERFALL_TIME_MAJOR_TICK_LENGTH)
    from tests.test_report_package import report

    start = datetime(2026, 9, 10, 19, tzinfo=UTC)  # 22:00 Europe/Kyiv
    base = report()
    seed = base.sweeps[0]
    sweeps = tuple(
        replace(seed, sweep_id=f"sweep-{minute}",
                started_at=start + timedelta(minutes=minute),
                finished_at=start + timedelta(minutes=minute, seconds=1))
        for minute in range(181)
    )
    data = replace(
        base, window_start=start, window_end=start + timedelta(hours=3, seconds=1),
        sweep_count=len(sweeps), success_count=len(sweeps),
        time_ordering=tuple(sweep.sweep_id for sweep in sweeps), sweeps=sweeps, gaps=(),
    )
    labels, lines = [], []
    original_text = ImageDraw.ImageDraw.text
    original_line = ImageDraw.ImageDraw.line

    def capture(draw, position, text, *args, **kwargs):
        font = kwargs.get("font")
        labels.append((text, position, font.path, font.size, kwargs.get("stroke_width", 0)))
        return original_text(draw, position, text, *args, **kwargs)

    def capture_line(draw, coordinates, *args, **kwargs):
        lines.append(tuple(coordinates))
        return original_line(draw, coordinates, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    monkeypatch.setattr(ImageDraw.ImageDraw, "line", capture_line)
    waterfall = tmp_path / "waterfall.png"
    render_report_waterfall(data, waterfall, "Europe/Kyiv")
    label_text = [item[0] for item in labels]
    hourly_labels = [label for label in label_text
                     if ":" in label or label == "11.09"]
    assert hourly_labels == ["22:00", "23:00", "11.09"]
    assert "00:00" not in label_text
    assert "01:00" not in label_text
    date = next(item for item in labels if item[0] == "11.09")
    first = next(item for item in labels if item[0] == "22:00")
    before = next(item for item in labels if item[0] == "23:00")
    assert (first[2], first[3], first[4]) == (before[2], before[3], before[4])
    assert (date[2], date[3], date[4]) == (before[2], before[3], before[4])
    assert date[1][0] < before[1][0]
    assert _WATERFALL_TIME_DATE_X_OFFSET == -4
    time_ticks = [line for line in lines if line[1] == line[3] and line[1] >= 26]
    assert [line[1] for line in time_ticks] == list(range(26, 207, 10))
    normal_tick = next(line for line in lines if line[1] == line[3] == 86)
    date_tick = next(line for line in lines if line[1] == line[3] == 146)
    assert normal_tick[2] - normal_tick[0] == _WATERFALL_TIME_MAJOR_TICK_LENGTH == 6
    assert date_tick[2] - date_tick[0] == _WATERFALL_TIME_DATE_TICK_LENGTH == 10
    date_box = ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox(
        (0, 0), date[0], font=ImageFont.truetype(date[2], date[3]))
    assert abs(date[1][1] + (date_box[1] + date_box[3]) / 2 - date_tick[1]) <= 0.5
    for label, position, path, size, _ in labels:
        if ":" not in label and label != "11.09":
            continue
        box = ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox(
            (0, 0), label, font=ImageFont.truetype(path, size))
        assert position[1] + box[1] >= 0
        assert position[1] + box[3] <= 207


def test_renderers_keep_report_window_and_gaps_without_interpolation(tmp_path):
    report = report_with_gaps(tmp_path)
    waterfall = tmp_path / "waterfall.png"
    heatmap = tmp_path / "heatmap.png"
    render_report_waterfall(report, waterfall)
    render_report_heatmap(report, heatmap)
    # Both functions consume the same canonical window and do not alter its gap metadata.
    assert report.window_start == START
    assert report.window_end == START + timedelta(seconds=180)
    assert [gap.kind for gap in report.gaps] == ["leading", "between", "between", "trailing"]
    assert report.sweeps[-1].powers == ()


def test_renderers_are_deterministic_for_same_report(tmp_path):
    report = report_with_gaps(tmp_path)
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    render_report_waterfall(report, first)
    render_report_waterfall(report, second)
    assert first.read_bytes() == second.read_bytes()
