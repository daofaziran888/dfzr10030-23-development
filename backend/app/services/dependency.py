"""依赖任务与调度状态绑定。

这里集中处理三件事：
1. 把「采集调度计划」和「依赖任务结果」解析成依赖完成状态；
2. 生成不可变的调度运行计划快照与依赖等待清单；
3. 按阶段顺序推进计划状态，依赖未完成时绝不产出运行批次。

快照只追加、不修改：每次重算生成新版本，旧版本原样保留，历史不变。
"""
from __future__ import annotations

import threading
from datetime import date, datetime
from typing import Any

from app.store import store

# 快照生成是「读当前版本 → 写新版本」，并发读接口可能同时触发首次生成，
# 用一把锁保证同一时刻只追加一版，避免出现重复版本号。
_SNAPSHOT_LOCK = threading.RLock()

SCHEDULE_MODULE = "schedule"
TASK_MODULE = "robot_task"
RECEIPT_MODULE = "dependency_receipt"
RUN_PLAN_MODULE = "schedule_run_plan"
WAITING_MODULE = "dependency_waiting"
BATCH_MODULE = "schedule_run_batch"
WORKBENCH_MODULE = "review_workbench"
LOG_LINK_MODULE = "dependency_log_link"

DONE = "已完成"
RECEIPT_DONE = "已完成"
RECEIPT_DELAYED = "延迟完成"
RECEIPT_PENDING = "未完成"
RECEIPT_FAILED = "失败"

TERMINAL_STATUSES = {"已完成", "已暂停"}
WAITING_STAGES = {"等待依赖", "暂运行待回执"}
STAGE_BY_STATUS = {
    "草稿": "草稿待依赖",
    "已排期": "已就绪",
    "运行中": "运行中",
    "已完成": "已完成",
    "已暂停": "已暂停",
}
SCOPE_SELF = "仅本计划"
SCOPE_DEPENDENCY = "同依赖任务"


def today() -> str:
    return date.today().isoformat()


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def task_index() -> dict[str, dict[str, Any]]:
    return {str(row.get("任务编号") or ""): row for row in store.rows(TASK_MODULE)}


def latest_receipt(task_code: str) -> dict[str, Any] | None:
    """依赖任务结果可以多次上报，最新一条回执代表当前完成情况。"""
    receipts = [row for row in store.aux_rows(RECEIPT_MODULE) if str(row.get("任务编号") or "") == task_code]
    if not receipts:
        return None
    return max(receipts, key=lambda row: int(row.get("id", 0)))


def expected_partition(plan: dict[str, Any], task_code: str, receipt: dict[str, Any] | None) -> str:
    if receipt and str(receipt.get("依赖分区") or "").strip():
        return str(receipt["依赖分区"]).strip()
    start = str(plan.get("开始时间") or "")[:10]
    return f"{task_code}/dt={start}" if start else f"{task_code}/dt=未登记"


