"""资源计量业务规则：状态流转、字段校验与筛选口径都收在这里。

列表、汇总、导出、定位共用同一条数据管线：

    过滤（记录编号 / 计算引擎 / 计费周期 / 结算状态，条件叠加生效）
    -> 去重（同引擎 + 同作业 + 同周期的重复扫描只采用一条，带结算凭证者优先）
    -> 排序（按 id 稳定排序，翻页与再次定位都落在同一结果集）

因此汇总值一定与筛选后的明细吻合，列表与导出看到相同的条数和金额。
"""
from __future__ import annotations

import re
import threading
from datetime import date
from math import fsum
from typing import Any

from app.store import store

MODULE = "resource"
REQUIRED_FIELDS = ["记录编号", "计算引擎", "作业名称"]
METRIC_FIELDS = ["CPU核秒", "内存GB秒", "存储占用GB", "扫描数据量"]
ENTRY_FIELDS = REQUIRED_FIELDS + METRIC_FIELDS + ["计费周期"]
STATUS_ORDER = ["草稿", "待汇总", "已结算", "已归档"]
ACTION_RULES = {"提交汇总": "待汇总", "结算确认": "已结算", "归档记录": "已归档"}

# 单价（元）：用量 × 单价 = 金额
UNIT_PRICES = {
    "CPU核秒": 0.0002,
    "内存GB秒": 0.0001,
    "存储占用GB": 0.35,
    "扫描数据量": 0.08,
}

# 计费周期固定为 YYYY-MM；非法周期不能混入已结算
PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# 结算串行化：并发结算同一条记录时，只有一个请求能开出首个结算凭证
SETTLE_LOCK = threading.Lock()


