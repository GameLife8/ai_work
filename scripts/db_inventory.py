"""列 TiDB 库内所有表 + 行数。给 backup / truncate 流程当前置摸底。

读 ``Config.TIDB_*`` 连库;不依赖 SQLAlchemy(避免 platform store side effect)。
"""
from __future__ import annotations

import sys

import pymysql

sys.path.insert(0, "/app")
from config import Config


def main() -> None:
    conn = pymysql.connect(
        host=Config.TIDB_HOST, port=Config.TIDB_PORT,
        user=Config.TIDB_USERNAME, password=Config.TIDB_PASSWORD,
        database=Config.TIDB_DATABASE, charset="utf8mb4",
    )
    cur = conn.cursor()
    cur.execute("SHOW TABLES")
    tables = [r[0] for r in cur.fetchall()]
    print(f"DB: {Config.TIDB_DATABASE} @ {Config.TIDB_HOST}:{Config.TIDB_PORT}")
    print(f"total tables: {len(tables)}")
    print()
    print(f"{'table':<40} {'rows':>12}")
    print("-" * 55)
    total = 0
    for t in sorted(tables):
        cur.execute("SELECT COUNT(*) FROM `" + t + "`")
        n = cur.fetchone()[0]
        total += n
        print(f"{t:<40} {n:>12,}")
    print("-" * 55)
    print(f"{'TOTAL':<40} {total:>12,}")


if __name__ == "__main__":
    main()
