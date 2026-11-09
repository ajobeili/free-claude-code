import os
import subprocess
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).parents[1] / "Start FCC Trader.bat"


def test_launcher_prefers_project_environment_and_requires_python_314():
    source = LAUNCHER.read_text()

    project_environment = source.index(r".venv\Scripts\python.exe")
    portable_environment = source.index(r"python-portable\python.exe")

    assert project_environment < portable_environment
    assert "sys.version_info < (3, 14)" in source


@pytest.mark.skipif(os.name != "nt", reason="Windows batch launcher")
def test_launcher_selects_supported_project_python():
    environment = os.environ.copy()
    environment["FCC_TRADER_CHECK_ONLY"] = "1"

    result = subprocess.run(
        ["cmd", "/d", "/c", str(LAUNCHER)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert not result.stderr
    lines = result.stdout.splitlines()
    assert (
        Path(lines[-2]).resolve()
        == (LAUNCHER.parent / ".venv/Scripts/python.exe").resolve()
    )
    assert tuple(map(int, lines[-1].split("."))) >= (3, 14)
