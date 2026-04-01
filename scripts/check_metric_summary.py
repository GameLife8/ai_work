from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Config
from services.zabbix_client import ZabbixClient


def main(argv: list[str]) -> int:
    host_name = argv[1] if len(argv) > 1 else ""
    host_ip = argv[2] if len(argv) > 2 else ""
    if not host_name and not host_ip:
        print('Usage: python scripts/check_metric_summary.py "<host_name>" ["<host_ip>"]')
        return 1

    client = ZabbixClient(
        base_url=Config.ZABBIX_BASE_URL,
        username=Config.ZABBIX_USERNAME,
        password=Config.ZABBIX_PASSWORD,
        timeout_seconds=Config.ZABBIX_TIMEOUT_SECONDS,
        use_stub=Config.USE_STUB_ZABBIX,
    )
    result = client.debug_metric_summary(
        {
            "host_name": host_name,
            "host_ip": host_ip,
            "severity": "high",
            "tags": {},
        }
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
