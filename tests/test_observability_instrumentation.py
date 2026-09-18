import json
from itertools import permutations
from pathlib import Path
from threading import Barrier, Event, Thread
from types import SimpleNamespace

import pytest

from rf_sentinel.observability import ObservabilityCoordinator, RunSummaryWriter


class FakeMonotonicClock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value

    def set(self, value):
        self.value = value


def _record_intervals(component, sweep_interval, component_interval):
    clock = FakeMonotonicClock()
    coordinator = ObservabilityCoordinator(clock=clock)
    contexts = {}
    events = (
        (sweep_interval[0], "sweep", True),
        (sweep_interval[1], "sweep", False),
        (component_interval[0], "component", True),
        (component_interval[1], "component", False),
    )
    for timestamp, name, entering in sorted(events):
        clock.set(timestamp)
        if entering:
            context = coordinator.sweep() if name == "sweep" else coordinator.span(component)
            contexts[name] = context
            context.__enter__()
        else:
            contexts.pop(name).__exit__(None, None, None)
    return coordinator.snapshot()


def _play_intervals(intervals, *, boundary_orders=None):
    """Play timestamp-ordered public callbacks, optionally permuting one batch."""
    clock = FakeMonotonicClock()
    coordinator = ObservabilityCoordinator(clock=clock)
    contexts = {}
    events = {}
    for span_id, (kind, start_ts, end_ts) in intervals.items():
        events.setdefault(start_ts, []).append((span_id, "start"))
        events.setdefault(end_ts, []).append((span_id, "exit"))
    for timestamp in sorted(events):
        clock.set(timestamp)
        callbacks = (boundary_orders or {}).get(timestamp, events[timestamp])
        for span_id, callback in callbacks:
            kind = intervals[span_id][0]
            if callback == "start":
                context = (coordinator.sweep() if kind == "sweep"
                           else coordinator.span("report"))
                contexts[span_id] = context
                context.__enter__()
            else:
                contexts.pop(span_id).__exit__(None, None, None)
    return coordinator


@pytest.mark.parametrize(
    ("intervals", "join_timestamp", "expected_duration"),
    (
        (
            {
                "s0": ("sweep", 0.0, 1.0),
                "s1": ("sweep", 0.0, 8.0),
                "c0": ("component", 0.0, 2.0),
                "c1": ("component", 2.0, 8.0),
            },
            2.0,
            8.0,
        ),
        (
            {
                "s0": ("sweep", 0.0, 1.0),
                "s1": ("sweep", 0.0, 6.0),
                "c0": ("component", 0.0, 3.0),
                "c1": ("component", 3.0, 6.0),
            },
            3.0,
            6.0,
        ),
    ),
    ids=("known-7-to-8", "known-4-to-6"),
)
def test_acceptance_probes_use_exact_online_union(
        intervals, join_timestamp, expected_duration):
    callbacks = [("c0", "exit"), ("c1", "start")]
    results = set()
    for callback_order in permutations(callbacks):
        coordinator = _play_intervals(
            intervals, boundary_orders={join_timestamp: callback_order})
        metric = coordinator.snapshot()["overlap"]["sweep_report"]
        results.add((metric["count"], metric["duration_seconds"]))

    assert results == {(1, expected_duration)}


@pytest.mark.parametrize(
    ("intervals", "expected"),
    (
        (
            {"s0": ("sweep", 0.0, 6.0),
             "c0": ("component", 1.0, 4.0),
             "c1": ("component", 3.0, 7.0)},
            (1, 5.0),
        ),
        (
            {"s0": ("sweep", 0.0, 8.0),
             "s1": ("sweep", 2.0, 6.0),
             "c0": ("component", 1.0, 7.0),
             "c1": ("component", 3.0, 5.0)},
            (1, 6.0),
        ),
        (
            {"s0": ("sweep", 0.0, 3.0),
             "s1": ("sweep", 3.0, 6.0),
             "c0": ("component", 0.0, 6.0)},
            (1, 6.0),
        ),
        (
            {"s0": ("sweep", 0.0, 2.0),
             "s1": ("sweep", 4.0, 6.0),
             "c0": ("component", 0.0, 6.0)},
            (2, 4.0),
        ),
    ),
    ids=("partial", "nested", "adjacent", "disjoint"),
)
def test_canonical_overlap_covers_partial_nested_adjacent_and_disjoint(
        intervals, expected):
    metric = _play_intervals(intervals).snapshot()["overlap"]["sweep_report"]

    assert (metric["count"], metric["duration_seconds"]) == expected


