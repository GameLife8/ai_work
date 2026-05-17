"""Inspect a few task IDs."""
import sys


def main(task_ids: list[str]) -> int:
    from runtime import create_runtime
    from config import Config
    runtime = create_runtime(Config)
    s = runtime.async_task_service
    for tid in task_ids:
        r = s.get(tid)
        if not r:
            print(f"=== {tid} NOT FOUND ===")
            continue
        node = r["node"]
        print(f"=== {tid} on {node} status={r['status']} exit={r.get('exit_code')} ===")
        stdout = r.get("stdout") or ""
        stderr = r.get("stderr") or ""
        print("stdout (first 300):", repr(stdout[:300]))
        print("stderr (first 200):", repr(stderr[:200]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
