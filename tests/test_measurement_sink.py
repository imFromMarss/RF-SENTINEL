from datetime import UTC, datetime, timedelta
from threading import Event, Thread
import time

import pytest

from rf_sentinel.acquisition import AsyncMeasurementSink, MeasurementReceipt, SpectrumSweep
from rf_sentinel.errors import MeasurementPersistenceError


NOW = datetime(2026, 9, 8, tzinfo=UTC)


def sweep(sequence):
    return SpectrumSweep(
        NOW, NOW + timedelta(seconds=1), 1, 24e6, 26e6,
        (24.5e6, 25.5e6), 1e6, (-40.0, -50.0), "test", sequence=sequence,
    )


def wait_for_completion(sink):
    """Wait on the writer's completion notification, without driving publication."""
    with sink._condition:
        assert sink._condition.wait_for(lambda: sink._pending == 0, timeout=1)


def test_normal_enqueue_write_and_ordered_flush():
    written = []
    release = Event()

    def write(item):
        assert release.wait(1)
        written.append(item)

    sink = AsyncMeasurementSink(write, max_queue=4, enqueue_timeout=0)
    try:
        receipts = [sink.store_sweep(sweep(number)) for number in range(1, 4)]
        assert [receipt.status for receipt in receipts] == ["accepted"] * 3
        release.set()
        sink.flush()
        assert [item.sequence for item in written] == [1, 2, 3]
        assert [receipt.status for receipt in receipts] == ["persisted"] * 3
        assert sink.accepted_count == 3
        assert sink.rejected_count == 0
    finally:
        release.set()
        sink.close(timeout=1)
    assert not sink._writer.is_alive()
    snapshot = sink.telemetry_snapshot()
    assert snapshot["shutdown_status"] == "complete"
    assert snapshot["writer_alive"] is False
    assert snapshot["drain_complete"] is True


def test_writer_success_publishes_terminal_receipt_before_flush_or_close():
    sink = AsyncMeasurementSink(lambda _item: None, enqueue_timeout=0)
    try:
        receipt = sink.store_sweep(sweep(1))
        wait_for_completion(sink)

        assert receipt.status == "persisted"
        assert sink.pending_count == 0
        assert sink.persisted_count == 1
        assert sink.failed_count == 0
    finally:
        sink.close(timeout=1)


def test_writer_exception_publishes_failed_receipt_and_counter_at_completion():
    def fail(_item):
        raise OSError("backend unavailable")

    sink = AsyncMeasurementSink(fail, enqueue_timeout=0)
    receipt = sink.store_sweep(sweep(1))
    wait_for_completion(sink)
    try:
        assert receipt.status == "failed"
        assert sink.pending_count == 0
        assert sink.persisted_count == 0
        assert sink.failed_count == 1
    finally:
        with pytest.raises(MeasurementPersistenceError):
            sink.close(timeout=1)


def test_downstream_rejected_receipt_is_not_promoted_to_persisted():
    rejection = MeasurementPersistenceError("downstream rejected sweep")

    def reject(item):
        return MeasurementReceipt(item.sweep_id, "rejected", rejection)

    sink = AsyncMeasurementSink(reject, enqueue_timeout=0)
    try:
        receipt = sink.store_sweep(sweep(1))
        wait_for_completion(sink)

        assert receipt.status == "rejected"
        assert receipt.error is rejection
        assert sink.persisted_count == 0
        assert sink.rejected_count == 0
        assert sink.failed_count == 1
    finally:
        with pytest.raises(MeasurementPersistenceError):
            sink.close(timeout=1)


def test_downstream_accepted_receipt_is_terminal_failure_not_persistence():
    def incomplete(item):
        return MeasurementReceipt(item.sweep_id, "accepted")

    sink = AsyncMeasurementSink(incomplete, enqueue_timeout=0)
    receipt = sink.store_sweep(sweep(1))
    wait_for_completion(sink)
    try:
        assert receipt.status == "failed"
        assert sink.persisted_count == 0
        assert sink.failed_count == 1
    finally:
        with pytest.raises(MeasurementPersistenceError):
            sink.close(timeout=1)


