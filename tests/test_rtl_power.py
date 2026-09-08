import subprocess
import traceback

import pytest

from rf_sentinel.errors import ConfigurationError, ParseError, ScanError
from rf_sentinel.rtl_power import RTLPowerScanner, parse_rtl_power
from rf_sentinel.spectrum import ScanProfile


CSV = (
    "2026-09-08, 00:00:10, 88000000, 98000000, 5000000.00, 20, -60, -20, -20\n"
    "2026-09-08, 00:00:10, 98000000, 108000000, 5000000.00, 30, -50, -40, -40\n"
    "2026-09-08, 00:00:20, 88000000, 98000000, 5000000.00, 20, -30, -25, -25\n"
    "2026-09-08, 00:00:20, 98000000, 108000000, 5000000.00, 30, -45, -40, -40\n"
)


def test_parser_stitches_hops_and_removes_duplicate():
    data = parse_rtl_power(CSV.splitlines())
    assert data.edges_hz == (88000000, 93000000, 98000000, 103000000, 108000000)
    assert len(data.frames) == 2
    assert data.frames[0].powers == (-60, -20, -50, -40)
    assert data.frames[0].sample_count == 50
    assert data.frames[0].timestamp.utcoffset().total_seconds() == 0


def test_parser_accepts_nonduplicated_and_rounded_steps():
    row = "2026-09-08, 00:00:10, 88000000, 89000000, 333333.33, 20, -60, -20, -50"
    data = parse_rtl_power([row])
    assert len(data.frames[0].powers) == 3


@pytest.mark.parametrize("source", [
    "", "bad", CSV.replace("-60", "nan"), CSV.replace("-60", "-inf"),
    CSV.replace("5000000.00", "0"), CSV.replace(", 20, -60", ", 0, -60"),
    CSV.replace("-60, -20, -20", "-60, -20, -19"),
    CSV.replace("98000000, 108000000", "99000000, 109000000"),
    "\n".join(CSV.splitlines()[:-1]), CSV + CSV.splitlines()[0],
])
def test_parser_rejects_invalid_or_incomplete_sweeps(source):
    with pytest.raises(ParseError):
        parse_rtl_power(source.splitlines())


class FakeProcess:
    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.returncode = 0
        self.killed = False
        self.terminated = False
        self.waited = False
        kwargs["stdout"].write(CSV.encode())
        kwargs["stdout"].flush()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        self.waited = True
        return self.returncode


def test_subprocess_is_bounded_and_environment_isolated(monkeypatch):
    processes = []
    def spawn(command, **kwargs):
        process = FakeProcess(command, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setenv("RF_SENTINEL_TELEGRAM_BOT_TOKEN", "synthetic-private")
    result = RTLPowerScanner(1, 20.7).scan(ScanProfile())
    process = processes[0]
    assert process.command == [
        "rtl_power", "-f", "88000000:108000000:125000", "-i", "10", "-e", "30",
        "-d", "1", "-g", "20.7", "-",
    ]
    assert process.kwargs["shell"] is False
    assert process.kwargs["stdin"] == subprocess.DEVNULL
    assert hasattr(process.kwargs["stderr"], "read")
    assert process.kwargs["env"]["TZ"] == "UTC"
    assert "RF_SENTINEL_TELEGRAM_BOT_TOKEN" not in process.kwargs["env"]
    assert result.backend == "rtl_power"
    assert process.waited


@pytest.mark.parametrize("kind", ["timeout", "exit", "missing", "io", "interrupt", "size"])
def test_subprocess_failures_and_cleanup(monkeypatch, kind, tmp_path):
    processes = []
    def spawn(command, **kwargs):
        if kind == "missing":
            raise FileNotFoundError("private")
        if kind == "io":
            raise OSError("private")
        process = FakeProcess(command, **kwargs)
        process.returncode = 1 if kind == "exit" else None
        if kind == "exit":
            kwargs["stderr"].write(b"synthetic usb failure\n")
            kwargs["stderr"].flush()
        processes.append(process)
        return process
    monkeypatch.setattr(subprocess, "Popen", spawn)
    if kind == "timeout":
        clock = iter((0, 100))
        monkeypatch.setattr("rf_sentinel.rtl_power.time.monotonic", lambda: next(clock))
    if kind == "interrupt":
        def interrupt(_):
            raise KeyboardInterrupt
        monkeypatch.setattr("rf_sentinel.rtl_power.time.sleep", interrupt)
    if kind == "size":
        monkeypatch.setattr("rf_sentinel.rtl_power.MAX_CSV_BYTES", 10)
    with pytest.raises(KeyboardInterrupt if kind == "interrupt" else ScanError) as error:
        RTLPowerScanner().scan(ScanProfile(), tmp_path / "spectrum.csv")
    assert "private" not in "".join(traceback.format_exception(error.value))
    if processes:
        assert processes[0].waited
        assert processes[0].terminated == (kind != "exit")
        assert not processes[0].killed
    if kind == "exit":
        assert error.value.reason == "subprocess_exit"
        assert error.value.returncode == 1
        assert (tmp_path / "rtl_power.stderr.txt").read_text() == "synthetic usb failure\n"


@pytest.mark.parametrize("kwargs", [
    {"low_hz": 1}, {"high_hz": 2_000_000_000}, {"bin_hz": 0},
    {"duration_seconds": 0}, {"integration_seconds": 31},
    {"duration_seconds": 301}, {"bin_hz": "125000; sh"},
    {"duration_seconds": 29}, {"high_hz": 300_000_000},
])
def test_invalid_profiles(kwargs):
    with pytest.raises(ConfigurationError):
        ScanProfile(**kwargs)


def test_out_of_range_spectrum_rejected(monkeypatch):
    def spawn(command, **kwargs):
        process = FakeProcess(command, **kwargs)
        kwargs["stdout"].seek(0)
        kwargs["stdout"].truncate()
        kwargs["stdout"].write(CSV.splitlines()[0].encode())
        return process
    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises(ScanError, match="покривають"):
        RTLPowerScanner().scan(ScanProfile())


def test_full_range_profile_and_raw_file(monkeypatch, tmp_path):
    processes = []
    def spawn(command, **kwargs):
        process = FakeProcess(command, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(subprocess, "Popen", spawn)
    raw = tmp_path / "spectrum.csv"
    profile = ScanProfile.full_range(24_000_000, 1_766_000_000)
    with pytest.raises(ScanError, match="покривають"):
        RTLPowerScanner().scan(profile, raw)
    assert raw.exists()
    assert raw.read_text() == CSV
    assert processes[0].command[2] == "24000000:1766000000:500000"
    assert processes[0].command[4:7] == ["60", "-e", "1800"]
