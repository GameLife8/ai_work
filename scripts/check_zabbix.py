from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Config
from services.zabbix_client import ZabbixClient


def main() -> int:
    client = ZabbixClient(
        base_url=Config.ZABBIX_BASE_URL,
        username=Config.ZABBIX_USERNAME,
        password=Config.ZABBIX_PASSWORD,
        timeout_seconds=Config.ZABBIX_TIMEOUT_SECONDS,
        use_stub=Config.USE_STUB_ZABBIX,
    )
    result = client.healthcheck()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("healthy") else 1


if __name__ == "__main__":
    raise SystemExit(main())
