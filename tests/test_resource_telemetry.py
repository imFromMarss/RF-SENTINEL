import json
import os
from dataclasses import asdict
from threading import Event

from rf_sentinel.config import Settings
from rf_sentinel.health import AcquisitionHealth, HealthOwner
from rf_sentinel.observability import LinuxResourceSampler, ResourceTelemetry, configure_operational_logging


def _fixture_tree(tmp_path):
    proc = tmp_path / "proc"
    sys = tmp_path / "sys"
    (proc / "self").mkdir(parents=True)
    (sys / "class/thermal/thermal_zone0").mkdir(parents=True)
    (sys / "devices/platform/soc/soc:firmware").mkdir(parents=True)
    (proc / "device-tree").mkdir()
    (proc / "self/stat").write_text("1 (rf sentinel) S " + " ".join(["0"] * 10 + ["10", "5"]), encoding="utf-8")
    (proc / "self/statm").write_text("100 8 0 0 0 0 0\n", encoding="utf-8")
    (proc / "stat").write_text("cpu 100 0 40 60 0 0 0 0\n", encoding="utf-8")
    (proc / "meminfo").write_text("MemTotal:       1024 kB\nMemAvailable:    512 kB\n", encoding="utf-8")
    (proc / "diskstats").write_text("8 0 sda 1 2 3 4 5 6 7 8 9 10 11 12\n", encoding="utf-8")
    (sys / "class/thermal/thermal_zone0/temp").write_text("42000\n", encoding="utf-8")
    (sys / "class/thermal/thermal_zone0/type").write_text("cpu-thermal\n", encoding="utf-8")
    (proc / "device-tree/compatible").write_bytes(b"raspberrypi,cm4\x00brcm,bcm2711\x00")
    return proc, sys


class _CommandResult:
    def __init__(self, stdout):
        self.stdout = stdout


def _throttling_runner(stdout):
    return lambda command, **kwargs: _CommandResult(stdout)


def test_proc_sys_metrics_parse_and_cpu_delta(tmp_path):
    proc, sys = _fixture_tree(tmp_path)
    ticks = iter([0.0, 1.0])
    sampler = LinuxResourceSampler(HealthOwner(tmp_path / "health.json", AcquisitionHealth.started()),
                                   proc_root=proc, sys_root=sys, clock=lambda: next(ticks),
                                   throttling_command_runner=_throttling_runner("throttled=0x0\n"))
    first = sampler.sample()
    (proc / "self/stat").write_text("1 (rf sentinel) S " + " ".join(["0"] * 10 + ["25", "5"]), encoding="utf-8")
    (proc / "stat").write_text("cpu 200 0 80 120 0 0 0 0\n", encoding="utf-8")
    second = sampler.sample()
    assert first.process_cpu_percent is None
    assert second.process_cpu_percent == 15.0
    assert second.system_cpu_percent == 70.0
    assert second.process_rss_bytes == 8 * os.sysconf("SC_PAGE_SIZE")
    assert second.system_memory_total_bytes == 1024 * 1024
    assert second.disk_read_bytes == 3 * 512
    assert second.disk_write_bytes == 7 * 512
    assert first.disk_read_delta_bytes is None
    assert second.disk_read_delta_bytes == 0
    assert second.disk_write_delta_bytes == 0
    assert second.cpu_temperature_celsius == 42.0
    assert second.throttling_current_flags == {
        "under_voltage": False, "arm_frequency_capped": False,
        "throttled": False, "soft_temperature_limit": False}
    assert second.throttling_sticky_flags == {
        "under_voltage": False, "arm_frequency_capped": False,
        "throttled": False, "soft_temperature_limit": False}


def test_temperature_requires_verified_cpu_thermal_source(tmp_path):
    proc, sys = _fixture_tree(tmp_path)
    zone1 = sys / "class/thermal/thermal_zone1"
    zone1.mkdir()
    (zone1 / "type").write_text("ambient\n", encoding="utf-8")
    (zone1 / "temp").write_text("19000\n", encoding="utf-8")
    sampler = LinuxResourceSampler(HealthOwner(tmp_path / "health.json", AcquisitionHealth.started()),
                                   proc_root=proc, sys_root=sys,
                                   throttling_command_runner=_throttling_runner("bad"))
    assert sampler._temperature() == 42.0


