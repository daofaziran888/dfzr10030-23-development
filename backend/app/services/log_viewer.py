"""采集日志业务规则：状态流转、字段校验与筛选口径都收在这里。

依赖等待清单的消费入口也在这里：
- 为等待中的延迟依赖补一条关联任务日志（幂等，同一计划同一依赖只记一次）；
- 读取运行计划与等待清单，返回逐一核对的缺口状态；
- 同步消费并生成待审核工作台项（幂等）；
- 记录日志与依赖回执的双向链接，调度页/日志页可以直接互相打开。
"""
from __future__ import annotations

import threading
from typing import Any

from app.services import dependency
from app.store import store

MODULE = "log_viewer"
LIST_FIELDS = ["日志编号", "关联任务", "日志级别", "记录时间", "日志内容", "错误堆栈", "处理状态", "处理人"]
REQUIRED_FIELDS = ["日志编号", "关联任务", "日志级别"]
STATUS_ORDER = ["未处理", "处理中", "已处理", "已忽略"]
ACTION_RULES = {"开始处理": "处理中", "标记已处理": "已处理", "忽略日志": "已忽略"}
NEGATIVE_ACTIONS = ["忽略日志"]

# 同一对象（计划+依赖任务）的同步只执行一次，其余直接拒绝。
_GUARD = threading.Lock()
_IN_FLIGHT: set[str] = set()


class BusyError(RuntimeError):
    pass


class log_guard:
    def __init__(self, key: str) -> None:
        self.key = key

    def __enter__(self) -> None:
        with _GUARD:
            if self.key in _IN_FLIGHT:
                raise BusyError("同一等待对象的日志同步正在执行，重复请求已拒绝")
            _IN_FLIGHT.add(self.key)

    def __exit__(self, *_: object) -> None:
        with _GUARD:
            _IN_FLIGHT.discard(self.key)