def test_slow_sink_does_not_block_acquisition_forever():
    started = Event()
    release = Event()
    written = []

    def slow_write(item):
        started.set()
        assert release.wait(1)
        written.append(item)

    sink = AsyncMeasurementSink(slow_write, max_queue=1, enqueue_timeout=0)
    try:
        first = sink.store_sweep(sweep(1))
        assert started.wait(1)
        second = sink.store_sweep(sweep(2))
        assert first.status == "accepted"
        assert second.status == "accepted"
        release.set()
        sink.flush()
    finally:
        release.set()
        sink.close(timeout=1)
    assert [item.sequence for item in written] == [1, 2]


def test_queue_full_is_explicit_rejection_and_counter():
    started = Event()
    release = Event()

    def blocked_write(_item):
        started.set()
        assert release.wait(1)

    sink = AsyncMeasurementSink(blocked_write, max_queue=1, enqueue_timeout=0)
    try:
        sink.store_sweep(sweep(1))
        assert started.wait(1)
        sink.store_sweep(sweep(2))
        rejected = sink.store_sweep(sweep(3))

        assert rejected.status == "rejected"
        assert sink.accepted_count == 2
        assert sink.rejected_count == 1
    finally:
        release.set()
        sink.close(timeout=1)


def test_persistence_failure_is_failed_not_persisted():
    def fail(_item):
        raise OSError("backend unavailable")

    sink = AsyncMeasurementSink(fail, enqueue_timeout=0)
    try:
        receipt = sink.store_sweep(sweep(1))
        assert receipt.status == "accepted"
        assert sink.accepted_count == 1
        assert sink.rejected_count == 0
        with pytest.raises(MeasurementPersistenceError):
            sink.flush()
        assert receipt.status == "failed"
        assert sink.accepted_count == 1
        assert sink.failed_count == 1
        assert not sink._writer.is_alive()
    finally:
        with pytest.raises(MeasurementPersistenceError):
            sink.close(timeout=1)
    assert not sink._writer.is_alive()


def test_close_drains_and_flush_is_idempotent():
    written = []
    sink = AsyncMeasurementSink(written.append, enqueue_timeout=0)
    try:
        sink.store_sweep(sweep(1))
        sink.close(timeout=1)
        sink.flush()
        sink.close(timeout=1)
    finally:
        if sink._writer.is_alive():
            sink.close(timeout=1)
    assert [item.sequence for item in written] == [1]


def test_close_deadline_returns_incomplete_while_writer_is_blocked():
    started, release = Event(), Event()

    def blocked(_item):
        started.set()
        release.wait(1)

    sink = AsyncMeasurementSink(blocked, enqueue_timeout=0)
    receipt = sink.store_sweep(sweep(1))
    assert started.wait(1)
    try:
        with pytest.raises(TimeoutError):
            sink.close(deadline=time.monotonic())
        assert receipt.status == "accepted"
        assert sink.persisted_count == 0
        assert sink.failed_count == 0
        snapshot = sink.telemetry_snapshot()
        assert snapshot["shutdown_status"] == "incomplete"
        assert snapshot["writer_alive"] is True
        assert snapshot["drain_complete"] is False
        assert sink._closed is False
    finally:
        release.set()
        sink.close(timeout=1)


def test_close_reconciles_persistence_completed_after_flush_timeout():
    started, release, persisted = Event(), Event(), Event()

    def delayed_write(_item):
        started.set()
        assert release.wait(1)
        persisted.set()

    sink = AsyncMeasurementSink(delayed_write, enqueue_timeout=0)
    receipt = sink.store_sweep(sweep(1))
    assert started.wait(1)
    try:
        with pytest.raises(TimeoutError):
            sink.flush(timeout=0)
        release.set()
        assert persisted.wait(1)
        sink.close(timeout=1)
        assert receipt.status == "persisted"
        assert sink.persisted_count == 1
        assert sink.failed_count == 0
    finally:
        release.set()
        if sink._writer.is_alive():
            sink.close(timeout=1)


