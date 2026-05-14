from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ENTRYPOINT_SH = Path(__file__).resolve().parents[2] / "sandbox" / "entrypoint.sh"


def test_sandbox_entrypoint_bootstraps_mock_google_adc(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config" / "amp").mkdir(parents=True)

    result = subprocess.run(
        [
            "bash",
            str(ENTRYPOINT_SH),
            "sh",
            "-lc",
            "printf '%s\n' \"$GOOGLE_APPLICATION_CREDENTIALS\" && cat \"$GOOGLE_APPLICATION_CREDENTIALS\"",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
    )

    assert result.returncode == 0, result.stderr or result.stdout
    adc_path, adc_json = result.stdout.split("\n", 1)
    assert adc_path == str(
        home / ".config" / "gcloud" / "application_default_credentials.json"
    )
    assert Path(adc_path).is_file()
    assert json.loads(adc_json) == {
        "type": "authorized_user",
        "client_id": "centaur-sandbox",
        "client_secret": "centaur-sandbox",
        "refresh_token": "centaur-sandbox",
    }