def evaluate_dependency(plan: dict[str, Any]) -> dict[str, Any]:
    """解析一个调度计划的依赖完成状态；这是运行计划与等待清单的唯一依据。

    ``可解释`` 表示依赖状态有明确出处（任务记录或回执）。没有出处的依赖按
    「未登记」处理并同样阻止推进：没有依赖完成依据的计划不能进入运行。
    """
    task_code = str(plan.get("依赖任务") or "").strip()
    tasks = task_index()
    task = tasks.get(task_code)
    receipt = latest_receipt(task_code) if task_code else None

    if not task_code:
        return {
            "任务编号": "", "任务是否存在": False, "任务状态": "",
            "回执编号": None, "回执状态": "", "依赖完成状态": "未绑定",
            "已完成": False, "可解释": False, "延迟": False, "分区": "",
            "等待原因": "计划没有绑定依赖任务，按无依赖计划处理",
            "依据": "计划未填写依赖任务",
        }

    task_status = str(task.get("status") or "") if task else ""
    receipt_status = str(receipt.get("完成状态") or "") if receipt else ""
    partition = expected_partition(plan, task_code, receipt)
    task_exists = task is not None
    evidence_parts: list[str] = []
    if receipt:
        evidence_parts.append(f"依赖回执 #{receipt.get('id')}（{receipt_status}）")
    if task_exists:
        evidence_parts.append(f"任务状态（{task_status}）")

    if receipt_status in (RECEIPT_DONE, RECEIPT_DELAYED):
        state, completed, delayed = DONE, True, receipt_status == RECEIPT_DELAYED
    elif receipt_status == RECEIPT_FAILED:
        state, completed, delayed = RECEIPT_FAILED, False, False
    elif task_exists and task_status == DONE:
        state, completed, delayed = DONE, True, False
    elif task_exists:
        state, completed, delayed = "未完成", False, False
    else:
        # 依赖任务没有登记，也没有回执：无法证明「完成」，但不属于可解释的依赖。
        state, completed, delayed = "未登记", False, False

    if completed:
        reason = ""
        if delayed:
            reason = f"上游任务 {task_code} 延迟完成，回执已确认分区 {partition}"
    elif state == "未登记":
        reason = f"依赖任务 {task_code} 未在RPA任务编排登记，也没有依赖回执"
    elif state == RECEIPT_FAILED:
        reason = f"上游任务 {task_code} 依赖失败，无法提供分区 {partition}"
    else:
        reason = f"上游任务 {task_code} 尚未完成（{task_status or '无状态'}），缺少分区 {partition}"

    return {
        "任务编号": task_code,
        "任务是否存在": task_exists,
        "任务状态": task_status,
        "回执编号": receipt.get("id") if receipt else None,
        "回执状态": receipt_status,
        "依赖完成状态": state,
        "已完成": completed,
        "可解释": task_exists or receipt is not None,
        "延迟": delayed,
        "分区": partition,
        "等待原因": reason,
        "依据": "；".join(evidence_parts) if evidence_parts else "无任务记录且无回执",
    }


def dependency_blocks(dep: dict[str, Any]) -> bool:
    """依赖是否构成推进阻塞。

    没有出处的依赖（任务未登记、也无回执）视为「未绑定有效依赖」，不阻塞推进；
    但产出运行批次仍要求明确的依赖完成依据，见 ``write_snapshot`` 的 ``是否可运行``。
    """
    return (not dep["已完成"]) and bool(dep.get("可解释"))


def _decide_stage(plan: dict[str, Any], dep: dict[str, Any], apply: bool) -> tuple[str, str, str, str | None]:
    """返回 (计划状态, 阶段, 推进结论, 等待原因)。apply=True 时才做状态推进。"""
    status = str(plan.get("status") or "草稿")
    if status in TERMINAL_STATUSES:
        return status, STAGE_BY_STATUS.get(status, status), "计划处于终态，按原状态保留", None

    start = str(plan.get("开始时间") or "")[:10]
    reached = bool(start) and start <= today()
    delayed_policy = str(plan.get("延迟策略") or "停在已排期")
    blocked = dependency_blocks(dep)

    if not blocked:
        if status == "运行中":
            return "运行中", "运行中", "依赖已完成，计划维持运行中", None
        if status == "草稿":
            return "草稿", "草稿待依赖", "依赖已完成，确认排期后可推进", None
        if dep["已完成"] and reached and apply:
            return "运行中", "运行中", "依赖已完成，推进至运行中", None
        if dep["已完成"]:
            return "已排期", "已就绪", "依赖已完成，等待推进动作", None
        return "已排期", "已就绪", "依赖未绑定有效任务，按无依赖计划处理，等待推进动作", None

    reason = dep["等待原因"] or "依赖未完成"
    if status == "运行中":
        if apply:
            return "已排期", "等待依赖", "检测到依赖未完成，运行中的计划回退到已排期并登记等待原因", reason
        return "运行中", "运行中冲突", "计划已在运行中，但依赖未完成，需推进修正", reason
    if status == "草稿":
        return "草稿", "草稿待依赖", "依赖未完成，确认排期后进入等待", reason
    if delayed_policy == "进入暂运行" and reached:
        return "已排期", "暂运行待回执", "计划已到开始时间，依赖未完成，进入暂运行并持续读取依赖回执", reason
    return "已排期", "等待依赖", "依赖未完成，计划停在已排期并生成等待清单", reason


def current_snapshot(plan_id: int) -> dict[str, Any] | None:
    snapshots = [
        row for row in store.aux_rows(RUN_PLAN_MODULE)
        if int(row.get("plan_id", 0)) == plan_id
    ]
    if not snapshots:
        return None
    return max(snapshots, key=lambda row: int(row.get("快照版本", 0)))


