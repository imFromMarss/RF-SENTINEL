import json
from datetime import UTC, datetime, timedelta

from rf_sentinel.reporting import (ReportData, ReportGap, ReportSweep,
                                   generate_report_package)


START = datetime(2026, 9, 8, 12, tzinfo=UTC)


def report(*, outcome="success", with_data=True):
    sweep = ReportSweep(
        "sweep-1", START + timedelta(seconds=10), START + timedelta(seconds=11),
        outcome, 0.5 if outcome == "partial" else 1.0,
        (100.0, 200.0) if with_data else (), (-50.0, -20.0) if with_data else (),
        50.0 if with_data else None, 250.0 if with_data else None,
        100.0 if with_data else None,
    )
    return ReportData(
        START, START + timedelta(minutes=1), 1,
        1 if outcome == "success" else 0,
        1 if outcome == "partial" else 0,
        1 if outcome == "failed" else 0,
        sweep.coverage, (50.0, 250.0) if with_data else None,
        ("sweep-1",), None if not with_data else 200.0,
        None if not with_data else -20.0, (sweep,),
        (ReportGap(START, sweep.started_at, "leading"),),
    )


def test_text_report_contains_window_quality_and_uncalibrated_note(tmp_path):
    text = report(outcome="partial").to_text("UTC")
    assert "Початок вікна:" in text
    assert "Завершення вікна:" in text
    assert "Тривалість:" in text
    assert "0 успішних, 1 неповних, 0 невдалих" in text
    assert "Покриття: 50.0%" in text
    assert "Діапазон частот: 0.000050–0.000250 МГц" in text
    assert "Пікова частота/потужність: 0.000200 МГц, -20.00 dB" in text
    assert "Попередження якості:" in text
    assert "рівні dB некалібровані" in text


def test_empty_and_failed_windows_still_get_all_png_artifacts(tmp_path):
    for name, data in (("empty", ReportData(
        START, START + timedelta(minutes=1), 0, 0, 0, 0, 0.0, None, (), None, None, (),
        (ReportGap(START, START + timedelta(minutes=1), "window"),),
    )), ("failed", report(outcome="failed", with_data=False))):
        package = generate_report_package(data, tmp_path / name, "UTC")
        assert all(path.exists() for path in package.paths)
        assert {path.parent for path in package.paths} == {package.artifact_dir}


def test_report_json_round_trip_and_package_metadata(tmp_path):
    data = report()
    package = generate_report_package(data, tmp_path / "window", "UTC")
    restored = ReportData.from_dict(json.loads(package.report_json.read_text(encoding="utf-8")))
    assert restored == data
    assert package.window_start == data.window_start
    assert package.window_end == data.window_end
    assert set(json.loads(package.report_json.read_text())) >= {
        "window_start", "window_end", "success_count", "gaps", "sweeps",
    }