def test_flush_timeout_then_later_completion_publishes_without_close():
    started, release = Event(), Event()

    def delayed_write(_item):
        started.set()
        assert release.wait(1)

    sink = AsyncMeasurementSink(delayed_write, enqueue_timeout=0)
    receipt = sink.store_sweep(sweep(1))
    assert started.wait(1)
    try:
        with pytest.raises(TimeoutError):
            sink.flush(timeout=0)
        release.set()
        wait_for_completion(sink)

        assert receipt.status == "persisted"
        assert sink.pending_count == 0
        assert sink.persisted_count == 1
        assert sink.failed_count == 0
    finally:
        release.set()
        sink.close(timeout=1)


def test_mixed_downstream_outcomes_keep_fifo_receipt_correspondence_and_counters():
    calls = []
    rejection = MeasurementPersistenceError("rejected by downstream")
    failure = MeasurementPersistenceError("downstream reported failure")

    def persist(item):
        calls.append(item.sequence)
        outcomes = {
            1: MeasurementReceipt(item.sweep_id, "persisted"),
            2: MeasurementReceipt(item.sweep_id, "rejected", rejection),
            3: MeasurementReceipt(item.sweep_id, "persisted"),
            4: MeasurementReceipt(item.sweep_id, "failed", failure),
        }
        return outcomes[item.sequence]

    sink = AsyncMeasurementSink(persist, max_queue=4, enqueue_timeout=0)
    try:
        receipts = [sink.store_sweep(sweep(number)) for number in range(1, 5)]
        wait_for_completion(sink)

        assert calls == [1, 2, 3, 4]
        assert [receipt.status for receipt in receipts] == [
            "persisted", "rejected", "persisted", "failed"
        ]
        assert [receipt.error for receipt in receipts] == [None, rejection, None, failure]
        assert sink.accepted_count == 4
        assert sink.persisted_count == 2
        assert sink.rejected_count == 0
        assert sink.failed_count == 2
        assert sink.pending_count == 0
        assert sink.queue_telemetry["outstanding"] == (
            sink.queue_telemetry["queued_depth"] + sink.queue_telemetry["in_flight"]
        )
        assert sink.accepted_count == (
            sink.persisted_count + sink.failed_count
        )
    finally:
        with pytest.raises(MeasurementPersistenceError):
            sink.close(timeout=1)


def test_close_reconciles_persistence_failure_after_flush_timeout():
    started, release, failed = Event(), Event(), Event()

    def delayed_failure(_item):
        started.set()
        assert release.wait(1)
        failed.set()
        raise OSError("backend unavailable")

    sink = AsyncMeasurementSink(delayed_failure, enqueue_timeout=0)
    receipt = sink.store_sweep(sweep(1))
    assert started.wait(1)
    try:
        with pytest.raises(TimeoutError):
            sink.flush(timeout=0)
        release.set()
        assert failed.wait(1)
        with pytest.raises(MeasurementPersistenceError):
            sink.close(timeout=1)
        assert receipt.status == "failed"
        assert sink.persisted_count == 0
        assert sink.failed_count == 1
    finally:
        release.set()
        if sink._writer.is_alive():
            sink._writer.join(timeout=1)


def test_downstream_close_finishes_before_deadline_and_shutdown_is_complete():
    received = []

    class Downstream:
        def store_sweep(self, _item):
            return None

        def close(self, *, deadline):
            received.append(deadline)

    deadline = time.monotonic() + 1
    sink = AsyncMeasurementSink(Downstream(), enqueue_timeout=0)
    sink.close(deadline=deadline)

    snapshot = sink.telemetry_snapshot()
    assert received == [deadline]
    assert snapshot["shutdown_status"] == "complete"
    assert snapshot["downstream_close_status"] == "complete"
    assert snapshot["downstream_close_active"] is False