def test_start_timestamp_capture_and_registration_are_one_linearized_operation():
    timestamp_captured = Event()
    allow_clock_return = Event()

    class PreemptingClock:
        def __init__(self):
            self.value = 0.0
            self.first_call = True

        def __call__(self):
            if self.first_call:
                self.first_call = False
                timestamp_captured.set()
                assert allow_clock_return.wait(2)
            return self.value

    clock = PreemptingClock()
    coordinator = ObservabilityCoordinator(clock=clock)
    report_entered = Event()
    finish_report = Event()
    sweep_attempted = Event()
    sweep_entered = Event()
    finish_sweep = Event()
    sweep_done = Event()

    def report_worker():
        with coordinator.span("report"):
            report_entered.set()
            assert finish_report.wait(2)

    def sweep_worker():
        sweep_attempted.set()
        with coordinator.sweep():
            sweep_entered.set()
            assert finish_sweep.wait(2)
        sweep_done.set()

    report_thread = Thread(target=report_worker)
    sweep_thread = Thread(target=sweep_worker)
    report_thread.start()
    assert timestamp_captured.wait(2)
    sweep_thread.start()
    assert sweep_attempted.wait(2)
    assert not sweep_entered.is_set()

    allow_clock_return.set()
    assert report_entered.wait(2)
    assert sweep_entered.wait(2)
    clock.value = 6.0
    finish_sweep.set()
    assert sweep_done.wait(2)
    finish_report.set()
    report_thread.join(2)
    sweep_thread.join(2)

    assert not report_thread.is_alive()
    assert not sweep_thread.is_alive()
    assert coordinator.snapshot()["overlap"]["sweep_report"] == {
        "count": 1,
        "duration_seconds": 6.0,
    }


def test_coordinator_records_timing_failure_retry_and_overlap_without_sleep():
    now = [0.0]
    coordinator = ObservabilityCoordinator(clock=lambda: now[0])
    with coordinator.sweep():
        with coordinator.span("report"):
            now[0] += 2.5
    coordinator.record_retry("telegram_polling")
    with pytest.raises(RuntimeError):
        with coordinator.span("telegram_handler"):
            now[0] += 1.0
            raise RuntimeError("synthetic")
    snapshot = coordinator.snapshot()
    assert snapshot["report"]["count"] == 1
    assert snapshot["report"]["success"] == 1
    assert snapshot["telegram"]["telegram_handler"]["failure"] == 1
    assert snapshot["telegram"]["telegram_polling"]["retries"] == 1
    assert snapshot["overlap"]["sweep_report"]["count"] == 1
    assert snapshot["overlap"]["sweep_report"]["duration_seconds"] == 2.5
    assert snapshot["active"] == {"sweeps": 0, "reports": 0,
                                   "telegram_handlers": 0, "telegram_deliveries": 0}


def test_span_reuses_start_and_end_timestamps_for_duration_and_overlap():
    class BoundaryClock:
        def __init__(self):
            self.value = 10.0
            self.calls = 0

        def __call__(self):
            self.calls += 1
            # An unexpected read after the span boundary models contention or
            # telemetry advancing the clock to 18.
            return 18.0 if self.calls >= 4 else self.value

        def set(self, value):
            self.value = value

    clock = BoundaryClock()
    coordinator = ObservabilityCoordinator(clock=clock)
    with coordinator.sweep():
        with coordinator.span("report"):
            clock.set(15.0)

    snapshot = coordinator.snapshot()
    assert snapshot["report"]["total_duration_seconds"] == 5.0
    assert snapshot["overlap"]["sweep_report"] == {
        "count": 1,
        "duration_seconds": 5.0,
    }