def test_multiple_cpu_thermal_zones_are_ambiguous(tmp_path):
    proc, sys = _fixture_tree(tmp_path)
    zone1 = sys / "class/thermal/thermal_zone1"
    zone1.mkdir()
    (zone1 / "type").write_text("cpu-core\n", encoding="utf-8")
    (zone1 / "temp").write_text("43000\n", encoding="utf-8")
    sampler = LinuxResourceSampler(HealthOwner(tmp_path / "health.json", AcquisitionHealth.started()),
                                   proc_root=proc, sys_root=sys)
    assert sampler._temperature() is None


def test_malformed_or_unavailable_temperature_is_null(tmp_path):
    proc, sys = _fixture_tree(tmp_path)
    (sys / "class/thermal/thermal_zone0/type").write_text("cpu-thermal\n", encoding="utf-8")
    (sys / "class/thermal/thermal_zone0/temp").write_text("not-a-temperature\n", encoding="utf-8")
    sampler = LinuxResourceSampler(HealthOwner(tmp_path / "health.json", AcquisitionHealth.started()),
                                   proc_root=proc, sys_root=sys)
    assert sampler._temperature() is None


def test_raspberry_pi_throttling_current_and_sticky_flags_are_separate(tmp_path):
    proc, sys = _fixture_tree(tmp_path)
    sampler = LinuxResourceSampler(HealthOwner(tmp_path / "health.json", AcquisitionHealth.started()),
                                   proc_root=proc, sys_root=sys,
                                   throttling_command_runner=_throttling_runner("throttled=0x90005\n"),
                                   throttling_timeout_seconds=0.25)
    current, sticky = sampler._throttling()
    assert current == {"under_voltage": True, "arm_frequency_capped": False,
                       "throttled": True, "soft_temperature_limit": False}
    assert sticky == {"under_voltage": True, "arm_frequency_capped": False,
                      "throttled": False, "soft_temperature_limit": True}


def test_throttling_unavailable_and_malformed_sources_are_null(tmp_path):
    proc, sys = _fixture_tree(tmp_path)
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("vcgencmd")
    sampler = LinuxResourceSampler(HealthOwner(tmp_path / "health.json", AcquisitionHealth.started()),
                                   proc_root=proc, sys_root=sys,
                                   throttling_command_runner=unavailable)
    assert sampler._throttling() == (None, None)
    sampler._throttling_command_runner = _throttling_runner("unexpected")
    assert sampler._throttling() == (None, None)
    (proc / "device-tree/compatible").unlink()
    assert sampler._throttling() == (None, None)


def test_non_raspberry_pi_throttling_is_cleanly_unavailable(tmp_path):
    proc, sys = _fixture_tree(tmp_path)
    (proc / "device-tree/compatible").write_bytes(b"intel,server\x00")
    called = False
    def runner(*args, **kwargs):
        nonlocal called
        called = True
        return _CommandResult("throttled=0x0")
    sampler = LinuxResourceSampler(HealthOwner(tmp_path / "health.json", AcquisitionHealth.started()),
                                   proc_root=proc, sys_root=sys,
                                   throttling_command_runner=runner)
    assert sampler._throttling() == (None, None)
    assert not called


def test_process_cpu_counter_reset_returns_null_then_recovers(tmp_path):
    proc, sys = _fixture_tree(tmp_path)
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0])
    sampler = LinuxResourceSampler(
        HealthOwner(tmp_path / "health.json", AcquisitionHealth.started()),
        proc_root=proc, sys_root=sys, clock=lambda: next(ticks))

    assert sampler.sample().process_cpu_percent is None
    (proc / "self/stat").write_text(
        "1 (rf sentinel) S " + " ".join(["0"] * 10 + ["20", "5"]),
        encoding="utf-8")
    assert sampler.sample().process_cpu_percent == 10.0
    (proc / "self/stat").write_text(
        "1 (rf sentinel) S " + " ".join(["0"] * 10 + ["5", "5"]),
        encoding="utf-8")
    assert sampler.sample().process_cpu_percent is None
    (proc / "self/stat").write_text(
        "1 (rf sentinel) S " + " ".join(["0"] * 10 + ["15", "5"]),
        encoding="utf-8")
    assert sampler.sample().process_cpu_percent == 10.0


def test_process_cpu_is_independent_of_system_cpu_source(tmp_path):
    proc, sys = _fixture_tree(tmp_path)
    ticks = iter([0.0, 1.0])
    sampler = LinuxResourceSampler(
        HealthOwner(tmp_path / "health.json", AcquisitionHealth.started()),
        proc_root=proc, sys_root=sys, clock=lambda: next(ticks))

    assert sampler.sample().process_cpu_percent is None
    (proc / "self/stat").write_text(
        "1 (rf sentinel) S " + " ".join(["0"] * 10 + ["25", "5"]),
        encoding="utf-8")
    (proc / "stat").unlink()
    second = sampler.sample()
    assert second.process_cpu_percent == 15.0
    assert second.system_cpu_percent is None


