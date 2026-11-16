"""基线预警结果接口（挂载在基线管理前缀 /api/baseline 下）。

包含 alerts / waterline / timeline / triggers 等字面量路径，并在路由汇总中
先于基线定义路由注册，保证不会被 ``/{entry_id}`` 抢占。
依赖解析侧通过 ``/api/dependency/shared-water-level`` 读取同一份结果，两边水位同源。
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.baseline_alert import baseline_alert_service

router = APIRouter(prefix="/api/baseline", tags=["基线预警"])


@router.get("/alerts", response_model=PageResult[dict])
def list_alerts(
    keyword: str | None = Query(default=None, description="按基线编号或作业名称检索"),
    water_level: str | None = Query(default=None, description="正常、即将破线、已破线"),
    active_only: bool = Query(default=False, description="为 true 时只返回生效中的预警结果"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """读取基线预警结果数据：只呈现经工作日/节假日/统一时区口径重算后的水位。"""
    if size > 200:
        size = 200
    items, total = baseline_alert_service.list_alerts(
        keyword=keyword, water_level=water_level, active_only=active_only,
        page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/alerts/recompute", response_model=ActionResult)
def recompute_alerts(payload: EntryPayload) -> ActionResult:
    """从基线承诺时间与依赖触发数据重算预警。

    可带 ``baseline_id`` 只重算单条基线；不带则全量重算。
    已完成运行的历史判定冻结保留，未完成运行按最新规则口径覆盖。
    """
    values = payload.values or {}
    baseline_id = values.get("baseline_id")
    if baseline_id is not None:
        try:
            baseline_id = int(baseline_id)
        except (TypeError, ValueError):
            return ActionResult(ok=False, message="baseline_id 必须是整数")
    result = baseline_alert_service.recompute(baseline_id=baseline_id)
    return ActionResult(ok=True, message=result["message"], entry=result)


@router.get("/waterline")
def shared_waterline() -> dict:
    """基线管理侧读取的预警水位：与依赖解析清单共享同一份基线预警结果数据。"""
    items = baseline_alert_service.timeline_view()
    return {
        "module": "baseline_alert",
        "total": len(items),
        "口径": "工作日规则 + 节假日偏移 + 统一UTC时区，禁止自然日直接破线",
        "items": items,
    }


@router.get("/timeline")
def baseline_timeline() -> dict:
    """基线时间线视图：与依赖解析清单读同一份预警水位（交付核对口径）。"""
    items = baseline_alert_service.timeline_view()
    return {"module": "baseline_alert", "total": len(items), "items": items}


@router.get("/triggers", response_model=PageResult[dict])
def list_triggers(page: int = 1, size: int = 50) -> PageResult[dict]:
    """查看当前参与重算的依赖触发数据。"""
    rows = baseline_alert_service.list_triggers()
    start = max(page - 1, 0) * size
    return PageResult(items=rows[start:start + size], total=len(rows),
                      page=page, size=size)


@router.post("/triggers", response_model=ActionResult)
def ingest_trigger(payload: EntryPayload) -> ActionResult:
    """接入依赖触发数据（幂等）：晚到触发会在下一次重算时修正已生成预警。"""
    entry, message = baseline_alert_service.ingest_trigger(payload.values or {})
    if not entry:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
