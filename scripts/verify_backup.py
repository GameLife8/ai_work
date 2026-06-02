"""备份验证:连恢复后的 MySQL,逐表 COUNT(*) 跟期望对比。

期望来自 db_inventory.py 的 snapshot (用户 backup 时跑过一次)。
不匹配的表打 ❌,匹配的打 ✅。任何一行 ❌ → exit 1。
"""
from __future__ import annotations

import sys

import pymysql

# 期望值(来自 backup 前的 db_inventory.py 输出)
EXPECTED = {
    "alert_decision": 9,
    "alert_event": 9,
    "chat_message": 31,
    "chat_session": 4,
    "incident": 3,
    "incident_alert_rel": 8,
    "platform_async_task": 16,
    "platform_connection": 78,
    "platform_http_skill": 1,
    "platform_model_config": 1,
    "platform_pending_action": 26,
    "platform_prompt_segment": 0,
    "platform_runbook": 5,
    "platform_runbook_execution": 12,
    "platform_skill_call": 222,
    "platform_user": 2,
}


def main() -> int:
    conn = pymysql.connect(
        host="127.0.0.1", port=13306,
        user="root", password="test",
        database="ai_alert", charset="utf8mb4",
    )
    cur = conn.cursor()
    cur.execute("SHOW TABLES")
    actual_tables = {r[0] for r in cur.fetchall()}

    print(f"{'table':<35} {'expected':>10} {'actual':>10} {'status':>8}")
    print("-" * 70)
    ok_count = 0
    fail_count = 0
    for t in sorted(EXPECTED):
        if t not in actual_tables:
            print(f"{t:<35} {EXPECTED[t]:>10} {'(missing)':>10} {'❌':>8}")
            fail_count += 1
            continue
        cur.execute("SELECT COUNT(*) FROM `" + t + "`")
        n = cur.fetchone()[0]
        ok = n == EXPECTED[t]
        marker = "✅" if ok else "❌"
        print(f"{t:<35} {EXPECTED[t]:>10} {n:>10} {marker:>8}")
        if ok:
            ok_count += 1
        else:
            fail_count += 1

    # 还要确认没有多出来的表
    extra = actual_tables - set(EXPECTED.keys())
    for t in sorted(extra):
        cur.execute("SELECT COUNT(*) FROM `" + t + "`")
        n = cur.fetchone()[0]
        print(f"{t:<35} {'(extra)':>10} {n:>10} {'⚠️':>8}")

    print("-" * 70)
    print(f"OK: {ok_count}  FAIL: {fail_count}  EXTRA: {len(extra)}")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
