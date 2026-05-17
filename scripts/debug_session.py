"""dump 指定 session 全部消息。"""
from __future__ import annotations
import sys
from sqlalchemy import create_engine, text
import os


def main(sid: str):
    eng = create_engine(os.environ["DATABASE_URL"])
    with eng.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, role, content, metadata_json, LENGTH(content) AS clen, created_at "
            "FROM chat_message WHERE session_id = :s ORDER BY id"
        ), {"s": sid}).mappings().all()
    print(f"session {sid} 共 {len(rows)} 条消息:\n")
    for r in rows:
        rid = r["id"]
        role = r["role"]
        clen = r["clen"]
        ts = r["created_at"]
        preview = (r["content"] or "").replace("\n", " ")[:400]
        meta = r["metadata_json"] or ""
        print(f"id={rid} role={role} chars={clen} ts={ts}")
        if "处理失败" in preview or "400" in preview or "error" in (meta or "").lower():
            print(f"  *** META: {meta}")
        print(f"  {preview}")
        print()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "7d2bab08-c23e-4814-a4eb-3afa8b6d62a3")