def test_blocked_downstream_close_does_not_hold_close_caller_past_deadline():
    close_started, release = Event(), Event()
    close_errors = []

    class Downstream:
        def store_sweep(self, _item):
            return None

        def close(self):
            close_started.set()
            release.wait()

    sink = AsyncMeasurementSink(Downstream(), enqueue_timeout=0)
    with sink._condition:
        sink._closing = True
        sink._condition.notify_all()
    sink._writer.join(1)
    assert not sink._writer.is_alive()
    with sink._condition:
        sink._closing = False

    def close_at_expired_deadline():
        try:
            sink.close(deadline=time.monotonic())
        except BaseException as error:
            close_errors.append(error)

    caller = Thread(target=close_at_expired_deadline, name="acceptance-close-caller")
    caller.start()
    caller.join(1)
    try:
        assert not caller.is_alive()
        assert len(close_errors) == 1
        assert isinstance(close_errors[0], TimeoutError)
        assert close_started.wait(1)
        snapshot = sink.telemetry_snapshot()
        assert snapshot["shutdown_status"] == "incomplete"
        assert snapshot["drain_complete"] is True
        assert snapshot["writer_alive"] is False
        assert snapshot["downstream_close_status"] == "incomplete"
        assert snapshot["downstream_close_active"] is True
        assert sink._downstream_close_thread.daemon is True
        assert sink._downstream_close_thread.name.endswith("-downstream-close")
    finally:
        release.set()
        sink.close(timeout=1)


def test_downstream_close_exception_is_non_clean_and_visible():
    failure = RuntimeError("close exploded")

    class Downstream:
        def store_sweep(self, _item):
            return None

        def close(self):
            raise failure

    sink = AsyncMeasurementSink(Downstream(), enqueue_timeout=0)
    with pytest.raises(MeasurementPersistenceError) as raised:
        sink.close(timeout=1)

    snapshot = sink.telemetry_snapshot()
    assert raised.value.__cause__ is failure
    assert snapshot["shutdown_status"] == "incomplete"
    assert snapshot["drain_complete"] is True
    assert snapshot["writer_alive"] is False
    assert snapshot["downstream_close_status"] == "failed"
    assert snapshot["downstream_close_active"] is False
    assert snapshot["downstream_close_error"] == "MeasurementPersistenceError"


def test_downstream_timeout_receives_only_budget_remaining_after_writer_join():
    class Clock:
        now = 100.0

        def __call__(self):
            return self.now

    class JoinedWriter:
        name = "budgeted-writer"

        def __init__(self, clock):
            self.clock = clock
            self.join_timeouts = []

        def join(self, timeout):
            self.join_timeouts.append(timeout)
            self.clock.now += 0.9

        def is_alive(self):
            return False

    received = []

    class Downstream:
        def store_sweep(self, _item):
            return None

        def close(self, *, timeout):
            received.append(timeout)

    clock = Clock()
    sink = AsyncMeasurementSink(
        Downstream(), enqueue_timeout=0, deadline_monotonic=clock)
    with sink._condition:
        sink._closing = True
        sink._condition.notify_all()
    sink._writer.join(1)
    assert not sink._writer.is_alive()
    with sink._condition:
        sink._closing = False
    writer = JoinedWriter(clock)
    sink._writer = writer

    sink.close(deadline=101.0)

    assert writer.join_timeouts == [pytest.approx(1.0)]
    assert received == [pytest.approx(0.1)]
    assert clock.now == pytest.approx(100.9)
    assert sink.telemetry_snapshot()["shutdown_status"] == "complete"


def test_downstream_close_timeout_preserves_receipt_and_counter_invariants():
    close_started, release = Event(), Event()

    class Downstream:
        def store_sweep(self, _item):
            return None

        def close(self):
            close_started.set()
            release.wait()

    sink = AsyncMeasurementSink(Downstream(), enqueue_timeout=0)
    receipt = sink.store_sweep(sweep(1))
    wait_for_completion(sink)
    with sink._condition:
        sink._closing = True
        sink._condition.notify_all()
    sink._writer.join(1)
    assert not sink._writer.is_alive()
    with sink._condition:
        sink._closing = False
    before = sink.queue_telemetry
    try:
        with pytest.raises(TimeoutError):
            sink.close(deadline=time.monotonic())
        assert close_started.wait(1)
        after = sink.queue_telemetry
        assert receipt.status == "persisted"
        assert before == after
        assert sink.accepted_count == sink.persisted_count == 1
        assert sink.failed_count == sink.rejected_count == 0
    finally:
        release.set()
        sink.close(timeout=1)