@pytest.mark.parametrize(
    ("component", "overlap_name"),
    (("report", "sweep_report"), ("telegram_handler", "sweep_telegram")),
)
@pytest.mark.parametrize(
    ("sweep_interval", "component_interval", "expected_count", "expected_duration"),
    (
        ((10.0, 15.0), (16.0, 20.0), 0, 0.0),
        ((10.0, 20.0), (12.0, 15.0), 1, 3.0),
        ((10.0, 15.0), (12.0, 20.0), 1, 3.0),
        ((12.0, 15.0), (10.0, 20.0), 1, 3.0),
        ((9.0, 15.0), (10.0, 14.0), 1, 4.0),
        ((12.0, 18.0), (10.0, 14.0), 1, 2.0),
    ),
    ids=("no-overlap", "full-containment", "partial-overlap", "reversed-start-order",
         "sweep-contains-report", "partial-report-tail"),
)
def test_overlap_is_interval_intersection_independent_of_start_order(
        component, overlap_name, sweep_interval, component_interval,
        expected_count, expected_duration):
    snapshot = _record_intervals(component, sweep_interval, component_interval)

    assert snapshot["overlap"][overlap_name] == {
        "count": expected_count,
        "duration_seconds": expected_duration,
    }
    if expected_count:
        assert snapshot["overlap"][overlap_name]["duration_seconds"] <= min(
            sweep_interval[1] - sweep_interval[0],
            component_interval[1] - component_interval[0],
        )


@pytest.mark.parametrize(
    ("reports", "expected_duration"),
    (
        (((1.0, 5.0), (3.0, 7.0)), 6.0),
        (((1.0, 7.0), (3.0, 5.0)), 6.0),
        (((1.0, 3.0), (3.0, 5.0)), 4.0),
        (((1.0, 3.0), (5.0, 7.0)), 4.0),
    ),
    ids=("partial-overlap-union", "nested-union", "adjacent-union", "disjoint-union"),
)
def test_concurrent_component_intervals_use_exact_sweep_union(reports, expected_duration):
    """Component exits contribute only uncovered portions of the union."""
    clock = FakeMonotonicClock()
    coordinator = ObservabilityCoordinator(clock=clock)
    sweep = coordinator.sweep()
    sweep.__enter__()
    contexts = []
    events = [(start, 1, index, True) for index, (start, _) in enumerate(reports)]
    events += [(end, 0, index, False) for index, (_, end) in enumerate(reports)]
    for timestamp, order, index, entering in sorted(events):
        clock.set(timestamp)
        if entering:
            context = coordinator.span("report")
            context.__enter__()
            contexts.append(context)
        else:
            contexts[index].__exit__(None, None, None)
    clock.set(10.0)
    sweep.__exit__(None, None, None)

    assert coordinator.snapshot()["overlap"]["sweep_report"] == {
        "count": 1 if reports[1][0] <= reports[0][1] else 2,
        "duration_seconds": expected_duration,
    }


@pytest.mark.parametrize(
    ("sweep_interval", "component_interval", "expected_duration"),
    (
        ((0.0, 4.0), (1.0, 6.0), 3.0),
        ((1.0, 6.0), (0.0, 4.0), 3.0),
    ),
    ids=("sweep-exits-first", "component-exits-first"),
)
def test_either_side_can_exit_first(sweep_interval, component_interval, expected_duration):
    snapshot = _record_intervals("report", sweep_interval, component_interval)

    assert snapshot["overlap"]["sweep_report"] == {
        "count": 1, "duration_seconds": expected_duration,
    }


def test_same_timestamp_callback_permutations_are_order_independent():
    intervals = {
        "s0": ("sweep", 0.0, 3.0),
        "s1": ("sweep", 3.0, 6.0),
        "c0": ("component", 0.0, 3.0),
        "c1": ("component", 3.0, 6.0),
    }
    callbacks = (("s0", "exit"), ("s1", "start"),
                 ("c0", "exit"), ("c1", "start"))
    results = set()
    for callback_order in permutations(callbacks):
        metric = _play_intervals(
            intervals, boundary_orders={3.0: callback_order}).snapshot()[
                "overlap"]["sweep_report"]
        results.add((metric["count"], metric["duration_seconds"]))

    assert results == {(1, 6.0)}


