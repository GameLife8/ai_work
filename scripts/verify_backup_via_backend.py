"""备份验证(从 backend 容器跑):连 backup-verify 容器,对比每表行数。"""
from __future__ import annotations

import sys

import pymysql

# 从 db_inventory.py 之前的 snapshot 拷过来
EXPECTED = {
    "alert_decision": 9, "alert_event": 9, "chat_message": 31, "chat_session": 4,
    "incident": 3, "incident_alert_rel": 8, "platform_async_task": 16,
    "platform_connection": 78, "platform_http_skill": 1, "platform_model_config": 1,
    "platform_pending_action": 26, "platform_prompt_segment": 0,
    "platform_runbook": 5, "platform_runbook_execution": 12,
    "platform_skill_call": 222, "platform_user": 2,
}

conn = pymysql.connect(
    host="backup-verify", port=3306,
    user="root", password="test",
    database="ai_alert", charset="utf8mb4",
)
cur = conn.cursor()
cur.execute("SHOW TABLES")
actual = {r[0] for r in cur.fetchall()}

print(f"{'table':<35} {'expected':>10} {'actual':>10} {'status':>8}")
print("-" * 70)
ok = fail = 0
for t in sorted(EXPECTED):
    if t not in actual:
        print(f"{t:<35} {EXPECTED[t]:>10} {'(missing)':>10}       X")
        fail += 1
        continue
    cur.execute("SELECT COUNT(*) FROM `" + t + "`")
    n = cur.fetchone()[0]
    is_ok = n == EXPECTED[t]
    marker = "OK" if is_ok else "DIFF"
    print(f"{t:<35} {EXPECTED[t]:>10} {n:>10} {marker:>8}")
    if is_ok:
        ok += 1
    else:
        fail += 1

print("-" * 70)
print(f"matched: {ok}  diff: {fail}")
sys.exit(0 if fail == 0 else 1)
