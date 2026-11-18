"""采集调度业务规则：状态流转、依赖门禁、运行计划快照与筛选口径都收在这里。

关键边界：
- 依赖完成状态由 ``app.services.dependency`` 统一解析，路由层不做判断；
- 计划状态与阶段分开：``已排期`` + 阶段 ``等待依赖/暂运行待回执`` 都表示「未推进」；
- 没有依赖完成依据的计划可以登记、排期，但不能进入运行中，也不能产出运行批次；
- 快照按版本追加，旧版本保持不变；重算范围由计划的「适用范围」决定。
"""
from __future__ import annotations

import threading
from typing import Any

from app.services import dependency
from app.store import store

MODULE = "schedule"
LIST_FIELDS = ["计划编号", "关联任务", "调度周期", "开始时间", "结束时间", "依赖任务", "失败策略", "计划状态"]
STRUCT_FIELDS = ["延迟策略", "适用范围"]
REQUIRED_FIELDS = ["计划编号", "关联任务", "调度周期"]
STATUS_ORDER = ["草稿", "已排期", "运行中", "已完成", "已暂停"]
STAGE_ORDER = ["草稿待依赖", "等待依赖", "暂运行待回执", "已就绪", "运行中", "已完成", "已暂停"]
ACTION_RULES = {"确认排期": "已排期", "启动调度": "运行中", "暂停调度": "已暂停"}
NEGATIVE_ACTIONS = []
DELAY_POLICIES = ["停在已排期", "进入暂运行"]
SCOPES = ["仅本计划", "同依赖任务"]

# 同一对象的并发操作只允许一个执行，其余在入口处直接拒绝。
_GUARD = threading.Lock()
_IN_FLIGHT: set[str] = set()


class BusyError(RuntimeError):
    pass


class schedule_guard:
    def __init__(self, key: str) -> None:
        self.key = key

    def __enter__(self) -> None:
        with _GUARD:
            if self.key in _IN_FLIGHT:
                raise BusyError("同一调度计划的操作正在执行，重复请求已拒绝")
            _IN_FLIGHT.add(self.key)

    def __exit__(self, *_: object) -> None:
        with _GUARD:
            _IN_FLIGHT.discard(self.key)