def plan_snapshots(plan_id: int) -> list[dict[str, Any]]:
    return [row for row in store.aux_rows(RUN_PLAN_MODULE) if int(row.get("plan_id", 0)) == plan_id]


def _apply_state(plan: dict[str, Any], status: str, stage: str) -> None:
    plan["status"] = status
    plan["计划状态"] = status
    plan["阶段"] = stage
    plan["pending"] = status not in TERMINAL_STATUSES
    plan["abnormal"] = stage in WAITING_STAGES or stage == "运行中冲突"


def _waiting_row(plan: dict[str, Any], dep: dict[str, Any], version: int) -> dict[str, Any] | None:
    status = str(plan.get("status") or "")
    stage = str(plan.get("阶段") or "")
    if dep["已完成"] or status in TERMINAL_STATUSES or status == "草稿":
        return None
    if stage not in WAITING_STAGES:
        return None
    rows = store.aux_rows(WAITING_MODULE)
    key = (int(plan.get("id", 0)), version, str(dep["任务编号"]))
    for row in rows:
        if (int(row.get("plan_id", 0)), int(row.get("快照版本", 0)), str(row.get("依赖任务") or "")) == key:
            return row
    row = {
        "id": store.next_id(rows),
        "plan_id": int(plan.get("id", 0)),
        "计划编号": str(plan.get("计划编号") or ""),
        "快照版本": version,
        "关联任务": str(plan.get("关联任务") or ""),
        "依赖任务": dep["任务编号"],
        "依赖完成状态": dep["依赖完成状态"],
        "依赖完成依据": dep["依据"],
        "缺失分区": dep["分区"],
        "等待原因": dep["等待原因"] or "依赖未完成",
        "计划状态": status,
        "阶段": stage,
        "状态": "等待中",
        "记录时间": stamp(),
    }
    rows.append(row)
    return row


def write_snapshot(
    plan: dict[str, Any],
    dep: dict[str, Any],
    *,
    status: str,
    stage: str,
    conclusion: str,
    scope: str,
) -> dict[str, Any]:
    """追加一版运行计划快照；不触碰任何旧版本。

    内容没有变化时复用最新版本，避免重复重算把历史撑成噪声。
    """
    rows = store.aux_rows(RUN_PLAN_MODULE)
    previous = current_snapshot(int(plan.get("id", 0)))
    signature = (
        status, stage, dep["依赖完成状态"], dep["回执编号"], dep["依据"], scope, dep["分区"],
    )
    if previous is not None:
        previous_signature = (
            previous.get("计划状态"), previous.get("阶段"), previous.get("依赖完成状态"),
            previous.get("依赖回执编号"), previous.get("依赖完成依据"), previous.get("适用范围"),
            previous.get("缺失分区"),
        )
        if previous_signature == signature:
            return previous
    version = max(
        (int(row.get("快照版本", 0)) for row in rows if int(row.get("plan_id", 0)) == int(plan.get("id", 0))),
        default=0,
    ) + 1
    snapshot = {
        "id": store.next_id(rows),
        "plan_id": int(plan.get("id", 0)),
        "计划编号": str(plan.get("计划编号") or ""),
        "关联任务": str(plan.get("关联任务") or ""),
        "依赖任务": dep["任务编号"],
        "快照版本": version,
        "适用范围": scope,
        "生成时间": stamp(),
        "计划状态": status,
        "阶段": stage,
        "依赖完成状态": dep["依赖完成状态"],
        "依赖完成依据": dep["依据"],
        "缺失分区": dep["分区"],
        "等待原因": dep["等待原因"] or None,
        "推进结论": conclusion,
        "是否可运行": bool(dep["已完成"] and status == "运行中"),
        "依赖回执编号": dep["回执编号"],
        "依赖回执状态": dep["回执状态"],
    }
    rows.append(snapshot)
    return snapshot


def resolve_waiting(plan_id: int, reason: str) -> None:
    """计划推进后把旧的等待项标记为已解除；记录保留，历史可追溯。"""
    for row in store.aux_rows(WAITING_MODULE):
        if int(row.get("plan_id", 0)) != plan_id:
            continue
        if row.get("状态") != "等待中":
            continue
        row["状态"] = "已解除"
        row["解除原因"] = reason
        row["解除时间"] = stamp()


