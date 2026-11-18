"""采集日志接口：维护日志条目，覆盖开始处理、标记已处理、忽略日志等动作。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import dependency
from app.services.log_viewer import LogViewerService
from app.store import store

router = APIRouter(prefix="/api/log_viewer", tags=["采集日志"])

service = LogViewerService()

LIST_FIELDS = ["日志编号", "关联任务", "日志级别", "记录时间", "日志内容", "错误堆栈", "处理状态", "处理人"]
STATUSES = ["未处理", "处理中", "已处理", "已忽略"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按日志编号检索"),
    status: str | None = Query(default=None, description="未处理、处理中、已处理、已忽略"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、日志编号关键字与状态过滤采集日志列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/dependency-waiting", response_model=dict)
def dependency_waiting(plan_id: int | None = Query(default=None)) -> dict:
    """日志页读取依赖等待清单，用于补关联任务日志与打开依赖回执。"""
    return {"items": service.waiting_items(plan_id)}


@router.get("/run-plans", response_model=dict)
def run_plans(plan_id: int | None = Query(default=None)) -> dict:
    """日志页读取调度运行计划，作为核对状态的对照。"""
    for plan in store.rows("schedule"):
        dependency.ensure_plan(plan)
    items = dependency.all_run_plans()
    if plan_id is not None:
        items = [item for item in items if int(item.get("plan_id", 0)) == plan_id]
    return {"items": items}


@router.get("/verify", response_model=dict)
def verify(plan_id: int | None = Query(default=None)) -> dict:
    """读取运行计划及等待清单并返回核对状态，不改动任何数据。"""
    return service.verify(plan_id=plan_id)


@router.post("/sync-waiting-logs", response_model=ActionResult)
def sync_waiting_logs(payload: dict[str, object] | None = None) -> ActionResult:
    """为延迟依赖记录关联任务日志；同一对象只执行一次，余下拒绝。"""
    plan_id = None
    if payload and payload.get("plan_id") is not None:
        plan_id = int(payload["plan_id"])
    outcome = service.sync_waiting_logs(plan_id)
    message = f"已补 {len(outcome['created'])} 条关联任务日志，跳过 {len(outcome['skipped'])} 条"
    return ActionResult(ok=True, message=message, entry=outcome)


@router.get("/workbench", response_model=dict)
def workbench() -> dict:
    """待审核工作台项：由运行计划与等待清单同步消费生成。"""
    return {"items": service.workbench()}


@router.post("/sync-workbench", response_model=ActionResult)
def sync_workbench(payload: dict[str, object] | None = None) -> ActionResult:
    """同步消费运行计划及等待清单，生成待审核工作台项；同一对象只生成一次。"""
    plan_id = None
    if payload and payload.get("plan_id") is not None:
        plan_id = int(payload["plan_id"])
    outcome = service.sync_workbench(plan_id)
    message = f"已生成 {len(outcome['created'])} 条待审核工作台项，跳过 {len(outcome['skipped'])} 条"
    return ActionResult(ok=True, message=message, entry=outcome)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="log_viewer.csv"'})


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条日志条目明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"日志条目 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条日志条目，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="日志条目已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条日志条目执行开始处理、标记已处理、忽略日志；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
