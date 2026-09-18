from datetime import UTC, datetime, timedelta
import json
from itertools import count
from threading import Event, Lock, Thread
import time

import pytest

from rf_sentinel.acquisition import AsyncMeasurementSink, SpectrumSweep, SweepProfile
from rf_sentinel.errors import MeasurementPersistenceError
from rf_sentinel.health import AcquisitionHealth, HealthOwner
from rf_sentinel.observability import AcquisitionObserver
from rf_sentinel.storage import SQLiteMeasurementSink


NOW = datetime(2026, 9, 8, tzinfo=UTC)


def sweep(number=1):
    return SpectrumSweep(NOW, NOW + timedelta(seconds=1), 1, 24e6, 26e6,
                         (24.5e6, 25.5e6), 1e6, (-40.0, -50.0), "test",
                         sequence=number, sweep_id=f"telemetry-{number}")


class BackpressureClock:
    """Real monotonic clock with an event at the full-queue wait boundary."""

    def __init__(self):
        self.waiting_started = Event()
        self._lock = Lock()
        self._armed = False
        self._calls = 0

    def arm(self):
        with self._lock:
            self._armed = True
            self._calls = 0
            self.waiting_started.clear()

    def __call__(self):
        with self._lock:
            if self._armed:
                self._calls += 1
                if self._calls == 2:
                    self.waiting_started.set()
        return time.monotonic()


def test_cadence_jitter_and_missed_slots_are_budget_relative(tmp_path):
    observer = AcquisitionObserver(tmp_path / "health.json", SweepProfile(), 60, 60)
    observer.sweep_started(NOW, 121)
    metrics = observer.state.cadence_telemetry
    assert metrics["last_jitter_seconds"] == 61
    assert metrics["overrun_count"] == metrics["deferred_count"] == 1
    assert metrics["missed_slots"] == 1


def test_completed_sweep_records_source_duration_and_correlated_state(tmp_path):
    observer = AcquisitionObserver(tmp_path / "health.json", SweepProfile(), 60, 60)
    observer.completed(sweep(), timing={"source_duration_seconds": 2.5},
                       queue={"current_depth": 1, "high_water_mark": 2},
                       persistence={"last_write_duration_seconds": .25})
    assert observer.state.cadence_telemetry["last_source_duration_seconds"] == 2.5
    assert observer.state.queue_telemetry["current_depth"] == 1
    assert observer.state.persistence_telemetry["last_write_duration_seconds"] == .25


def test_sqlite_write_timing_counts_success_and_failure(tmp_path):
    ticks = iter([10.0, 10.25, 11.0, 11.75])
    store = SQLiteMeasurementSink(tmp_path / "sweeps.sqlite3", monotonic=lambda: next(ticks))
    store.store_sweep(sweep())
    assert store.persistence_telemetry["write_count"] == 1
    assert store.persistence_telemetry["last_write_duration_seconds"] == pytest.approx(.25)
    store._db.close()
    with pytest.raises(MeasurementPersistenceError):
        store.store_sweep(sweep(2))
    assert store.persistence_telemetry["write_count"] == 2
    assert store.persistence_telemetry["failed_persists"] == 1
    assert store.persistence_telemetry["total_write_duration_seconds"] == pytest.approx(1.0)


def test_queue_high_water_rejection_and_enqueue_metrics():
    started, release = Event(), Event()

    def blocked(_item):
        started.set()
        release.wait(1)

    ticks = count()
    sink = AsyncMeasurementSink(blocked, max_queue=1, enqueue_timeout=0,
                                monotonic=lambda: next(ticks) / 10)
    try:
        assert sink.store_sweep(sweep()).status == "accepted"
        assert started.wait(1)
        assert sink.store_sweep(sweep(2)).status == "accepted"
        assert sink.store_sweep(sweep(3)).status == "rejected"
        metrics = sink.queue_telemetry
        assert metrics["queued_depth"] == metrics["current_depth"] == 1
        assert metrics["in_flight"] == 1
        assert metrics["outstanding"] == metrics["pending_depth"] == 2
        assert metrics["high_water_mark"] == 1
        assert metrics["accepted_count"] == 2
        assert metrics["rejected_count"] == 1
        assert metrics["enqueue_count"] == 3
        assert metrics["backpressure_wait_count"] == 0
        assert metrics["last_backpressure_wait_seconds"] is None
    finally:
        release.set()
        sink.close(timeout=1)