def test_process_cpu_invalid_wall_delta_does_not_return_negative_or_poison_baseline(tmp_path):
    proc, sys = _fixture_tree(tmp_path)
    ticks = iter([1.0, 1.0, 2.0])
    sampler = LinuxResourceSampler(
        HealthOwner(tmp_path / "health.json", AcquisitionHealth.started()),
        proc_root=proc, sys_root=sys, clock=lambda: next(ticks))

    assert sampler.sample().process_cpu_percent is None
    (proc / "self/stat").write_text(
        "1 (rf sentinel) S " + " ".join(["0"] * 10 + ["20", "5"]),
        encoding="utf-8")
    assert sampler.sample().process_cpu_percent is None
    (proc / "self/stat").write_text(
        "1 (rf sentinel) S " + " ".join(["0"] * 10 + ["30", "5"]),
        encoding="utf-8")
    assert sampler.sample().process_cpu_percent == 10.0


def test_unavailable_metrics_are_null(tmp_path):
    proc = tmp_path / "proc"
    sys = tmp_path / "sys"
    owner = HealthOwner(tmp_path / "health.json", AcquisitionHealth.started())
    sampler = LinuxResourceSampler(owner, proc_root=proc, sys_root=sys)
    snapshot = sampler.sample()
    assert snapshot.process_cpu_percent is None
    assert snapshot.process_rss_bytes is None
    assert snapshot.system_memory_total_bytes is None
    assert snapshot.disk_read_bytes is None
    assert snapshot.disk_read_delta_bytes is None
    assert snapshot.cpu_temperature_celsius is None
    assert snapshot.throttling_current_flags is None
    assert snapshot.throttling_sticky_flags is None


def test_resource_aggregates_first_and_multiple_valid_samples_are_bounded(tmp_path):
    sampler = LinuxResourceSampler(
        HealthOwner(tmp_path / "health.json", AcquisitionHealth.started()))

    sampler._record_aggregate(ResourceTelemetry(
        "one", process_cpu_percent=10.0, process_rss_bytes=100,
        system_cpu_percent=20.0, system_memory_available_bytes=1000,
        cpu_temperature_celsius=40.0, disk_read_delta_bytes=3,
        disk_write_delta_bytes=5))
    assert sampler.resource_aggregates == {
        "process_cpu_percent": {"count": 1, "average": 10.0, "max": 10.0},
        "process_rss_bytes": {"count": 1, "average": 100.0, "max": 100},
        "system_cpu_percent": {"count": 1, "average": 20.0, "max": 20.0},
        "system_memory_available_bytes": {"count": 1, "average": 1000.0, "min": 1000},
        "cpu_temperature_celsius": {"count": 1, "average": 40.0, "max": 40.0},
        "disk_read_delta_bytes": {"total": 3, "max": 3},
        "disk_write_delta_bytes": {"total": 5, "max": 5},
    }

    sampler._record_aggregate(ResourceTelemetry(
        "two", process_cpu_percent=30.0, process_rss_bytes=300,
        system_cpu_percent=10.0, system_memory_available_bytes=700,
        cpu_temperature_celsius=50.0, disk_read_delta_bytes=7,
        disk_write_delta_bytes=2))
    aggregates = sampler.resource_aggregates
    assert aggregates["process_cpu_percent"] == {"count": 2, "average": 20.0, "max": 30.0}
    assert aggregates["process_rss_bytes"] == {"count": 2, "average": 200.0, "max": 300}
    assert aggregates["system_cpu_percent"] == {"count": 2, "average": 15.0, "max": 20.0}
    assert aggregates["system_memory_available_bytes"] == {
        "count": 2, "average": 850.0, "min": 700}
    assert aggregates["cpu_temperature_celsius"] == {"count": 2, "average": 45.0, "max": 50.0}
    assert aggregates["disk_read_delta_bytes"] == {"total": 10, "max": 7}
    assert aggregates["disk_write_delta_bytes"] == {"total": 7, "max": 5}
    assert len(aggregates) == 7


