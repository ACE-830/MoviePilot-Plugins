"""统一业务日志；上下文仅用于显示，不参与识别、同步或数据存储。"""

from contextlib import contextmanager
from contextvars import ContextVar

from app.sdk.logging import logger


_operation = ContextVar("doubanwatchingplus_log", default=("媒体匹配", "", 1, 1))


@contextmanager
def operation(task, title="", index=1, total=1):
    """同一操作的底层匹配沿用条目名称和批次进度；异常退出也会恢复上下文。"""
    current = _operation.get()
    context = current if current[0] == task and current[1] else (task, title, index, total)
    token = _operation.set(context)
    try:
        yield
    finally:
        _operation.reset(token)


def format_message(task, stage, title, detail="", *, index=1, total=1, use_context=True):
    current = _operation.get()
    if use_context and current[0] == task and current[1]:
        _, title, index, total = current
    clean = lambda value: " ".join(str(value or "").split())
    progress = f"（{index}/{total}）" if total > 1 else ""
    message = f"[{clean(task)}][{clean(stage)}] {clean(title) or '批量任务'}{progress}"
    return message + (f"｜{clean(detail)}" if detail else "")


def log_event(task, stage, title, detail="", *, level=None, index=1, total=1):
    message = format_message(task, stage, title, detail, index=index, total=total)
    write = getattr(logger, level or ("warning" if stage == "失败" else "info"))
    write(message)
    return message


def log_recognition(stage, title, detail, *, debug=False):
    """识别、匹配和写入共用当前操作名称，不再输出另一套“识别链路”格式。"""
    task = _operation.get()[0]
    phase = {"媒体识别": "识别", "同步目标": "判断", "豆瓣写入": "写入"}.get(stage, "匹配")
    # 搜索入口和候选来源仍须可辨认，阶段名本身不重复输出。
    if stage not in {"媒体识别", "同步目标", "豆瓣写入", "豆瓣匹配"}:
        detail = f"{stage}：{detail}"
    return log_event(task, phase, title, detail, level="debug" if debug else "info")
