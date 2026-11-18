"""可观测埋点：本地 JSONL 日志（06 表 C18）。

记录 trace_id / 阶段 / LLM 调用 / 工具调用 / ReAct 步骤，可完整复现一次调用链。
评测断言（14 表）会检查 trace 中「无非白名单工具调用」「generate_questions 未被调用」等，
因此工具调用必须先经 log_tool 落 trace 再执行。
"""
import json
import threading
import time
import uuid

import config

_lock = threading.Lock()


def new_trace_id() -> str:
    return uuid.uuid4().hex[:12]


def log_event(trace_id: str, event: str, **fields) -> None:
    """追加一条 trace 日志。失败不影响主流程（日志非关键路径）。"""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "trace_id": trace_id, "event": event}
    rec.update(fields)
    try:
        with _lock:
            with TRACE_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def log_tool(trace_id: str, name: str, args_summary: str = "", ok: bool = True,
             detail: str = "") -> None:
    log_event(trace_id, "tool", tool=name, args=args_summary[:200], ok=ok, detail=detail[:300])