def test_resource_aggregates_ignore_null_samples_and_recover(tmp_path):
    sampler = LinuxResourceSampler(
        HealthOwner(tmp_path / "health.json", AcquisitionHealth.started()))
    sampler._record_aggregate(ResourceTelemetry("unavailable"))
    assert sampler.resource_aggregates == {}
    sampler._record_aggregate(ResourceTelemetry("valid", process_cpu_percent=12.0,
                                                 disk_read_delta_bytes=4))
    sampler._record_aggregate(ResourceTelemetry("reset", process_cpu_percent=None,
                                                 disk_read_delta_bytes=None))
    sampler._record_aggregate(ResourceTelemetry("recovered", process_cpu_percent=18.0,
                                                 disk_read_delta_bytes=6))
    assert sampler.resource_aggregates["process_cpu_percent"] == {
        "count": 2, "average": 15.0, "max": 18.0}
    assert sampler.resource_aggregates["disk_read_delta_bytes"] == {
        "total": 10, "max": 6}


def test_resource_aggregates_are_written_to_health_for_custom_sampler(tmp_path):
    owner = HealthOwner(tmp_path / "health.json", AcquisitionHealth.started())
    sampler = LinuxResourceSampler(owner, sample_fn=lambda: ResourceTelemetry(
        "now", process_rss_bytes=123))
    sample = sampler._sample_fn()
    sampler._record_aggregate(sample)
    owner.state.resource_telemetry = asdict(sample)
    owner.state.resource_aggregates = sampler.resource_aggregates
    owner.save()
    values = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
    assert values["resource_telemetry"]["process_rss_bytes"] == 123
    assert values["resource_aggregates"]["process_rss_bytes"] == {
        "count": 1, "average": 123.0, "max": 123}


def _disk_sampler(tmp_path, diskstats, sys_devices=()):
    proc = tmp_path / "proc"
    sys = tmp_path / "sys"
    proc.mkdir()
    (proc / "diskstats").write_text(diskstats, encoding="utf-8")
    dev_block = sys / "dev/block"
    dev_block.mkdir(parents=True)
    for name, major, minor, target, partition in sys_devices:
        target_path = sys / target
        target_path.mkdir(parents=True)
        if partition:
            (target_path / "partition").write_text("1\n", encoding="utf-8")
        (dev_block / f"{major}:{minor}").symlink_to(target_path)
    return LinuxResourceSampler(
        HealthOwner(tmp_path / "health.json", AcquisitionHealth.started()),
        proc_root=proc, sys_root=sys, clock=lambda: 0.0)


def test_diskstats_prefers_whole_device_over_partition(tmp_path):
    sampler = _disk_sampler(
        tmp_path,
        "8 0 sda 0 0 100 0 0 0 200 0 0 0 0 0\n"
        "8 1 sda1 0 0 40 0 0 0 50 0 0 0 0 0\n",
        (("sda", 8, 0, "devices/pci/block/sda", False),
         ("sda1", 8, 1, "devices/pci/block/sda/sda1", True)),
    )
    assert sampler._disk() == (100 * 512, 200 * 512, frozenset({"sda"}))


def test_diskstats_uses_multiple_physical_devices(tmp_path):
    sampler = _disk_sampler(
        tmp_path,
        "8 0 sda 0 0 3 0 0 0 7 0 0 0 0 0\n"
        "8 16 sdb 0 0 5 0 0 0 11 0 0 0 0 0\n",
        (("sda", 8, 0, "devices/pci/block/sda", False),
         ("sdb", 8, 16, "devices/usb/block/sdb", False)),
    )
    assert sampler._disk()[:2] == (8 * 512, 18 * 512)


def test_diskstats_ignores_loop_device(tmp_path):
    sampler = _disk_sampler(
        tmp_path,
        "7 0 loop0 0 0 100 0 0 0 200 0 0 0 0 0\n"
        "8 0 sda 0 0 3 0 0 0 7 0 0 0 0 0\n",
        (("loop0", 7, 0, "devices/virtual/block/loop0", False),
         ("sda", 8, 0, "devices/pci/block/sda", False)),
    )
    assert sampler._disk()[:2] == (3 * 512, 7 * 512)