@pytest.mark.parametrize("first_callback", ("sweep_exit", "component_start"))
def test_point_only_intersection_is_one_zero_duration_episode(first_callback):
    intervals = {
        "s0": ("sweep", 0.0, 1.0),
        "c0": ("component", 1.0, 2.0),
    }
    callbacks = (("s0", "exit"), ("c0", "start"))
    if first_callback == "component_start":
        callbacks = tuple(reversed(callbacks))

    metric = _play_intervals(
        intervals, boundary_orders={1.0: callbacks}).snapshot()["overlap"]["sweep_report"]

    assert metric == {"count": 1, "duration_seconds": 0.0}


def test_same_timestamp_zero_duration_spans_form_one_episode():
    clock = FakeMonotonicClock(1.0)
    coordinator = ObservabilityCoordinator(clock=clock)

    with coordinator.sweep():
        with coordinator.span("report"):
            pass

    assert coordinator.snapshot()["overlap"]["sweep_report"] == {
        "count": 1, "duration_seconds": 0.0,
    }


def test_sequential_intersections_are_distinct_overlap_episodes():
    clock = FakeMonotonicClock()
    coordinator = ObservabilityCoordinator(clock=clock)

    with coordinator.sweep():
        clock.set(1.0)
        with coordinator.span("report"):
            clock.set(2.0)
        clock.set(3.0)
        with coordinator.span("report"):
            clock.set(4.0)
        clock.set(5.0)

    clock.set(10.0)
    with coordinator.span("telegram_handler"):
        clock.set(11.0)
        with coordinator.sweep():
            clock.set(12.0)
        clock.set(13.0)
        with coordinator.sweep():
            clock.set(14.0)
        clock.set(15.0)

    snapshot = coordinator.snapshot()
    assert snapshot["overlap"]["sweep_report"] == {
        "count": 2, "duration_seconds": 2.0,
    }
    assert snapshot["overlap"]["sweep_telegram"] == {
        "count": 2, "duration_seconds": 2.0,
    }


def test_overlap_false_to_true_adds_exactly_one_episode():
    clock = FakeMonotonicClock()
    coordinator = ObservabilityCoordinator(clock=clock)

    with coordinator.sweep():
        clock.set(1.0)
        with coordinator.span("report"):
            clock.set(2.0)
            with coordinator.span("report"):
                clock.set(3.0)
            clock.set(4.0)

    assert coordinator.snapshot()["overlap"]["sweep_report"] == {
        "count": 1, "duration_seconds": 3.0,
    }


@pytest.mark.parametrize(
    ("component", "nested_component", "overlap_name"),
    (
        ("report", "report", "sweep_report"),
        ("telegram_handler", "telegram_delivery", "sweep_telegram"),
    ),
)
def test_nested_spans_do_not_double_count_overlap(component, nested_component, overlap_name):
    clock = FakeMonotonicClock()
    coordinator = ObservabilityCoordinator(clock=clock)

    with coordinator.sweep():
        clock.set(1.0)
        with coordinator.sweep():
            clock.set(2.0)
            with coordinator.span(component):
                clock.set(3.0)
                with coordinator.span(nested_component):
                    clock.set(4.0)
                clock.set(5.0)
            clock.set(6.0)
        clock.set(7.0)

    snapshot = coordinator.snapshot()
    assert snapshot["overlap"][overlap_name] == {
        "count": 1, "duration_seconds": 3.0,
    }
    assert snapshot["active"] == {"sweeps": 0, "reports": 0,
                                   "telegram_handlers": 0, "telegram_deliveries": 0}


