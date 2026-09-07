import subprocess
import sys


def test_application_runs_without_hardware(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "rf_sentinel"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert result.stdout == "RF Sentinel\n"
    assert result.stderr == ""