def _to_number(value: Any) -> float | None:
    """把用量字段解析成数值；缺失或不是数值时返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _clean(value: float) -> float | int:
    rounded = round(value, 4)
    return int(rounded) if float(rounded).is_integer() else rounded


def usage_amount(row: dict[str, Any]) -> float | None:
    """按单价表计价；任一用量缺失或非数值时无法计价，返回 None。"""
    values = [_to_number(row.get(field)) for field in METRIC_FIELDS]
    if any(value is None for value in values):
        return None
    total = fsum(value * UNIT_PRICES[field] for field, value in zip(METRIC_FIELDS, values))
    return round(total, 2)


def row_amount(row: dict[str, Any]) -> float | None:
    """优先取已入账金额，缺失时按用量现算。"""
    amount = _to_number(row.get("金额"))
    if amount is not None:
        return round(amount, 2)
    return usage_amount(row)


class ResourceService:
    # ---------- 数据管线：过滤 -> 去重 -> 稳定排序 ----------

    def _filtered_rows(
        self,
        *,
        keyword: str | None = None,
        engine: str | None = None,
        period: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(store.rows(MODULE))
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("记录编号", ""))]
        if engine:
            rows = [row for row in rows if engine in str(row.get("计算引擎", ""))]
        if period:
            rows = [row for row in rows if str(row.get("计费周期", "")).strip() == period]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        # 稳定排序：同样的筛选条件永远得到同样的顺序，翻页与定位才可复现
        rows.sort(key=lambda row: int(row.get("id", 0)))
        return rows

    @staticmethod
    def _scan_rank(row: dict[str, Any]) -> tuple[int, int]:
        # 带完整结算凭证的扫描优先采用；都没有凭证时采用最新一次扫描（id 大者）
        return (1 if row.get("结算凭证") else 0, int(row.get("id", 0)))

    def _dedupe_scans(
        self, rows: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """同一作业（引擎 + 作业名称 + 计费周期）的多次扫描只采用一条。"""
        best: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in rows:
            key_parts = tuple(
                str(row.get(field, "")).strip() for field in ("计算引擎", "作业名称", "计费周期")
            )
            key = key_parts if all(key_parts) else ("__row__", int(row.get("id", 0)))
            current = best.get(key)
            if current is None or self._scan_rank(row) > self._scan_rank(current):
                best[key] = row
        adopted_ids = {id(row) for row in best.values()}
        adopted = [row for row in rows if id(row) in adopted_ids]
        duplicates = [row for row in rows if id(row) not in adopted_ids]
        return adopted, duplicates

    def _result_set(
        self,
        *,
        keyword: str | None = None,
        engine: str | None = None,
        period: str | None = None,
        status: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rows = self._filtered_rows(keyword=keyword, engine=engine, period=period, status=status)
        return self._dedupe_scans(rows)

    @staticmethod
    def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {"count": len(rows)}
        for field in METRIC_FIELDS:
            result[field] = _clean(
                fsum(v for v in (_to_number(row.get(field)) for row in rows) if v is not None)
            )
        result["金额"] = round(
            fsum(a for a in (row_amount(row) for row in rows) if a is not None), 2
        )
        return result

    # ---------- 列表 / 汇总 / 导出 / 定位 ----------

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        engine: str | None = None,
        period: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows, _ = self._result_set(keyword=keyword, engine=engine, period=period, status=status)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def summary(
        self,
        *,
        keyword: str | None = None,
        engine: str | None = None,
        period: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """成本汇总：与列表共用筛选口径；金额与用量只统计筛选后的已结算记录。"""
        rows, duplicates = self._result_set(
            keyword=keyword, engine=engine, period=period, status=status
        )
        status_counts = {name: 0 for name in STATUS_ORDER}
        for row in rows:
            name = str(row.get("status", ""))
            status_counts[name] = status_counts.get(name, 0) + 1
        settled_rows = [row for row in rows if row.get("status") == "已结算"]
        pending_rows = [row for row in rows if row.get("status") == "待汇总"]
        return {
            "filters": {"keyword": keyword, "engine": engine, "period": period, "status": status},
            "total": len(rows),
            "status_counts": status_counts,
            # 已结算结果只含已结算记录，未结算记录绝不计入
            "settled": self._aggregate(settled_rows),
            # 待汇总清单：异常记录带失败原因，等待重新结算
            "pending": {
                "count": len(pending_rows),
                "items": [
                    {
                        "id": row.get("id"),
                        "记录编号": row.get("记录编号"),
                        "计算引擎": row.get("计算引擎"),
                        "作业名称": row.get("作业名称"),
                        "计费周期": row.get("计费周期"),
                        "abnormal": bool(row.get("abnormal")),
                        "失败原因": row.get("失败原因"),
                    }
                    for row in pending_rows
                ],
            },
            "failed_count": sum(1 for row in rows if row.get("abnormal")),
            "duplicates": {
                "count": len(duplicates),
                "items": [
                    {
                        "id": row.get("id"),
                        "记录编号": row.get("记录编号"),
                        "计算引擎": row.get("计算引擎"),
                        "作业名称": row.get("作业名称"),
                        "计费周期": row.get("计费周期"),
                    }
                    for row in duplicates
                ],
            },
        }

    def export(
        self,
        *,
        keyword: str | None = None,
        engine: str | None = None,
        period: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """导出：与列表相同的筛选与去重口径，小计、总计与逐项金额可相互核对。"""
        rows, duplicates = self._result_set(
            keyword=keyword, engine=engine, period=period, status=status
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["金额"] = row_amount(row)
            items.append(item)
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in items:
            key = (str(item.get("计费周期") or ""), str(item.get("计算引擎") or ""))
            groups.setdefault(key, []).append(item)
        subtotals = [
            {"计费周期": key[0], "计算引擎": key[1], **self._aggregate(group)}
            for key, group in sorted(groups.items())
        ]
        return {
            "module": MODULE,
            "filters": {"keyword": keyword, "engine": engine, "period": period, "status": status},
            "total": len(items),
            "items": items,
            "subtotals": subtotals,
            "grand_total": self._aggregate(items),
            "duplicates_excluded": len(duplicates),
        }

    def locate(
        self,
        *,
        record_no: str | None = None,
        entry_id: int | None = None,
        size: int = 20,
        keyword: str | None = None,
        engine: str | None = None,
        period: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """在当前筛选结果集内定位记录：返回所在页与序号；筛选不变时结果集不变。"""
        rows, _ = self._result_set(keyword=keyword, engine=engine, period=period, status=status)
        target: tuple[int, dict[str, Any]] | None = None
        for index, row in enumerate(rows):
            if entry_id is not None and int(row.get("id", 0)) == entry_id:
                target = (index, row)
                break
            if record_no and str(row.get("记录编号", "")) == record_no:
                target = (index, row)
                break
        if target is None:
            return {
                "ok": False,
                "message": "记录不在当前筛选结果集内，请核对筛选条件",
                "total": len(rows),
            }
        index, row = target
        position = index + 1
        page = (position - 1) // size + 1 if size > 0 else 1
        return {
            "ok": True,
            "entry_id": row.get("id"),
            "记录编号": row.get("记录编号"),
            "position": position,
            "page": page,
            "size": size,
            "total": len(rows),
        }

    # ---------- 登记 / 队列写入 ----------

    def _validate_metering(self, values: dict[str, Any]) -> list[str]:
        problems: list[str] = []
        period = str(values.get("计费周期") or "").strip()
        if not period:
            problems.append("计费周期缺失")
        elif not PERIOD_PATTERN.match(period):
            problems.append(f"计费周期「{period}」非法，应为 YYYY-MM，非法周期不能混入已结算")
        for field in METRIC_FIELDS:
            number = _to_number(values.get(field))
            if number is None:
                problems.append(f"{field}缺失或不是数值")
            elif number < 0:
                problems.append(f"{field}不能为负数")
        return problems

    def _build_entry(self, values: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows(MODULE)
        entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        for field in ENTRY_FIELDS:
            if values.get(field) is not None:
                entry[field] = values.get(field)
        if values.get("来源模块"):
            entry["来源模块"] = values.get("来源模块")
        amount = usage_amount(entry)
        if amount is not None:
            entry["金额"] = amount
        return entry

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        entry = self._build_entry(values)
        problems = self._validate_metering(entry)
        if problems:
            # 异常记录不静默丢弃：进入待汇总清单并保留失败原因
            entry["status"] = "待汇总"
            entry["pending"] = True
            entry["abnormal"] = True
            entry["失败原因"] = "；".join(problems)
        else:
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
        store.rows(MODULE).append(entry)
        return entry, []

    def ingest_scan(self, values: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """跨模块写入资源队列：流批作业的资源扫描结果直接进入待汇总队列。"""
        entry = self._build_entry(values)
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        problems = self._validate_metering(entry)
        if missing:
            problems = [f"缺少必填字段：{'、'.join(missing)}"] + problems
        entry["status"] = "待汇总"
        entry["pending"] = True
        entry["abnormal"] = bool(problems)
        if problems:
            entry["失败原因"] = "；".join(problems)
        store.rows(MODULE).append(entry)
        return entry, problems

    # ---------- 动作流转 ----------

    def run_action(
        self, entry_id: int, action: str, values: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str, bool]:
        values = values or {}
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"计量记录 {entry_id} 不存在或已归档", False
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于资源计量可执行范围", False
        if action == "结算确认":
            return self._settle(entry, values)
        if action == "提交汇总":
            status = entry.get("status")
            if status == "已归档":
                return None, "已归档记录不能重新提交汇总", False
            if status == "已结算":
                return None, "已结算记录无需再提交汇总", False
            if status == "待汇总":
                return entry, "计量记录已处于待汇总队列", True
            entry["status"] = "待汇总"
            entry["pending"] = True
            return entry, "计量记录已提交汇总", True
        # 归档记录
        if entry.get("status") == "已归档":
            return None, "记录已归档，无需重复操作", False
        entry["status"] = "已归档"
        entry["pending"] = False
        return entry, "计量记录已归档", True

    def _settle(
        self, entry: dict[str, Any], values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str, bool]:
        with SETTLE_LOCK:
            # 重复点击或并发结算：以首个结算凭证为唯一依据，只扣一次
            voucher = str(entry.get("结算凭证") or "").strip()
            if entry.get("status") == "已结算" and voucher:
                return entry, f"重复结算被拦截：以首个结算凭证 {voucher} 为准，不重复扣减", True
            if entry.get("status") == "已归档":
                return None, "已归档记录不能结算", False
            problems = self._validate_metering(entry)
            own_period = str(entry.get("计费周期") or "").strip()
            expected_period = str(values.get("计费周期") or "").strip()
            if expected_period and expected_period != own_period:
                # 结算日期跨月末时，月初的延迟记录也不得回写到上个周期
                problems.append(
                    f"结算周期「{expected_period}」与记录计费周期「{own_period}」不一致，"
                    "延迟记录不得回写到其他周期"
                )
            if problems:
                entry["status"] = "待汇总"
                entry["pending"] = True
                entry["abnormal"] = True
                entry["失败原因"] = "；".join(problems)
                return entry, f"结算校验失败，记录已回到待汇总清单：{entry['失败原因']}", False
            entry["status"] = "已结算"
            entry["pending"] = False
            entry["abnormal"] = False
            entry["结算凭证"] = f"SETTLE-{own_period}-{int(entry.get('id', 0)):04d}"
            # 结算时间只作入账记录，绝不在结算时改写计费周期
            entry["结算时间"] = str(values.get("结算日期") or "").strip() or date.today().isoformat()
            entry["金额"] = usage_amount(entry)
            entry.pop("失败原因", None)
            return entry, f"结算完成，结算凭证 {entry['结算凭证']}", True
