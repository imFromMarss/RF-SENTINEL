from datetime import UTC, datetime, timedelta
from threading import Event

import pytest

from rf_sentinel.acquisition import AsyncMeasurementSink, SpectrumSweep
from rf_sentinel.errors import MeasurementPersistenceError


NOW = datetime(2026, 9, 8, tzinfo=UTC)


def sweep(sequence):
    return SpectrumSweep(
        NOW, NOW + timedelta(seconds=1), 1, 24e6, 26e6,
        (24.5e6, 25.5e6), 1e6, (-40.0, -50.0), "test", sequence=sequence,
    )


def test_normal_enqueue_write_and_ordered_flush():
    written = []
    sink = AsyncMeasurementSink(written.append, max_queue=4, enqueue_timeout=0)
    receipts = [sink.store_sweep(sweep(number)) for number in range(1, 4)]

    assert [receipt.status for receipt in receipts] == ["accepted"] * 3
    sink.flush()
    assert [item.sequence for item in written] == [1, 2, 3]
    assert [receipt.status for receipt in receipts] == ["persisted"] * 3
    sink.close()
    assert not sink._writer.is_alive()


def test_slow_sink_does_not_block_acquisition_forever():
    started = Event()
    release = Event()
    written = []

    def slow_write(item):
        started.set()
        assert release.wait(1)
        written.append(item)

    sink = AsyncMeasurementSink(slow_write, max_queue=1, enqueue_timeout=0)
    first = sink.store_sweep(sweep(1))
    assert started.wait(1)
    second = sink.store_sweep(sweep(2))
    assert first.status == "accepted"
    assert second.status == "accepted"
    release.set()
    sink.flush()
    sink.close()
    assert [item.sequence for item in written] == [1, 2]


def test_queue_full_is_explicit_rejection_and_counter():
    started = Event()
    release = Event()

    def blocked_write(_item):
        started.set()
        assert release.wait(1)

    sink = AsyncMeasurementSink(blocked_write, max_queue=1, enqueue_timeout=0)
    sink.store_sweep(sweep(1))
    assert started.wait(1)
    sink.store_sweep(sweep(2))
    rejected = sink.store_sweep(sweep(3))

    assert rejected.status == "rejected"
    assert sink.rejected_count == 1
    release.set()
    sink.close()


def test_persistence_failure_is_failed_not_persisted():
    def fail(_item):
        raise OSError("backend unavailable")

    sink = AsyncMeasurementSink(fail, enqueue_timeout=0)
    receipt = sink.store_sweep(sweep(1))
    with pytest.raises(MeasurementPersistenceError):
        sink.flush()
    assert receipt.status == "failed"
    assert sink.failed_count == 1
    with pytest.raises(MeasurementPersistenceError):
        sink.close()
    assert not sink._writer.is_alive()


def test_close_drains_and_flush_is_idempotent():
    written = []
    sink = AsyncMeasurementSink(written.append, enqueue_timeout=0)
    sink.store_sweep(sweep(1))
    sink.close()
    sink.flush()
    sink.close()
    assert [item.sequence for item in written] == [1]