def test_immediate_success_execution_time_is_not_backpressure():
    release = Event()
    ticks = iter([10.0, 10.25])
    sink = AsyncMeasurementSink(lambda _item: release.wait(1), enqueue_timeout=0,
                                monotonic=lambda: next(ticks))
    try:
        assert sink.store_sweep(sweep()).status == "accepted"
        metrics = sink.queue_telemetry
        assert metrics["last_enqueue_duration_seconds"] == pytest.approx(.25)
        assert metrics["backpressure_wait_count"] == 0
        assert metrics["last_backpressure_wait_seconds"] is None
    finally:
        release.set()
        sink.close(timeout=1)


def test_timed_enqueue_records_actual_wait_before_success():
    started, release = Event(), Event()
    clock = BackpressureClock()

    def blocked(_item):
        started.set()
        assert release.wait(1)

    sink = AsyncMeasurementSink(blocked, max_queue=1, enqueue_timeout=1,
                                monotonic=clock)
    result = []
    producer = None
    try:
        sink.store_sweep(sweep())
        assert started.wait(1)
        sink.store_sweep(sweep(2))
        clock.arm()
        producer = Thread(target=lambda: result.append(sink.store_sweep(sweep(3))))
        producer.start()
        assert clock.waiting_started.wait(1)
        release.set()
        producer.join(1)
        assert not producer.is_alive()
        sink.flush(timeout=1)
        assert result[0].status == "persisted"
        metrics = sink.queue_telemetry
        assert metrics["backpressure_wait_count"] == 1
        assert metrics["last_backpressure_wait_seconds"] > 0
        assert metrics["total_backpressure_wait_seconds"] > 0
    finally:
        release.set()
        if producer is not None:
            producer.join(1)
        sink.close(timeout=1)


def test_timed_enqueue_records_actual_wait_on_timeout_rejection():
    started, release = Event(), Event()
    clock = BackpressureClock()

    def blocked(_item):
        started.set()
        assert release.wait(1)

    sink = AsyncMeasurementSink(blocked, max_queue=1, enqueue_timeout=.05,
                                monotonic=clock)
    result = []
    producer = None
    try:
        sink.store_sweep(sweep())
        assert started.wait(1)
        sink.store_sweep(sweep(2))
        clock.arm()
        producer = Thread(target=lambda: result.append(sink.store_sweep(sweep(3))))
        producer.start()
        assert clock.waiting_started.wait(1)
        producer.join(1)
        assert not producer.is_alive()
        assert result[0].status == "rejected"
        metrics = sink.queue_telemetry
        assert metrics["accepted_count"] == 2
        assert metrics["backpressure_wait_count"] == 1
        assert metrics["last_backpressure_wait_seconds"] > 0
        assert metrics["rejected_count"] == 1
    finally:
        release.set()
        if producer is not None:
            producer.join(1)
        sink.close(timeout=1)


def test_queue_high_water_counts_only_queued_items_with_concurrent_consumer():
    started, release = Event(), Event()

    def blocked(_item):
        started.set()
        assert release.wait(1)

    sink = AsyncMeasurementSink(blocked, max_queue=3, enqueue_timeout=0)
    try:
        sink.store_sweep(sweep())
        assert started.wait(1)
        sink.store_sweep(sweep(2))
        sink.store_sweep(sweep(3))
        metrics = sink.queue_telemetry
        assert metrics["queued_depth"] == 2
        assert metrics["in_flight"] == 1
        assert metrics["outstanding"] == 3
        assert metrics["high_water_mark"] == 2
    finally:
        release.set()
        sink.close(timeout=1)