class ScheduleService:
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
            rows = [row for row in rows if keyword.casefold() in str(row.get("计划编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        dependency.ensure_plan(entry)
        return entry

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": store.next_id(rows)}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["计划状态"] = STATUS_ORDER[0]
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["延迟策略"] = str(values.get("延迟策略") or DELAY_POLICIES[0])
        if entry["延迟策略"] not in DELAY_POLICIES:
            return None, ["延迟策略"]
        entry["适用范围"] = str(values.get("适用范围") or SCOPES[0])
        if entry["适用范围"] not in SCOPES:
            return None, ["适用范围"]
        entry["archived"] = False
        rows.append(entry)
        # 登记即生成第一版运行计划快照，等待清单在确认排期后才有意义。
        dependency.generate(entry, apply=False)
        return entry, []

    # ---- 依赖完成情况 -------------------------------------------------
    def dependency_status(self, entry_id: int) -> dict[str, Any] | None:
        plan = store.find(MODULE, entry_id)
        if plan is None:
            return None
        snapshot = dependency.ensure_plan(plan)
        return {
            "计划编号": plan.get("计划编号"),
            "关联任务": plan.get("关联任务"),
            "依赖任务": plan.get("依赖任务"),
            "计划状态": snapshot.get("计划状态"),
            "阶段": snapshot.get("阶段"),
            "依赖完成状态": snapshot.get("依赖完成状态"),
            "依赖完成依据": snapshot.get("依赖完成依据"),
            "缺失分区": snapshot.get("缺失分区"),
            "等待原因": snapshot.get("等待原因"),
            "快照版本": snapshot.get("快照版本"),
            "依赖回执编号": snapshot.get("依赖回执编号"),
            "依赖回执状态": snapshot.get("依赖回执状态"),
            "关联任务日志编号": dependency.log_link_for(entry_id, str(plan.get("依赖任务") or "")),
            "可运行": snapshot.get("是否可运行"),
        }

    def compare_options(self, entry_id: int) -> dict[str, Any] | None:
        """比较「停在已排期」与「进入暂运行」两个边界，不写任何数据。

        比较对象是一份「依赖未完成、已到开始时间」的计划：两种策略都不会在依赖
        完成前产出运行批次，区别只在状态停留位置与是否持续读取依赖回执。
        """
        plan = store.find(MODULE, entry_id)
        if plan is None:
            return None
        probe = dict(plan)
        probe["status"] = "已排期"
        probe["计划状态"] = "已排期"
        waiting = "停在已排期"
        temporary = "进入暂运行"
        return {
            "计划编号": plan.get("计划编号"),
            "依赖任务": plan.get("依赖任务"),
            "当前策略": plan.get("延迟策略"),
            "比较场景": "依赖已延迟未完成、且计划已到达开始时间",
            "选项": [
                dependency.project(probe, waiting),
                dependency.project(probe, temporary),
            ],
            "结论": (
                "两种边界都不会在依赖完成前产出运行批次；"
                "停在已排期保持已排期状态并生成等待清单，进入暂运行先进入独立暂运行阶段并持续读取依赖回执。"
            ),
        }

    # ---- 运行计划与等待清单 -------------------------------------------
    def run_plans(self, *, include_history: bool = False, plan_id: int | None = None) -> list[dict[str, Any]]:
        for plan in store.rows(MODULE):
            dependency.ensure_plan(plan)
        items = dependency.all_run_plans(include_history=include_history)
        if plan_id is not None:
            items = [item for item in items if int(item.get("plan_id", 0)) == plan_id]
        return items

    def waiting_list(self, *, plan_id: int | None = None) -> list[dict[str, Any]]:
        for plan in store.rows(MODULE):
            dependency.ensure_plan(plan)
        items = dependency.all_waiting()
        if plan_id is not None:
            items = [item for item in items if int(item.get("plan_id", 0)) == plan_id]
        return items

    def recalculate(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        plan = store.find(MODULE, entry_id)
        if plan is None:
            return None, f"调度计划 {entry_id} 不存在或已归档"
        if plan.get("archived"):
            return None, f"调度计划 {entry_id} 已归档，历史快照不可重算"
        try:
            with schedule_guard(f"{MODULE}:{entry_id}"):
                outcome = dependency.refresh_plan(plan)
        except BusyError as error:
            return None, str(error)
        affected = [item["plan"].get("计划编号") for item in outcome["plans"]]
        return outcome, f"已按适用范围「{outcome['scope']}」重算：{'、'.join(affected)}"

    # ---- 回执与批次 ----------------------------------------------------
    def report_receipt(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        task_code = str(values.get("任务编号") or "").strip()
        if not task_code:
            return None, "缺少必填字段：任务编号"
        receipt_status = str(values.get("完成状态") or "").strip()
        allowed = [dependency.RECEIPT_DONE, dependency.RECEIPT_DELAYED,
                   dependency.RECEIPT_PENDING, dependency.RECEIPT_FAILED]
        if receipt_status not in allowed:
            return None, f"完成状态必须是：{'、'.join(allowed)}"
        if not str(values.get("依赖分区") or "").strip():
            return None, "缺少必填字段：依赖分区"
        rows = store.aux_rows(dependency.RECEIPT_MODULE)
        receipt = {
            "id": store.next_id(rows),
            "任务编号": task_code,
            "完成状态": receipt_status,
            "依赖分区": str(values["依赖分区"]).strip(),
            "回执说明": values.get("回执说明"),
            "记录时间": dependency.stamp(),
        }
        rows.append(receipt)
        affected = dependency.refresh_dependents(task_code)
        message = f"依赖回执已登记，重算 {sum(len(item['plans']) for item in affected)} 条调度计划"
        return {"receipt": receipt, "重算": [item["scope"] for item in affected]}, message

    def receipts(self, task_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.aux_rows(dependency.RECEIPT_MODULE)
        if task_code:
            rows = [row for row in rows if str(row.get("任务编号") or "") == task_code]
        return sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)

    def receipt_detail(self, receipt_id: int) -> dict[str, Any] | None:
        receipt = store.aux_find(dependency.RECEIPT_MODULE, receipt_id)
        if receipt is None:
            return None
        task_code = str(receipt.get("任务编号") or "")
        logs = [
            row for row in store.rows("log_viewer")
            if str(row.get("关联任务") or "") == task_code
        ]
        return {"receipt": receipt, "logs": logs}

    def generate_batch(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        plan = store.find(MODULE, entry_id)
        if plan is None:
            return None, f"调度计划 {entry_id} 不存在或已归档"
        if plan.get("archived"):
            return None, f"调度计划 {entry_id} 已归档，不能产出运行批次"
        try:
            with schedule_guard(f"{MODULE}:{entry_id}:batch"):
                return self._generate_batch(plan, entry_id)
        except BusyError as error:
            return None, str(error)

    def _generate_batch(self, plan: dict[str, Any], entry_id: int) -> tuple[dict[str, Any] | None, str]:
        snapshot = dependency.current_snapshot(entry_id)
        if snapshot is None:
            snapshot = dependency.ensure_plan(plan)
        stage = str(snapshot.get("阶段") or "")
        dep_state = str(snapshot.get("依赖完成状态") or "")
        if plan.get("status") != "运行中" or stage != "运行中":
            return None, f"计划当前阶段为「{stage}」，只有运行中可以产出运行批次"
        if dep_state != dependency.DONE or not snapshot.get("依赖回执编号"):
            return None, f"依赖完成依据不足（{snapshot.get('依赖完成依据') or '无回执'}），依赖完成前不能产出运行批次"
        partition = str(snapshot.get("缺失分区") or "")
        existing = dependency.find_batch(entry_id, partition)
        if existing is not None:
            return None, (
                f"同一对象（计划 {plan.get('计划编号')} / 分区 {partition}）已产出运行批次"
                f"#{existing.get('id')}，重复操作已拒绝"
            )
        rows = store.aux_rows(dependency.BATCH_MODULE)
        batch = {
            "id": store.next_id(rows),
            "plan_id": entry_id,
            "计划编号": plan.get("计划编号"),
            "关联任务": plan.get("关联任务"),
            "依赖任务": plan.get("依赖任务"),
            "依赖分区": partition,
            "快照版本": snapshot.get("快照版本"),
            "依赖回执编号": snapshot.get("依赖回执编号"),
            "幂等键": dependency.batch_key(entry_id, partition),
            "状态": "已产出",
            "记录时间": dependency.stamp(),
        }
        rows.append(batch)
        plan["最近批次"] = f"#{batch['id']}"
        return {"batch": batch, "snapshot": snapshot}, f"运行批次 #{batch['id']} 已产出，分区 {partition}"

    def batches(self, plan_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.aux_rows(dependency.BATCH_MODULE)
        if plan_id is not None:
            rows = [row for row in rows if int(row.get("plan_id", 0)) == plan_id]
        return sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)

    # ---- 状态动作 ------------------------------------------------------
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"调度计划 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集调度可执行范围"
        try:
            with schedule_guard(f"{MODULE}:{entry_id}:{action}"):
                return self._apply_action(entry, action)
        except BusyError as error:
            return None, str(error)

    def _apply_action(self, entry: dict[str, Any], action: str) -> tuple[dict[str, Any] | None, str]:
        if entry.get("archived"):
            return None, f"调度计划 {entry.get('计划编号')} 已归档，不能继续流转"
        target = ACTION_RULES[action]
        status = str(entry.get("status") or "")
        if action == "确认排期":
            if status != "草稿":
                return None, f"只有草稿计划可以确认排期，当前状态为「{status}」"
            entry["status"] = "已排期"
            entry["计划状态"] = "已排期"
            dependency.generate(entry, apply=True)
            return entry, "调度计划已确认排期" + self._stage_suffix(entry)
        if action == "启动调度":
            if status != "已排期":
                return None, f"只有已排期计划可以启动调度，当前状态为「{status}」"
            dep = dependency.evaluate_dependency(entry)
            if dependency.dependency_blocks(dep):
                dependency.generate(entry, apply=True)
                return None, (
                    f"依赖任务 {dep['任务编号'] or '未绑定'} 未完成，计划不能进入运行中；"
                    f"原因：{dep['等待原因']}"
                )
            entry["status"] = "运行中"
            entry["计划状态"] = "运行中"
            dependency.generate(entry, apply=True)
            suffix = "，依赖已完成" if dep["已完成"] else "，依赖任务未登记，运行批次仍要求回执依据"
            return entry, "调度计划已启动调度" + suffix + self._stage_suffix(entry)
        # 暂停调度
        if status in ("已完成", "已暂停"):
            return None, f"当前状态「{status}」不能暂停"
        entry["status"] = "已暂停"
        entry["计划状态"] = "已暂停"
        dependency.generate(entry, apply=True)
        return entry, "调度计划已暂停调度"

    @staticmethod
    def _stage_suffix(entry: dict[str, Any]) -> str:
        stage = entry.get("阶段")
        return f"，当前阶段：{stage}" if stage else ""

    def archive(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"调度计划 {entry_id} 不存在或已归档"
        if entry.get("archived"):
            return None, f"调度计划 {entry.get('计划编号')} 已归档，重复归档已拒绝"
        status = str(entry.get("status") or "")
        if status not in ("已完成", "已暂停"):
            return None, f"只有已完成或已暂停的计划可以归档，当前状态为「{status}」"
        entry["archived"] = True
        entry["pending"] = False
        entry["归档时间"] = dependency.stamp()
        dependency.generate(entry, apply=False)
        return entry, f"调度计划 {entry.get('计划编号')} 已归档，历史快照保留"

    def restore(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"调度计划 {entry_id} 不存在或已归档"
        if not entry.get("archived"):
            return None, f"调度计划 {entry.get('计划编号')} 未归档，无需恢复"
        if str(entry.get("status") or "") != "已暂停":
            return None, f"只有已暂停的计划可以从归档恢复，当前状态为「{entry.get('status')}」"
        entry["archived"] = False
        entry["pending"] = True
        entry.pop("归档时间", None)
        dependency.generate(entry, apply=True)
        return entry, f"调度计划 {entry.get('计划编号')} 已恢复，状态仍为已暂停"

    def complete(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"调度计划 {entry_id} 不存在或已归档"
        if str(entry.get("status") or "") != "运行中":
            return None, f"只有运行中的计划可以标记完成，当前状态为「{entry.get('status')}」"
        entry["status"] = "已完成"
        entry["计划状态"] = "已完成"
        entry["pending"] = False
        dependency.generate(entry, apply=False)
        return entry, "调度计划已完成"