def test_disk_deltas_first_normal_reset_and_recovery(tmp_path):
    proc = tmp_path / "proc"
    sys = tmp_path / "sys"
    proc.mkdir()
    sys.mkdir()
    path = proc / "diskstats"
    sampler = LinuxResourceSampler(
        HealthOwner(tmp_path / "health.json", AcquisitionHealth.started()),
        proc_root=proc, sys_root=sys, clock=lambda: 0.0)
    path.write_text("8 0 sda 0 0 10 0 0 0 20 0 0 0 0 0\n", encoding="utf-8")
    assert sampler.sample().disk_read_delta_bytes is None
    path.write_text("8 0 sda 0 0 13 0 0 0 25 0 0 0 0 0\n", encoding="utf-8")
    sample = sampler.sample()
    assert (sample.disk_read_delta_bytes, sample.disk_write_delta_bytes) == (3 * 512, 5 * 512)
    path.write_text("8 0 sda 0 0 2 0 0 0 4 0 0 0 0 0\n", encoding="utf-8")
    sample = sampler.sample()
    assert sample.disk_read_delta_bytes is None and sample.disk_write_delta_bytes is None
    path.write_text("8 0 sda 0 0 5 0 0 0 9 0 0 0 0 0\n", encoding="utf-8")
    sample = sampler.sample()
    assert (sample.disk_read_delta_bytes, sample.disk_write_delta_bytes) == (3 * 512, 5 * 512)


def test_disk_invalid_sample_and_device_change_recover_baseline(tmp_path):
    proc = tmp_path / "proc"
    sys = tmp_path / "sys"
    proc.mkdir()
    sys.mkdir()
    path = proc / "diskstats"
    sampler = LinuxResourceSampler(
        HealthOwner(tmp_path / "health.json", AcquisitionHealth.started()),
        proc_root=proc, sys_root=sys, clock=lambda: 0.0)
    path.write_text("8 0 sda 0 0 10 0 0 0 20 0 0 0 0 0\n", encoding="utf-8")
    sampler.sample()
    path.write_text("malformed\n", encoding="utf-8")
    assert sampler.sample().disk_read_bytes is None
    path.write_text("8 0 sda 0 0 12 0 0 0 23 0 0 0 0 0\n", encoding="utf-8")
    assert sampler.sample().disk_read_delta_bytes is None
    path.write_text("8 0 sda 0 0 15 0 0 0 30 0 0 0 0 0\n", encoding="utf-8")
    assert sampler.sample().disk_read_delta_bytes == 3 * 512
    path.write_text(
        "8 0 sda 0 0 16 0 0 0 31 0 0 0 0 0\n"
        "8 16 sdb 0 0 4 0 0 0 8 0 0 0 0 0\n", encoding="utf-8")
    assert sampler.sample().disk_read_delta_bytes is None
    path.write_text(
        "8 0 sda 0 0 17 0 0 0 32 0 0 0 0 0\n"
        "8 16 sdb 0 0 6 0 0 0 10 0 0 0 0 0\n", encoding="utf-8")
    sample = sampler.sample()
    assert (sample.disk_read_delta_bytes, sample.disk_write_delta_bytes) == (3 * 512, 3 * 512)


def test_sampler_lifecycle_failure_isolated_and_health_integrated(tmp_path):
    owner = HealthOwner(tmp_path / "health.json", AcquisitionHealth.started())
    sampled = Event()

    def sample():
        sampled.set()
        raise OSError("synthetic unavailable source")

    sampler = LinuxResourceSampler(owner, 3600, sample_fn=sample)
    assert sampler.start() is sampler
    assert sampled.wait(1)
    assert sampler.stop(1)
    assert sampler.stop(1)
    assert owner.state.resource_telemetry is None

    persisted = Event()

    def successful_sample():
        persisted.set()
        return ResourceTelemetry("now", process_rss_bytes=123)

    sampler = LinuxResourceSampler(owner, 3600, sample_fn=successful_sample)
    sampler.start()
    assert persisted.wait(1)
    assert sampler.stop(1)
    values = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
    assert values["resource_telemetry"]["process_rss_bytes"] == 123


def test_sampler_does_not_control_acquisition_stop(tmp_path):
    owner = HealthOwner(tmp_path / "health.json", AcquisitionHealth.started())
    acquisition_stop = Event()
    def sample():
        assert not acquisition_stop.is_set()
        return ResourceTelemetry("now")

    sampler = LinuxResourceSampler(
        owner, 3600, sample_fn=sample)
    sampler.start()
    assert sampler.stop(1)
    assert not acquisition_stop.is_set()


def test_resource_config_and_log_level(tmp_path):
    settings = Settings.from_env({
        "RF_SENTINEL_RESOURCE_SAMPLING_INTERVAL_SECONDS": "2.5",
        "RF_SENTINEL_LOG_LEVEL": "debug",
    })
    assert settings.resource_sampling_interval_seconds == 2.5
    assert settings.log_level == "DEBUG"
    try:
        configure_operational_logging(tmp_path / "logs", level=settings.log_level)
        import logging
        assert logging.getLogger().level == logging.DEBUG
    finally:
        logging.shutdown()