def test_final_snapshot_after_successful_drain_is_empty_and_includes_write_metrics(tmp_path):
    store = SQLiteMeasurementSink(tmp_path / "sweeps.sqlite3",
                                  monotonic=iter([10.0, 10.25]).__next__)
    sink = AsyncMeasurementSink(store)
    try:
        assert sink.store_sweep(sweep()).status == "accepted"
        sink.close(timeout=1)
        snapshot = sink.telemetry_snapshot()
        assert snapshot["shutdown_status"] == "complete"
        assert snapshot["queue"]["queued_depth"] == 0
        assert snapshot["queue"]["in_flight"] == 0
        assert snapshot["queue"]["outstanding"] == 0
        assert snapshot["persistence"]["write_count"] == 1
        assert snapshot["persistence"]["last_write_duration_seconds"] == pytest.approx(.25)
        assert snapshot["persistence"]["total_write_duration_seconds"] == pytest.approx(.25)
    finally:
        store.close()


def test_final_snapshot_reports_persistence_failure(tmp_path):
    store = SQLiteMeasurementSink(tmp_path / "sweeps.sqlite3")
    sink = AsyncMeasurementSink(store)
    store._db.close()
    try:
        assert sink.store_sweep(sweep()).status == "accepted"
        with pytest.raises(MeasurementPersistenceError):
            sink.close(timeout=1)
        snapshot = sink.telemetry_snapshot()
        assert snapshot["shutdown_status"] == "incomplete"
        assert snapshot["persistence"]["write_count"] == 1
        assert snapshot["persistence"]["failed_persists"] == 1
        assert snapshot["persistence"]["status"] == "failed"
        assert snapshot["persistence"]["last_write_duration_seconds"] is not None
        assert snapshot["queue"]["outstanding"] == 0
    finally:
        # The test intentionally closes the database before the sink.
        sink._writer.join(timeout=1)


def test_final_snapshot_is_coherent_for_multiple_pending_items():
    started, release = Event(), Event()

    def blocked(_item):
        started.set()
        assert release.wait(1)

    sink = AsyncMeasurementSink(blocked, max_queue=4, enqueue_timeout=0)
    try:
        assert sink.store_sweep(sweep()).status == "accepted"
        assert started.wait(1)
        assert sink.store_sweep(sweep(2)).status == "accepted"
        assert sink.store_sweep(sweep(3)).status == "accepted"
        before = sink.telemetry_snapshot()
        assert before["queue"]["outstanding"] == 3
        assert before["queue"]["outstanding"] == (
            before["queue"]["queued_depth"] + before["queue"]["in_flight"])
        release.set()
        sink.close(timeout=1)
        after = sink.telemetry_snapshot()
        assert after["shutdown_status"] == "complete"
        assert after["queue"]["outstanding"] == 0
        assert after["queue"]["queued_depth"] == after["queue"]["in_flight"] == 0
    finally:
        release.set()
        if not sink._closed:
            sink.close(timeout=1)


def test_timeout_snapshot_remains_incomplete_and_does_not_claim_empty_queue():
    started, release = Event(), Event()

    def blocked(_item):
        started.set()
        assert release.wait(1)

    sink = AsyncMeasurementSink(blocked, enqueue_timeout=0)
    try:
        sink.store_sweep(sweep())
        assert started.wait(1)
        with pytest.raises(TimeoutError):
            sink.close(timeout=0)
        snapshot = sink.telemetry_snapshot()
        assert snapshot["shutdown_status"] == "incomplete"
        assert snapshot["queue"]["in_flight"] == 1
        assert snapshot["queue"]["outstanding"] == 1
    finally:
        release.set()
        sink.close(timeout=1)


def test_sink_snapshot_boundary_is_thread_safe_and_coherent():
    started, release = Event(), Event()

    def blocked(_item):
        started.set()
        assert release.wait(1)

    sink = AsyncMeasurementSink(blocked, max_queue=8, enqueue_timeout=0)
    snapshots = []
    try:
        sink.store_sweep(sweep())
        assert started.wait(1)

        def read_snapshots():
            for _ in range(100):
                snapshots.append(sink.telemetry_snapshot())

        reader = Thread(target=read_snapshots)
        reader.start()
        sink.store_sweep(sweep(2))
        reader.join(1)
        assert not reader.is_alive()
        for snapshot in snapshots:
            queue = snapshot["queue"]
            assert queue["outstanding"] == queue["queued_depth"] + queue["in_flight"]
    finally:
        release.set()
        sink.close(timeout=1)


