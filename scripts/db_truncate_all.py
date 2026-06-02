"""清空 ai_alert 数据库内所有表(逐张 TRUNCATE),保留 schema。

为什么用 TRUNCATE 不是 DELETE
=============================
- 比 DELETE 快几个数量级(尤其 platform_skill_call 这种含大 JSON 字段)
- 自动重置 AUTO_INCREMENT(不留 id gap)
- TiDB 同样支持,语义跟 MySQL 一致

为什么关 FOREIGN_KEY_CHECKS
==========================
- incident_alert_rel 引用 incident.id + alert_event.id
- 如果先 truncate 父表会被 FK 拦住
- 关掉 → 一次性清完 → 重启 FK 检查
- TiDB 默认不强制 FK 但兼容性照 MySQL 走

执行前调用方必须确认有最新备份。脚本本身不做交互。
"""
from __future__ import annotations

import sys
import time

import pymysql

sys.path.insert(0, "/app")
from config import Config


def main() -> int:
    conn = pymysql.connect(
        host=Config.TIDB_HOST, port=Config.TIDB_PORT,
        user=Config.TIDB_USERNAME, password=Config.TIDB_PASSWORD,
        database=Config.TIDB_DATABASE, charset="utf8mb4",
        autocommit=True,
    )
    cur = conn.cursor()
    cur.execute("SHOW TABLES")
    tables = sorted(r[0] for r in cur.fetchall())
    print(f"待清空 {len(tables)} 张表 @ {Config.TIDB_DATABASE}")
    print()

    cur.execute("SET FOREIGN_KEY_CHECKS = 0")
    print("[i] FOREIGN_KEY_CHECKS = 0")
    print()

    print(f"{'#':>3} {'table':<35} {'before':>8} {'after':>6} {'ms':>6} {'status':>8}")
    print("-" * 75)
    failures = []
    for i, t in enumerate(tables, 1):
        cur.execute("SELECT COUNT(*) FROM `" + t + "`")
        before = cur.fetchone()[0]
        t0 = time.time()
        try:
            cur.execute("TRUNCATE TABLE `" + t + "`")
            elapsed_ms = int((time.time() - t0) * 1000)
            cur.execute("SELECT COUNT(*) FROM `" + t + "`")
            after = cur.fetchone()[0]
            status = "OK" if after == 0 else "FAIL"
            if after != 0:
                failures.append(t)
        except Exception as exc:
            elapsed_ms = int((time.time() - t0) * 1000)
            after = "ERR"
            status = "ERR"
            failures.append((t, str(exc)))
            print(f"  [error] {t}: {exc}")
        print(f"{i:>3} {t:<35} {before:>8} {str(after):>6} {elapsed_ms:>6} {status:>8}")

    cur.execute("SET FOREIGN_KEY_CHECKS = 1")
    print()
    print("[i] FOREIGN_KEY_CHECKS = 1")
    print()

    if failures:
        print(f"❌ 失败 {len(failures)} 张:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"✓ 全部 {len(tables)} 张表已清空")
    return 0


if __name__ == "__main__":
    sys.exit(main())