class LogViewerService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("日志编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"日志条目 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集日志可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"日志条目已{action}"

    # ---- 依赖等待清单消费 ---------------------------------------------
    def _link(self, plan_id: int, task_code: str, log_id: int, plan_code: str) -> None:
        rows = store.aux_rows(dependency.LOG_LINK_MODULE)
        for row in rows:
            if int(row.get("plan_id", 0)) == plan_id and str(row.get("任务编号") or "") == task_code:
                return
        rows.append({
            "id": store.next_id(rows),
            "plan_id": plan_id,
            "计划编号": plan_code,
            "任务编号": task_code,
            "日志编号": str(log_id),
            "记录时间": dependency.stamp(),
        })

    def sync_waiting_logs(self, plan_id: int | None = None) -> dict[str, Any]:
        """为等待清单里的延迟依赖补一条关联任务日志；同一对象只执行一次。"""
        waiting = self.waiting_items(plan_id)
        created: list[dict[str, Any]] = []
        skipped: list[str] = []
        for item in waiting:
            key = f"{item.get('plan_id')}:{item.get('依赖任务')}"
            try:
                with log_guard(key):
                    if dependency.log_link_for(int(item["plan_id"]), str(item.get("依赖任务") or "")):
                        skipped.append(key)
                        continue
                    log = self._create_waiting_log(item)
                    self._link(int(item["plan_id"]), str(item.get("依赖任务") or ""),
                               int(log["id"]), str(item.get("计划编号") or ""))
                    created.append(log)
            except BusyError as error:
                skipped.append(f"{key}（{error}）")
        return {"created": created, "skipped": skipped, "waiting": len(waiting)}

    def _create_waiting_log(self, item: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows(MODULE)
        entry = {
            "id": store.next_id(rows),
            "日志编号": f"LOG-{store.next_id(rows):04d}",
            "关联任务": str(item.get("依赖任务") or ""),
            "日志级别": "延迟",
            "记录时间": dependency.stamp(),
            "日志内容": (
                f"调度计划 {item.get('计划编号')} 等待依赖任务 {item.get('依赖任务')}："
                f"{item.get('等待原因')}；缺失分区 {item.get('缺失分区')}"
            ),
            "错误堆栈": "—",
            "处理状态": "未处理",
            "处理人": "—",
            "status": "未处理",
            "pending": True,
            "abnormal": True,
            "plan_id": int(item["plan_id"]),
            "快照版本": item.get("快照版本"),
            "依赖回执编号": None,
        }
        rows.append(entry)
        return entry

    def waiting_items(self, plan_id: int | None = None) -> list[dict[str, Any]]:
        """读取依赖等待清单（日志侧消费入口）。"""
        return self._waiting(plan_id)

    def _waiting(self, plan_id: int | None) -> list[dict[str, Any]]:
        for plan in store.rows("schedule"):
            dependency.ensure_plan(plan)
        items = dependency.all_waiting()
        if plan_id is not None:
            items = [item for item in items if int(item.get("plan_id", 0)) == plan_id]
        return items

    def verify(self, plan_id: int | None = None) -> dict[str, Any]:
        """读取运行计划与等待清单，逐条返回核对状态（只读）。"""
        for plan in store.rows("schedule"):
            dependency.ensure_plan(plan)
        plans = dependency.all_run_plans()
        waiting = dependency.all_waiting()
        if plan_id is not None:
            plans = [row for row in plans if int(row.get("plan_id", 0)) == plan_id]
            waiting = [row for row in waiting if int(row.get("plan_id", 0)) == plan_id]
        waiting_keys = {(int(row.get("plan_id", 0)), int(row.get("快照版本", 0))) for row in waiting}
        items: list[dict[str, Any]] = []
        for plan in plans:
            key = (int(plan.get("plan_id", 0)), int(plan.get("快照版本", 0)))
            stage = str(plan.get("阶段") or "")
            log_code = dependency.log_link_for(int(plan.get("plan_id", 0)), str(plan.get("依赖任务") or ""))
            if stage in ("运行中",) and str(plan.get("依赖完成状态")) != dependency.DONE:
                verdict, reason = "冲突", "计划处于运行中，但快照里没有依赖完成依据"
            elif key in waiting_keys and not log_code:
                verdict, reason = "缺日志", "等待清单有记录，但还没有关联任务日志"
            elif key in waiting_keys:
                verdict, reason = "待核对", f"等待原因：{plan.get('等待原因') or '依赖未完成'}"
            elif stage == "草稿待依赖":
                verdict, reason = "未排期", "计划仍是草稿，确认排期后才进入等待清单"
            elif str(plan.get("依赖完成状态")) == dependency.DONE:
                verdict, reason = "一致", "依赖已完成，运行计划与等待清单口径一致"
            else:
                verdict, reason = "待核对", "依赖未完成，但当前阶段不在等待清单范围内"
            items.append({
                "计划编号": plan.get("计划编号"),
                "plan_id": plan.get("plan_id"),
                "快照版本": plan.get("快照版本"),
                "关联任务": plan.get("关联任务"),
                "依赖任务": plan.get("依赖任务"),
                "计划状态": plan.get("计划状态"),
                "阶段": stage,
                "依赖完成状态": plan.get("依赖完成状态"),
                "核对状态": verdict,
                "核对说明": reason,
                "在等待清单中": key in waiting_keys,
                "关联任务日志编号": log_code,
                "依赖回执编号": plan.get("依赖回执编号"),
            })
        summary = {
            "运行计划": len(plans),
            "等待清单": len(waiting),
            "一致": sum(1 for item in items if item["核对状态"] == "一致"),
            "待核对": sum(1 for item in items if item["核对状态"] == "待核对"),
            "缺日志": sum(1 for item in items if item["核对状态"] == "缺日志"),
            "冲突": sum(1 for item in items if item["核对状态"] == "冲突"),
            "未排期": sum(1 for item in items if item["核对状态"] == "未排期"),
        }
        return {"items": items, "summary": summary}

    def workbench(self) -> list[dict[str, Any]]:
        rows = store.aux_rows(dependency.WORKBENCH_MODULE)
        return sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)

    def sync_workbench(self, plan_id: int | None = None) -> dict[str, Any]:
        """同步消费运行计划与等待清单，生成待审核工作台项；同一对象只生成一次。"""
        waiting = self.waiting_items(plan_id)
        created: list[dict[str, Any]] = []
        skipped: list[str] = []
        for item in waiting:
            key = f"{item.get('plan_id')}:{item.get('快照版本')}:{item.get('依赖任务')}"
            try:
                with log_guard(f"workbench:{key}"):
                    rows = store.aux_rows(dependency.WORKBENCH_MODULE)
                    if any(str(row.get("幂等键") or "") == key for row in rows):
                        skipped.append(key)
                        continue
                    task_log = dependency.log_link_for(int(item["plan_id"]), str(item.get("依赖任务") or ""))
                    row = {
                        "id": store.next_id(rows),
                        "幂等键": key,
                        "计划编号": item.get("计划编号"),
                        "plan_id": item.get("plan_id"),
                        "关联任务": item.get("关联任务"),
                        "依赖任务": item.get("依赖任务"),
                        "快照版本": item.get("快照版本"),
                        "计划状态": item.get("计划状态"),
                        "阶段": item.get("阶段"),
                        "依赖完成状态": item.get("依赖完成状态"),
                        "等待原因": item.get("等待原因"),
                        "关联任务日志编号": task_log,
                        "审核状态": "待审核",
                        "来源": "调度运行计划及依赖等待清单",
                        "记录时间": dependency.stamp(),
                    }
                    rows.append(row)
                    created.append(row)
            except BusyError as error:
                skipped.append(f"{key}（{error}）")
        return {"created": created, "skipped": skipped, "waiting": len(waiting)}