def test_health_is_pending_before_drain_and_zero_after_final_persist(tmp_path):
    health_path = tmp_path / "status" / "health.json"
    health = HealthOwner(health_path, AcquisitionHealth.started())
    store = SQLiteMeasurementSink(tmp_path / "sweeps.sqlite3",
                                  monotonic=iter([10.0, 10.25]).__next__)
    sink = AsyncMeasurementSink(store)
    observer = AcquisitionObserver(health_path, SweepProfile(), 60, 60,
                                   storage=store, health_owner=health)
    try:
        sink.store_sweep(sweep())
        observer.completed(sweep(2), queue=sink.queue_telemetry,
                           persistence=store.persistence_telemetry)
        before = json.loads(health_path.read_text())
        assert before["queue_telemetry"]["outstanding"] == 1

        sink.close(timeout=1)
        observer.persist_final_sink_snapshot(sink)
        after = json.loads(health_path.read_text())
        assert after["shutdown_status"] == "complete"
        assert after["queue_telemetry"]["queued_depth"] == 0
        assert after["queue_telemetry"]["in_flight"] == 0
        assert after["queue_telemetry"]["outstanding"] == 0
        assert after["persistence_telemetry"]["last_write_duration_seconds"] == pytest.approx(.25)
    finally:
        store.close()


def test_final_health_persist_keeps_pending_state_on_timeout(tmp_path):
    health_path = tmp_path / "health.json"
    health = HealthOwner(health_path, AcquisitionHealth.started())
    started, release = Event(), Event()

    def blocked(_item):
        started.set()
        release.wait(1)

    sink = AsyncMeasurementSink(blocked)
    observer = AcquisitionObserver(health_path, SweepProfile(), 60, 60,
                                   health_owner=health)
    try:
        sink.store_sweep(sweep())
        assert started.wait(1)
        with pytest.raises(TimeoutError):
            sink.close(timeout=0)
        observer.persist_final_sink_snapshot(sink)
        persisted = json.loads(health_path.read_text())
        assert persisted["shutdown_status"] == "incomplete"
        assert persisted["queue_telemetry"]["in_flight"] == 1
        assert persisted["queue_telemetry"]["outstanding"] == 1
    finally:
        release.set()
        sink.close(timeout=1)


def test_final_health_persist_contains_latest_persistence_failure(tmp_path):
    health_path = tmp_path / "health.json"
    health = HealthOwner(health_path, AcquisitionHealth.started())
    store = SQLiteMeasurementSink(tmp_path / "sweeps.sqlite3")
    sink = AsyncMeasurementSink(store)
    observer = AcquisitionObserver(health_path, SweepProfile(), 60, 60,
                                   storage=store, health_owner=health)
    store._db.close()
    try:
        sink.store_sweep(sweep())
        with pytest.raises(MeasurementPersistenceError):
            sink.close(timeout=1)
        observer.persist_final_sink_snapshot(sink)
        persisted = json.loads(health_path.read_text())
        assert persisted["shutdown_status"] == "incomplete"
        assert persisted["persistence_telemetry"]["status"] == "failed"
        assert persisted["persistence_telemetry"]["failed_persists"] == 1
        assert persisted["failed_persists"] == 1
    finally:
        sink._writer.join(timeout=1)


def test_final_health_write_follows_sink_snapshot(tmp_path):
    health_path = tmp_path / "health.json"
    events = []

    class OrderedSink:
        def telemetry_snapshot(self):
            events.append("snapshot")
            return {"queue": {"outstanding": 0}, "persistence": {"write_count": 1},
                    "shutdown_status": "complete"}

    health = HealthOwner(health_path, AcquisitionHealth.started())
    original_save = health.save
    health.save = lambda: (events.append("save"), original_save())[1]
    observer = AcquisitionObserver(health_path, SweepProfile(), 60, 60,
                                   health_owner=health)
    observer.persist_final_sink_snapshot(OrderedSink())
    assert events == ["snapshot", "save"]