def generate(plan: dict[str, Any], *, apply: bool, scope: str | None = None) -> dict[str, Any]:
    """生成/推进一版运行计划，并同步依赖等待清单。"""
    with _SNAPSHOT_LOCK:
        return _generate_locked(plan, apply=apply, scope=scope)


def _generate_locked(plan: dict[str, Any], *, apply: bool, scope: str | None) -> dict[str, Any]:
    dep = evaluate_dependency(plan)
    status, stage, conclusion, _ = _decide_stage(plan, dep, apply=apply)
    if apply:
        _apply_state(plan, status, stage)
    else:
        # 初次生成只记录现状，不改变计划状态，避免读接口产生推进副作用。
        plan["阶段"] = stage
    use_scope = scope or str(plan.get("适用范围") or SCOPE_SELF)
    snapshot = write_snapshot(plan, dep, status=status, stage=stage, conclusion=conclusion, scope=use_scope)
    plan["阶段"] = stage
    waiting = _waiting_row(plan, dep, int(snapshot["快照版本"]))
    if dep["已完成"] or status in TERMINAL_STATUSES or stage not in WAITING_STAGES:
        resolve_waiting(int(plan.get("id", 0)),
                        "依赖已完成" if dep["已完成"] else f"计划进入{stage}")
    return {"plan": plan, "snapshot": snapshot, "dependency": dep, "waiting": waiting}


def project(plan: dict[str, Any], delay_policy: str) -> dict[str, Any]:
    """只读投影：不写任何数据，用来比较两种延迟边界。

    比较场景固定为「依赖未完成且已到开始时间」这个决策点，所以基线状态设为
    已排期、开始时间视为已到达，不受计划当前是否推进或开始时间是否在未来影响。
    """
    probe = dict(plan)
    probe["延迟策略"] = delay_policy
    probe["status"] = "已排期"
    probe["计划状态"] = "已排期"
    probe["开始时间"] = today()
    dep = evaluate_dependency(probe)
    status, stage, conclusion, _ = _decide_stage(probe, dep, apply=False)
    return {
        "选项": delay_policy,
        "依赖完成状态": dep["依赖完成状态"],
        "计划状态": status,
        "阶段": stage,
        "等待原因": dep["等待原因"] if not dep["已完成"] else None,
        "推进结论": conclusion,
        "是否进入运行": status == "运行中",
        "是否产出运行批次": bool(dep["已完成"] and status == "运行中"),
        "是否读取依赖回执": stage == "暂运行待回执",
        "缺失分区": dep["分区"],
    }


