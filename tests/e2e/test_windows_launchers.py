import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows BAT integration")


@pytest.mark.parametrize("filename", ["start-web.bat", "start-cli.bat"])
def test_bat_help_from_another_directory(filename, tmp_path):
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", str(ROOT / filename), "--help"],
        cwd=tmp_path,
        env={**os.environ, "MODEL_SCOUT_NO_PAUSE": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Usage" in result.stdout
    content = (ROOT / filename).read_bytes()
    assert b"\r\n" in content and not content.startswith(b"\xef\xbb\xbf")


def test_bat_starts_zero_config_desktop(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    data = tmp_path / "saved data"
    process = subprocess.Popen(
        [
            "cmd.exe",
            "/d",
            "/c",
            str(ROOT / "start-web.bat"),
            "--no-browser",
            "--port",
            str(port),
            "--data-dir",
            str(data),
            "--max-history",
            "1",
        ],
        cwd=tmp_path,
        env={**os.environ, "MODEL_SCOUT_NO_PAUSE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=1
        ) as client:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                assert process.poll() is None
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            else:
                pytest.fail("BAT-started server did not become ready")
            assert client.get("/").status_code == 200
            files = list((data / "sessions").glob("*.token"))
            assert len(files) == 1
            token = files[0].read_text().strip()
            response = client.get("/providers", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200
            assert response.json()["management_enabled"] is True
            assert response.json()["providers"] == []
            assert (data / "scans.sqlite3").is_file()
    finally:
        if process.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=10
            )
        process.communicate(timeout=10)
