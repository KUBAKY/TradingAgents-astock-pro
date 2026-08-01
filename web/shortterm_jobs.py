"""短线分析后台任务 — 页面切换不中断（模块级字典存活于整个server进程）。"""

from __future__ import annotations

import threading
import time
import traceback
import uuid

_JOBS: dict[str, dict] = {}
_LATEST: dict[str, str] = {}  # kind -> job_id，跨页面/跨会话找回任务
_LOCK = threading.Lock()


def _set(job_id: str, **kw):
    with _LOCK:
        _JOBS[job_id].update(kw)


def get_job(job_id: str) -> dict | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def _run(job_id: str, fn):
    _set(job_id, status="running", started=time.time())
    try:
        result = fn()
        _set(job_id, status="done", result=result, elapsed=round(time.time() - _JOBS[job_id]["started"], 1))
    except Exception as e:
        _set(job_id, status="error", error=f"{e}\n{traceback.format_exc(limit=3)}")


def latest_job_id(kind: str) -> str | None:
    with _LOCK:
        return _LATEST.get(kind)


def start_job(kind: str, fn) -> str:
    """kind: 'stock' | 'screen'。fn: 无参 callable，返回结果。"""
    job_id = f"{kind}-{uuid.uuid4().hex[:8]}"
    with _LOCK:
        _JOBS[job_id] = {"kind": kind, "status": "queued"}
        _LATEST[kind] = job_id
    threading.Thread(target=_run, args=(job_id, fn), daemon=True).start()
    return job_id


def clear_job(job_id: str):
    with _LOCK:
        _JOBS.pop(job_id, None)
