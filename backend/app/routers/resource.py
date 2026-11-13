"""资源计量接口：计量记录的筛选列表、成本汇总、导出、定位与结算动作。

列表、汇总、导出、定位共用同一套筛选参数（keyword / engine / period / status），
保证各入口看到的结果集一致。/summary、/export、/locate 声明在 /{entry_id} 之前，
避免被路径参数路由截获。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.resource import ResourceService

router = APIRouter(prefix="/api/resource", tags=["资源计量"])

service = ResourceService()

LIST_FIELDS = ["记录编号", "计算引擎", "作业名称", "CPU核秒", "内存GB秒", "存储占用GB", "扫描数据量", "计费周期"]
STATUSES = ["草稿", "待汇总", "已结算", "已归档"]


@router.get("/summary")
def summary_entries(
    keyword: str | None = Query(default=None, description="按记录编号检索"),
    engine: str | None = Query(default=None, description="按计算引擎过滤"),
    period: str | None = Query(default=None, description="按计费周期过滤，如 2026-10"),
    status: str | None = Query(default=None, description="草稿、待汇总、已结算、已归档"),
) -> dict[str, Any]:
    """成本汇总：与列表共用筛选条件；金额与用量只统计筛选后的已结算记录，
    待汇总清单保留失败原因，重复扫描只计入带结算凭证的一条。"""
    return service.summary(keyword=keyword, engine=engine, period=period, status=status)


@router.get("/export")
def export_entries(
    keyword: str | None = Query(default=None, description="按记录编号检索"),
    engine: str | None = Query(default=None, description="按计算引擎过滤"),
    period: str | None = Query(default=None, description="按计费周期过滤，如 2026-10"),
    status: str | None = Query(default=None, description="草稿、待汇总、已结算、已归档"),
) -> dict[str, Any]:
    """导出资源计量清单：与列表相同的筛选与去重口径，小计、总计与逐项金额可相互核对。"""
    return service.export(keyword=keyword, engine=engine, period=period, status=status)


@router.get("/locate")
def locate_entry(
    record_no: str | None = Query(default=None, description="按记录编号定位"),
    entry_id: int | None = Query(default=None, description="按记录 id 定位"),
    size: int = Query(default=20, description="定位时使用的每页条数"),
    keyword: str | None = Query(default=None, description="按记录编号检索"),
    engine: str | None = Query(default=None, description="按计算引擎过滤"),
    period: str | None = Query(default=None, description="按计费周期过滤，如 2026-10"),
    status: str | None = Query(default=None, description="草稿、待汇总、已结算、已归档"),
) -> dict[str, Any]:
    """在当前筛选结果集内定位记录所在页；筛选条件不变时再次定位仍落在原结果集。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    if record_no is None and entry_id is None:
        raise HTTPException(status_code=400, detail="请提供 record_no 或 entry_id 进行定位")
    return service.locate(
        record_no=record_no,
        entry_id=entry_id,
        size=size,
        keyword=keyword,
        engine=engine,
        period=period,
        status=status,
    )


@router.post("/queue", response_model=ActionResult)
def ingest_queue(payload: EntryPayload) -> ActionResult:
    """跨模块写入资源队列：其他模块的资源扫描结果直接写入待汇总队列；
    异常记录同样入队并保留失败原因，不静默丢弃。"""
    entry, problems = service.ingest_scan(payload.values)
    if problems:
        return ActionResult(
            ok=True,
            message=f"扫描结果已写入资源队列，但存在异常待处理：{entry.get('失败原因')}",
            entry=entry,
        )
    return ActionResult(ok=True, message="扫描结果已写入资源队列，等待汇总结算", entry=entry)


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按记录编号检索"),
    engine: str | None = Query(default=None, description="按计算引擎过滤"),
    period: str | None = Query(default=None, description="按计费周期过滤，如 2026-10"),
    status: str | None = Query(default=None, description="草稿、待汇总、已结算、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按记录编号、计算引擎、计费周期与结算状态叠加过滤；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, engine=engine, period=period, status=status, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条计量记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"计量记录 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条计量记录，缺字段时说明原因；异常记录进入待汇总清单并保留失败原因。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    if entry and entry.get("abnormal"):
        return ActionResult(
            ok=True,
            message=f"计量记录已登记，但存在异常已进入待汇总清单：{entry.get('失败原因')}",
            entry=entry,
        )
    return ActionResult(ok=True, message="计量记录已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条计量记录执行提交汇总、结算确认、归档记录。

    结算确认是幂等的：重复点击或并发请求都以首个结算凭证为准，只扣一次；
    计费周期非法或与指定结算周期不一致时，记录回到待汇总清单并保留失败原因。
    """
    action = str(payload.values.get("action") or "").strip()
    entry, message, ok = service.run_action(entry_id, action, payload.values)
    return ActionResult(ok=ok, message=message, entry=entry)