def test_1000_completed_sweeps_under_long_component_retain_no_history():
    clock = FakeMonotonicClock()
    coordinator = ObservabilityCoordinator(clock=clock)

    with coordinator.span("report"):
        retained_shapes = set()
        for index in range(1000):
            clock.set(index * 2.0 + 1.0)
            with coordinator.sweep():
                clock.set(index * 2.0 + 2.0)
            retained = coordinator._overlap_domains["sweep_report"]
            retained_shapes.add((
                len(retained),
                len(retained["boundary"]),
                len(retained["sweep_tokens"]),
                len(retained["component_tokens"]),
            ))
        clock.set(2001.0)

    snapshot = coordinator.snapshot()
    assert snapshot["overlap"]["sweep_report"] == {
        "count": 1000, "duration_seconds": 1000.0,
    }
    assert retained_shapes == {(7, 4, 0, 1)}
    assert snapshot["active"]["reports"] == 0
    retained = coordinator._overlap_domains["sweep_report"]
    assert retained["sweep_tokens"] == set()
    assert retained["component_tokens"] == set()


@pytest.mark.parametrize(
    ("component", "overlap_name"),
    (("report", "sweep_report"), ("telegram_handler", "sweep_telegram")),
)
@pytest.mark.parametrize("exception_side", ("component", "sweep"))
def test_overlap_cleanup_on_exception(component, overlap_name, exception_side):
    clock = FakeMonotonicClock()
    coordinator = ObservabilityCoordinator(clock=clock)

    outer = coordinator.sweep() if exception_side == "component" else coordinator.span(component)
    inner = coordinator.span(component) if exception_side == "component" else coordinator.sweep()
    with outer:
        clock.set(1.0)
        with pytest.raises(RuntimeError):
            with inner:
                clock.set(3.0)
                raise RuntimeError("synthetic")
        clock.set(4.0)

    snapshot = coordinator.snapshot()
    assert snapshot["overlap"][overlap_name] == {
        "count": 1, "duration_seconds": 2.0,
    }
    assert snapshot["active"] == {"sweeps": 0, "reports": 0,
                                   "telegram_handlers": 0, "telegram_deliveries": 0}


def test_snapshot_includes_open_overlap_without_double_counting_it():
    clock = FakeMonotonicClock()
    coordinator = ObservabilityCoordinator(clock=clock)

    with coordinator.sweep():
        clock.set(2.0)
        with coordinator.span("report"):
            clock.set(5.0)
            assert coordinator.snapshot()["overlap"]["sweep_report"] == {
                "count": 1, "duration_seconds": 3.0,
            }
            assert coordinator.snapshot()["overlap"]["sweep_report"] == {
                "count": 1, "duration_seconds": 3.0,
            }
            clock.set(7.0)
            assert coordinator.snapshot()["overlap"]["sweep_report"] == {
                "count": 1, "duration_seconds": 5.0,
            }
            clock.set(8.0)
        clock.set(10.0)

    assert coordinator.snapshot()["overlap"]["sweep_report"] == {
        "count": 1, "duration_seconds": 6.0,
    }