def scope_plans(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """适用范围决定重算边界。

    - ``仅本计划``：只重算自己，别的计划的重算也不会带上它；
    - ``同依赖任务``：与其它声明同范围的计划一起重算，范围外计划保持原状。
    """
    scope = str(plan.get("适用范围") or SCOPE_SELF)
    if scope != SCOPE_DEPENDENCY:
        return [plan]
    dependency = str(plan.get("依赖任务") or "")
    return [
        row for row in store.rows(SCHEDULE_MODULE)
        if not row.get("archived")
        and str(row.get("依赖任务") or "") == dependency
        and str(row.get("适用范围") or SCOPE_SELF) == SCOPE_DEPENDENCY
    ]


def ensure_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """保证计划至少有一版运行计划快照，重复调用不追加版本。"""
    with _SNAPSHOT_LOCK:
        snapshot = current_snapshot(int(plan.get("id", 0)))
        if snapshot is not None:
            return snapshot
        return _generate_locked(plan, apply=False, scope=None)["snapshot"]


def refresh_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """按适用范围重算并推进：这是唯一允许改计划运行状态的依赖入口。"""
    with _SNAPSHOT_LOCK:
        scope = str(plan.get("适用范围") or SCOPE_SELF)
        results: list[dict[str, Any]] = []
        seen: set[int] = set()
        for target in scope_plans(plan):
            target_id = int(target.get("id", 0))
            if target_id in seen:
                continue
            seen.add(target_id)
            results.append(_generate_locked(target, apply=True, scope=scope))
        return {
            "scope": scope,
            "plans": results,
            "snapshot": results[0]["snapshot"] if results else None,
        }


def refresh_dependents(task_code: str) -> list[dict[str, Any]]:
    """依赖任务结果变化后，重算引用该任务的所有计划（各自按适用范围）。"""
    results: list[dict[str, Any]] = []
    seen: set[int] = set()
    for plan in store.rows(SCHEDULE_MODULE):
        if plan.get("archived") or str(plan.get("依赖任务") or "") != task_code:
            continue
        plan_id = int(plan.get("id", 0))
        if plan_id in seen:
            continue
        outcome = refresh_plan(plan)
        for item in outcome["plans"]:
            seen.add(int(item["plan"].get("id", 0)))
        results.append(outcome)
    return results


def log_link_for(plan_id: int, task_code: str) -> str | None:
    links = [
        row for row in store.aux_rows(LOG_LINK_MODULE)
        if int(row.get("plan_id", 0)) == plan_id and str(row.get("任务编号") or "") == task_code
    ]
    if not links:
        return None
    return str(max(links, key=lambda row: int(row.get("id", 0))).get("日志编号") or "")


def waiting_view(plan: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any] | None:
    stage = str(snapshot.get("阶段") or "")
    if stage not in WAITING_STAGES:
        return None
    dep_state = str(snapshot.get("依赖完成状态") or "")
    if dep_state == DONE:
        return None
    waiting_rows = [
        row for row in store.aux_rows(WAITING_MODULE)
        if int(row.get("plan_id", 0)) == int(plan.get("id", 0))
        and int(row.get("快照版本", 0)) == int(snapshot.get("快照版本", 0))
    ]
    if waiting_rows:
        source = waiting_rows[0]
    else:
        source = {
            "依赖完成状态": dep_state,
            "等待原因": snapshot.get("等待原因") or "依赖未完成",
            "缺失分区": snapshot.get("缺失分区") or "",
            "依赖完成依据": snapshot.get("依赖完成依据") or "",
            "记录时间": snapshot.get("生成时间"),
        }
    task_code = str(snapshot.get("依赖任务") or "")
    return {
        "plan_id": int(plan.get("id", 0)),
        "计划编号": snapshot.get("计划编号"),
        "关联任务": snapshot.get("关联任务"),
        "依赖任务": task_code,
        "计划状态": snapshot.get("计划状态"),
        "阶段": stage,
        "快照版本": snapshot.get("快照版本"),
        "适用范围": snapshot.get("适用范围"),
        "依赖完成状态": source.get("依赖完成状态"),
        "依赖完成依据": source.get("依赖完成依据"),
        "缺失分区": source.get("缺失分区"),
        "等待原因": source.get("等待原因"),
        "关联任务日志编号": log_link_for(int(plan.get("id", 0)), task_code),
        "依赖回执编号": snapshot.get("依赖回执编号"),
        "状态": "等待中",
        "记录时间": source.get("记录时间"),
    }


def all_waiting() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for plan in store.rows(SCHEDULE_MODULE):
        if plan.get("archived"):
            continue
        snapshot = current_snapshot(int(plan.get("id", 0))) or ensure_plan(plan)
        view = waiting_view(plan, snapshot)
        if view is not None:
            items.append(view)
    return items


def all_run_plans(include_history: bool = False) -> list[dict[str, Any]]:
    """运行计划视图：默认只看未归档计划的最新快照；历史视图保留归档计划供审计。"""
    plans: list[dict[str, Any]] = []
    for plan in store.rows(SCHEDULE_MODULE):
        if plan.get("archived") and not include_history:
            continue
        ensure_plan(plan)
        if include_history:
            snapshots = sorted(plan_snapshots(int(plan.get("id", 0))),
                               key=lambda row: int(row.get("快照版本", 0)), reverse=True)
        else:
            snapshot = current_snapshot(int(plan.get("id", 0)))
            snapshots = [snapshot] if snapshot else []
        for snapshot in snapshots:
            item = dict(snapshot)
            item["archived"] = bool(plan.get("archived"))
            item["延迟策略"] = plan.get("延迟策略")
            item["关联任务日志编号"] = log_link_for(int(plan.get("id", 0)), str(snapshot.get("依赖任务") or ""))
            plans.append(item)
    return plans


def batch_key(plan_id: int, partition: str) -> str:
    return f"{plan_id}:{partition}"


def find_batch(plan_id: int, partition: str) -> dict[str, Any] | None:
    key = batch_key(plan_id, partition)
    for row in store.aux_rows(BATCH_MODULE):
        if str(row.get("幂等键") or "") == key:
            return row
    return None
