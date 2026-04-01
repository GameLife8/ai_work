from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Config
from models.db import InMemoryStore, create_store


def main() -> int:
    store = create_store(Config)
    if isinstance(store, InMemoryStore):
        print(
            json.dumps(
                {
                    "initialized": False,
                    "backend": store.backend_name,
                    "message": "SQL backend is not active. Set STORE_BACKEND=sql or tidb and provide DATABASE_URL.",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    result = store.healthcheck()
    print(json.dumps({"initialized": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
