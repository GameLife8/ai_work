from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_init_db_script_reports_memory_mode_when_sql_disabled():
    env = os.environ.copy()
    env["STORE_BACKEND"] = "memory"

    result = subprocess.run(
        [sys.executable, "scripts/init_db.py"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert '"backend": "memory"' in result.stdout
