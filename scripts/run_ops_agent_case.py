from __future__ import annotations

import json
import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import Config
from ops_agent import UnifiedOpsAgent
from runtime import create_runtime


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2:
        print('用法: python scripts/run_ops_agent_case.py "帮我看看 iiot-haitu_seatable 为什么起不来"')
        return 1

    runtime = create_runtime(Config)
    agent = UnifiedOpsAgent(runtime, max_steps=Config.AGENT_MAX_REASONING_STEPS)
    outcome = agent.ask(sys.argv[1])
    print(
        json.dumps(
            {"message": outcome.message, "trace": outcome.trace},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
