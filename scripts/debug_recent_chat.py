"""查最近 30 分钟的对话记录。完整 session_id 不截断。"""
from __future__ import annotations
from sqlalchemy import create_engine, text
import os


def main():
    url = os.environ.get("DATABASE_URL")
    eng = create_engine(url)
    with eng.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, session_id, role, LEFT(content, 600) AS preview, "
            "LENGTH(content) AS clen, metadata_json, created_at "
            "FROM chat_message "
            "WHERE created_at > NOW() - INTERVAL 60 MINUTE "
            "ORDER BY id DESC LIMIT 40"
        )).mappings().all()
    print(f"近 60 分钟内 {len(rows)} 条消息：\n")
    for r in rows:
        ts = r["created_at"]
        sid = r["session_id"]
        role = r["role"]
        clen = r["clen"]
        rid = r["id"]
        preview = (r["preview"] or "").replace("\n", " ")
        meta = r["metadata_json"] or ""
        marker = ""
        if "处理失败" in preview or "400" in preview or "error" in meta.lower():
            marker = " *** FAILED"
        print(f"id={rid:6d} sid={sid} role={role:9s} chars={clen:6d} ts={ts}{marker}")
        if marker:
            print(f"  META: {meta}")
        print(f"  {preview[:300]}")
        print()


if __name__ == "__main__":
    main()