def test_coordinator_state_is_thread_safe_at_minimum_boundary():
    coordinator = ObservabilityCoordinator(clock=lambda: 0.0)
    barrier = Barrier(4)

    def worker():
        barrier.wait()
        for _ in range(100):
            with coordinator.span("telegram_delivery"):
                pass

    threads = [Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    snapshot = coordinator.snapshot()
    assert snapshot["telegram"]["telegram_delivery"]["count"] == 300
    assert snapshot["active"]["telegram_deliveries"] == 0


@pytest.mark.parametrize(
    ("component", "overlap_name"),
    (("report", "sweep_report"), ("telegram_delivery", "sweep_telegram")),
)
def test_concurrent_nested_spans_form_one_thread_safe_overlap_episode(
        component, overlap_name):
    clock = FakeMonotonicClock()
    coordinator = ObservabilityCoordinator(clock=clock)
    entered = Barrier(4)
    release = Barrier(4)

    def worker():
        with coordinator.span(component):
            entered.wait()
            release.wait()

    with coordinator.sweep():
        threads = [Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        entered.wait()
        assert coordinator.snapshot()["overlap"][overlap_name] == {
            "count": 1, "duration_seconds": 0.0,
        }
        clock.set(5.0)
        release.wait()
        for thread in threads:
            thread.join()
        clock.set(6.0)

    assert coordinator.snapshot()["overlap"][overlap_name] == {
        "count": 1, "duration_seconds": 5.0,
    }


def test_run_summary_is_bounded_redacted_and_atomic(tmp_path):
    path = tmp_path / "status" / "run-summary.json"
    writer = RunSummaryWriter(path, run_id="run-1", clock=lambda: __import__("datetime").datetime(
        2026, 1, 1, tzinfo=__import__("datetime").timezone.utc))
    payload = writer.write(settings=SimpleNamespace(
        telegram_bot_token="123:secret", telegram_chat_id="999",
        data_dir=Path("runtime"), safe=7), coordinator=ObservabilityCoordinator(),
                           incidents=[{"secret": "nope"}] * 100,
                           status="completed")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded == payload
    assert loaded["schema"] == "rf-sentinel.capacity-run-summary.v1"
    assert loaded["run_id"] == "run-1"
    assert loaded["configuration"]["safe"] == 7
    assert "telegram_bot_token" not in loaded["configuration"]
    assert "telegram_chat_id" not in loaded["configuration"]
    assert loaded["failures"]["status"] == "clean"
    assert len(loaded["failures"]["incidents"]) == 32
    assert loaded["failures"]["incidents"][0]["secret"] == "[REDACTED]"
    assert not list(path.parent.glob(".run-summary-*"))


def test_run_summary_failure_accounting_is_bounded_and_keeps_recent_incidents(tmp_path):
    writer = RunSummaryWriter(tmp_path / "summary.json", max_incidents=2)
    payload = writer.build(
        incidents=[{"id": "old"}, {"id": "newer"}, {"id": "newest"}],
        component_failures=[{"component": "acquisition", "message": "boom"}],
    )

    assert payload["application"]["status"] == "failed"
    assert payload["failures"]["status"] == "failed"
    assert [item["id"] for item in payload["failures"]["incidents"]] == ["newer", "newest"]
    assert payload["failures"]["components"][0]["component"] == "acquisition"


def test_run_summary_coordinator_failure_is_non_clean_and_redacted(tmp_path):
    coordinator = ObservabilityCoordinator()
    with pytest.raises(RuntimeError):
        with coordinator.span("report"):
            raise RuntimeError("token=super-secret")
    payload = RunSummaryWriter(tmp_path / "summary.json").build(
        settings=SimpleNamespace(api_token="super-secret"), coordinator=coordinator,
    )

    assert payload["application"]["status"] == "failed"
    assert payload["failures"]["status"] == "failed"
    assert payload["failures"]["coordinator"][0]["message"] == "token=[REDACTED]"


def test_run_summary_no_incidents_and_source_failure_are_valid(tmp_path):
    writer = RunSummaryWriter(tmp_path / "summary.json")
    empty = writer.build()
    unavailable = writer.build(incident_source="unavailable")

    assert empty["failures"] == {
        "status": "clean", "components": [], "coordinator": [], "incidents": [],
        "incident_source": "available",
    }
    assert unavailable["failures"]["incidents"] == []
    assert unavailable["failures"]["incident_source"] == "unavailable"


def test_run_summary_start_marker_is_running_and_json_serializable(tmp_path):
    path = tmp_path / "status" / "run-summary.json"
    writer = RunSummaryWriter(path, run_id="run-current", clock=lambda: __import__("datetime").datetime(
        2026, 1, 1, tzinfo=__import__("datetime").timezone.utc))

    marker = writer.write_start_marker()

    assert json.loads(path.read_text(encoding="utf-8")) == marker
    assert marker["run_id"] == "run-current"
    assert marker["application"] == {
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": None,
        "status": "running",
    }
    assert json.dumps(marker, ensure_ascii=False)


def test_run_summary_graceful_finalization_keeps_start_run_id(tmp_path):
    path = tmp_path / "status" / "run-summary.json"
    ticks = iter(__import__("datetime").datetime(2026, 1, day, tzinfo=__import__("datetime").timezone.utc)
                 for day in (1, 2))
    writer = RunSummaryWriter(path, run_id="run-same", clock=lambda: next(ticks))

    writer.write_start_marker()
    final = writer.write(status="stopped")

    assert final["run_id"] == "run-same"
    assert final["application"]["status"] == "stopped"
    assert final["application"]["started_at"] == "2026-01-01T00:00:00+00:00"
    assert final["application"]["ended_at"] == "2026-01-02T00:00:00+00:00"


def _final_sink_snapshot(**overrides):
    snapshot = {
        "queue": {"queued_depth": 0, "in_flight": 0, "outstanding": 0},
        "persistence": {"status": "ok", "failed_persists": 0, "storage_error": None},
        "shutdown_status": "complete",
        "writer_alive": False,
        "drain_complete": True,
    }
    snapshot.update(overrides)
    return snapshot


def test_run_summary_complete_sink_is_clean_and_stopped(tmp_path):
    payload = RunSummaryWriter(tmp_path / "summary.json").build(
        status="stopped", sink_snapshot=_final_sink_snapshot())

    assert payload["application"]["status"] == "stopped"
    assert payload["failures"]["status"] == "clean"
    assert payload["failures"]["components"] == []


@pytest.mark.parametrize("component", ("acquisition", "report-scheduler", "telegram-daemon",
                                        "resource-sampler"))
def test_run_summary_incomplete_managed_component_overrides_clean(tmp_path, component):
    payload = RunSummaryWriter(tmp_path / "summary.json").build(
        status="stopped", sink_snapshot=_final_sink_snapshot(),
        component_failures=[{
            "component": component, "category": "shutdown", "code": "incomplete",
            "message": f"{component} remained alive",
        }],
    )

    assert payload["application"]["status"] == "failed"
    assert payload["failures"]["status"] == "failed"
    assert payload["failures"]["components"][0]["component"] == component


def test_run_summary_retains_multiple_bounded_component_shutdown_failures(tmp_path):
    failures = [
        {"component": "telegram-daemon", "category": "shutdown", "code": "incomplete",
         "message": "Telegram remained alive"},
        {"component": "resource-sampler", "category": "shutdown", "code": "incomplete",
         "message": "Sampler timed out"},
    ]
    payload = RunSummaryWriter(tmp_path / "summary.json", max_incidents=2).build(
        status="stopped", sink_snapshot=_final_sink_snapshot(), component_failures=failures,
    )

    assert payload["application"]["status"] == "failed"
    assert payload["failures"]["status"] == "failed"
    assert [item["component"] for item in payload["failures"]["components"]] == [
        "telegram-daemon", "resource-sampler"
    ]


@pytest.mark.parametrize(
    "snapshot",
    [
        _final_sink_snapshot(shutdown_status="incomplete"),
        _final_sink_snapshot(writer_alive=True),
        _final_sink_snapshot(drain_complete=False),
        _final_sink_snapshot(queue={"queued_depth": 1, "in_flight": 0, "outstanding": 1}),
    ],
)
def test_run_summary_non_clean_sink_shutdown_is_not_clean(tmp_path, snapshot):
    payload = RunSummaryWriter(tmp_path / "summary.json").build(
        status="stopped", sink_snapshot=snapshot)

    assert payload["application"]["status"] in {"partial", "failed"}
    assert payload["failures"]["status"] != "clean"
    assert payload["failures"]["components"][-1]["component"] == "storage"
    assert payload["failures"]["components"][-1]["category"] == "sink_shutdown"


def test_run_summary_persistence_failure_is_failed(tmp_path):
    payload = RunSummaryWriter(tmp_path / "summary.json").build(
        status="stopped",
        sink_snapshot=_final_sink_snapshot(
            persistence={"status": "failed", "failed_persists": 1,
                         "storage_error": "SQLite persistence failed"}),
    )

    assert payload["application"]["status"] == "failed"
    assert payload["failures"]["status"] == "failed"
    assert "persistence_failed" in payload["failures"]["components"][-1]["message"]


def test_run_summary_reuses_coherent_final_sink_storage_values(tmp_path):
    snapshot = _final_sink_snapshot(
        queue={"queued_depth": 2, "in_flight": 1, "outstanding": 3},
        persistence={"status": "failed", "failed_persists": 2,
                     "storage_error": "SQLite persistence failed"},
        shutdown_status="incomplete", writer_alive=True, drain_complete=False,
    )
    payload = RunSummaryWriter(tmp_path / "summary.json").build(
        state=__import__("rf_sentinel.health", fromlist=["AcquisitionHealth"]).AcquisitionHealth.started(),
        status="stopped", sink_snapshot=snapshot)

    assert payload["acquisition"]["queue_telemetry"] == snapshot["queue"]
    assert payload["acquisition"]["persistence_telemetry"] == snapshot["persistence"]
    assert payload["acquisition"]["shutdown_status"] == snapshot["shutdown_status"]


def test_run_summary_new_start_replaces_previous_completed_or_unfinalized_run(tmp_path):
    path = tmp_path / "status" / "run-summary.json"
    completed = RunSummaryWriter(path, run_id="run-old").write(status="completed")
    current = RunSummaryWriter(path, run_id="run-new").write_start_marker()

    assert completed["run_id"] != current["run_id"]
    assert current["application"]["status"] == "running"
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == "run-new"

    stale = RunSummaryWriter(path, run_id="run-crashed").write_start_marker()
    next_current = RunSummaryWriter(path, run_id="run-next").write_start_marker()
    assert stale["application"]["status"] == "running"
    assert stale["run_id"] != next_current["run_id"]
    assert next_current["application"]["status"] == "running"


def test_run_summary_start_marker_atomic_failure_is_cleaned(tmp_path, monkeypatch):
    path = tmp_path / "summary.json"
    writer = RunSummaryWriter(path, run_id="run-1")
    monkeypatch.setattr(Path, "replace", lambda *_args: (_ for _ in ()).throw(OSError("disk")))

    with pytest.raises(OSError):
        writer.write_start_marker()
    assert not list(tmp_path.glob(".run-summary-*"))


def test_run_summary_configuration_redacts_identifiers_nested_values_and_preserves_capacity(tmp_path):
    settings = SimpleNamespace(
        telegram_bot_token="123:super-secret-token",
        telegram_chat_id="-100123456",
        telegram_allowed_chat_ids=("-100123456", "-100999999"),
        telegram_allowed_user_ids={"42", "84"},
        acquisition_low_hz=24_000_000,
        acquisition_high_hz=1_766_000_000,
        acquisition_cadence_budget_seconds=60,
        resource_sampling_interval_seconds=15,
        timezone="Europe/Kyiv",
        nested={"password": "nested-secret", "safe": "capacity"},
        leaked_copy="prefix 123:super-secret-token suffix",
        data_dir=Path("runtime"),
    )

    payload = RunSummaryWriter(tmp_path / "summary.json").build(settings=settings)
    serialized = json.dumps(payload, ensure_ascii=False)
    configuration = payload["configuration"]

    assert "123:super-secret-token" not in serialized
    assert "-100123456" not in serialized
    assert "-100999999" not in serialized
    assert '"42"' not in serialized and '"84"' not in serialized
    assert "telegram_bot_token" not in configuration
    assert "telegram_chat_id" not in configuration
    assert "telegram_allowed_chat_ids" not in configuration
    assert "telegram_allowed_user_ids" not in configuration
    assert configuration["nested"] == {"safe": "capacity"}
    assert configuration["acquisition_low_hz"] == 24_000_000
    assert configuration["acquisition_cadence_budget_seconds"] == 60
    assert configuration["resource_sampling_interval_seconds"] == 15
    assert configuration["timezone"] == "Europe/Kyiv"
    assert json.loads(serialized) == payload


def test_run_summary_allows_unavailable_metrics_and_cleans_failed_atomic_write(tmp_path, monkeypatch):
    path = tmp_path / "summary.json"
    writer = RunSummaryWriter(path)
    payload = writer.write(state=None, coordinator=None)
    assert payload["resources"]["latest"] is None
    monkeypatch.setattr(Path, "replace", lambda *_args: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        writer.write()
    assert not list(tmp_path.glob(".run-summary-*"))
