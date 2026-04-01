from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Config
from models.db import create_store


def main() -> int:
    store = create_store(Config)
    result = store.healthcheck()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("healthy") else 1


if __name__ == "__main__":
    raise SystemExit(main())
