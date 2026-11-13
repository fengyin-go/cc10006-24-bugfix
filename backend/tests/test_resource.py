"""资源计量链路测试：筛选、汇总、分页、定位、结算与导出的一致性。

种子数据覆盖的场景：
- Spark / 2026-09：已结算（RESO-0001）、待汇总（RESO-0003）、失败（RESO-0004）共存
- RESO-0001 / RESO-0002：同一作业的两次扫描，只有 RESO-0001 带结算凭证
- RESO-0009：非法计费周期 2026-13
- RESO-0005 / RESO-0006：同一作业跨两个计费周期
"""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.resource import ResourceService
from app.store import store

client = TestClient(app)


@pytest.fixture(autouse=True)
def restore_resource_table():
    """每个用例结束后还原资源计量表，保证用例互不影响。"""
    original = copy.deepcopy(store.rows("resource"))
    yield
    store._tables["resource"] = original


def list_ids(params: dict) -> tuple[list[int], int]:
    payload = client.get("/api/resource", params=params).json()
    return [item["id"] for item in payload["items"]], payload["total"]


# ---------- 筛选链路：所有条件叠加生效 ----------

def test_filters_stack_and_dedupe():
    ids, total = list_ids({"engine": "Spark", "period": "2026-09"})
    assert ids == [1, 3, 4]  # RESO-0002 是同作业重复扫描，凭证在 RESO-0001 上
    ids, total = list_ids({"engine": "Spark", "period": "2026-09", "status": "待汇总"})
    assert ids == [2, 3, 4]  # 状态过滤后 RESO-0002 成为该组唯一扫描
    ids, total = list_ids({"engine": "Spark"})
    assert total == 5  # id 1、3、4、10、12
    ids, total = list_ids({"period": "2026-09"})
    assert ids == [1, 3, 4, 5, 14]
    ids, total = list_ids({"keyword": "RESO-001"})
    assert ids == [10, 11, 12, 13, 14]


def test_same_engine_period_holds_all_states():
    """同一引擎在一个计费周期内可同时存在待汇总、已结算和失败记录。"""
    summary = client.get("/api/resource/summary", params={"engine": "Spark", "period": "2026-09"}).json()
    assert summary["status_counts"]["已结算"] == 1
    assert summary["status_counts"]["待汇总"] == 2
    assert summary["failed_count"] == 1


# ---------- 汇总：只含已结算记录，与筛选后明细吻合 ----------

def test_summary_matches_filtered_details():
    params = {"engine": "Spark", "period": "2026-09"}
    summary = client.get("/api/resource/summary", params=params).json()
    _, total = list_ids(params)
    assert summary["total"] == total == 3
    # 汇总值只统计筛选后的已结算记录，未结算记录不混入
    assert summary["settled"]["count"] == 1
    assert summary["settled"]["金额"] == pytest.approx(305.56)
    assert summary["settled"]["CPU核秒"] == 86400
    assert summary["settled"]["内存GB秒"] == 172800
    detail = client.get("/api/resource", params={**params, "status": "已结算"}).json()
    assert detail["total"] == summary["settled"]["count"]
    assert detail["items"][0]["金额"] == pytest.approx(summary["settled"]["金额"])
    # 失败记录进入待汇总清单并保留失败原因
    pending = {item["记录编号"]: item for item in summary["pending"]["items"]}
    assert set(pending) == {"RESO-0003", "RESO-0004"}
    assert pending["RESO-0003"]["失败原因"] is None
    assert "扫描数据量" in pending["RESO-0004"]["失败原因"]


def test_summary_respects_same_filters_as_list():
    for params in ({"period": "2026-10"}, {"engine": "Flink"}, {"status": "已结算"}, {"keyword": "RESO-001"}):
        summary = client.get("/api/resource/summary", params=params).json()
        _, total = list_ids(params)
        assert summary["total"] == total


# ---------- 分页与定位：不混周期，重新定位仍落在原结果集 ----------

def test_pagination_never_mixes_other_periods():
    seen: list[int] = []
    page, total = 1, None
    while True:
        payload = client.get("/api/resource", params={"period": "2026-10", "page": page, "size": 2}).json()
        total = payload["total"]
        for item in payload["items"]:
            assert item["计费周期"] == "2026-10"  # 翻到下一页也不混入别周期
        seen.extend(item["id"] for item in payload["items"])
        if page * 2 >= total:
            break
        page += 1
    assert total == 7
    assert len(seen) == len(set(seen)) == 7  # 页与页之间不重复、不遗漏


def test_locate_stays_in_same_result_set():
    params = {"period": "2026-10", "size": 2}
    first = client.get("/api/resource/locate", params={**params, "record_no": "RESO-0013"}).json()
    assert first["ok"] and first["position"] == 7 and first["page"] == 4
    # 按相同筛选条件再次定位，仍落在原结果集的同一位置
    second = client.get("/api/resource/locate", params={**params, "record_no": "RESO-0013"}).json()
    assert second == first
    page_items = client.get(
        "/api/resource", params={"period": "2026-10", "page": first["page"], "size": 2}
    ).json()
    assert "RESO-0013" in [item["记录编号"] for item in page_items["items"]]
    # 重新定位时筛选仍有效：不在当前结果集内的记录定位失败
    miss = client.get("/api/resource/locate", params={"period": "2026-10", "record_no": "RESO-0001"}).json()
    assert miss["ok"] is False


# ---------- 结算：幂等、并发安全、周期校验 ----------

def test_repeated_settle_clicks_deduct_once():
    first = client.post("/api/resource/10/actions", json={"values": {"action": "结算确认"}}).json()
    assert first["ok"]
    voucher = first["entry"]["结算凭证"]
    before = client.get("/api/resource/summary", params={"period": "2026-10"}).json()["settled"]
    second = client.post("/api/resource/10/actions", json={"values": {"action": "结算确认"}}).json()
    assert second["ok"]
    assert second["entry"]["结算凭证"] == voucher  # 以首个结算凭证为唯一依据
    assert "首个结算凭证" in second["message"]
    after = client.get("/api/resource/summary", params={"period": "2026-10"}).json()["settled"]
    assert after == before  # 重复点击只扣一次


def test_concurrent_settle_deducts_once():
    service = ResourceService()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: service.run_action(7, "结算确认", {}), range(8)))
    assert all(ok for _, _, ok in results)
    vouchers = {entry["结算凭证"] for entry, _, _ in results if entry}
    assert vouchers == {"SETTLE-2026-10-0007"}  # 并发下只开出一张凭证
    summary = service.summary(period="2026-10")
    assert summary["settled"]["count"] == 4  # id6、id8、id13 + 新结算的 id7
    assert summary["settled"]["金额"] == pytest.approx(505.14)


def test_settle_amount_counted_once_in_summary():
    before = client.get("/api/resource/summary", params={"period": "2026-10"}).json()["settled"]
    for _ in range(3):
        client.post("/api/resource/7/actions", json={"values": {"action": "结算确认"}})
    after = client.get("/api/resource/summary", params={"period": "2026-10"}).json()["settled"]
    assert after["count"] == before["count"] + 1
    assert after["金额"] == pytest.approx(round(before["金额"] + 74.64, 2))


def test_illegal_period_cannot_mix_into_settled():
    resp = client.post("/api/resource/9/actions", json={"values": {"action": "结算确认"}}).json()
    assert not resp["ok"]
    entry = resp["entry"]
    assert entry["status"] == "待汇总"
    assert entry["abnormal"] is True
    assert "非法" in entry["失败原因"]
    settled_ids, _ = list_ids({"status": "已结算", "size": 200})
    assert 9 not in settled_ids  # 非法周期不能混入已结算
    summary = client.get("/api/resource/summary", params={"engine": "Trino"}).json()
    reasons = {item["记录编号"]: item["失败原因"] for item in summary["pending"]["items"]}
    assert "非法" in reasons["RESO-0009"]


def test_delayed_record_not_backwritten_to_previous_period():
    # 月初的延迟记录指定上个周期结算 → 拒绝回写
    resp = client.post(
        "/api/resource/10/actions",
        json={"values": {"action": "结算确认", "计费周期": "2026-09"}},
    ).json()
    assert not resp["ok"]
    assert resp["entry"]["status"] == "待汇总"
    assert "回写" in resp["entry"]["失败原因"]
    settled_sep = client.get("/api/resource/summary", params={"period": "2026-09"}).json()["settled"]
    assert settled_sep["count"] == 2  # 仍只有 RESO-0001、RESO-0005
    # 结算日期跨到月末/次月，记录仍属于原计费周期
    ok = client.post(
        "/api/resource/10/actions",
        json={"values": {"action": "结算确认", "结算日期": "2026-11-30"}},
    ).json()
    assert ok["ok"]
    assert ok["entry"]["计费周期"] == "2026-10"
    assert ok["entry"]["结算时间"] == "2026-11-30"
    assert client.get("/api/resource/summary", params={"period": "2026-10"}).json()["settled"]["count"] == 4
    assert client.get("/api/resource/summary", params={"period": "2026-09"}).json()["settled"]["count"] == 2


# ---------- 重复扫描去重：只采用带完整结算凭证的一次 ----------

def test_duplicate_scans_adopt_voucher_only():
    ids, _ = list_ids({"engine": "Spark", "period": "2026-09"})
    assert ids == [1, 3, 4]
    export = client.get("/api/resource/export", params={"engine": "Spark", "period": "2026-09"}).json()
    jobs = [item["作业名称"] for item in export["items"]]
    assert jobs.count("日终汇总作业") == 1  # 导出不再重复显示同一作业
    adopted = next(item for item in export["items"] if item["作业名称"] == "日终汇总作业")
    assert adopted["记录编号"] == "RESO-0001"
    assert adopted["结算凭证"] == "SETTLE-2026-09-0001"
    summary = client.get("/api/resource/summary", params={"engine": "Spark", "period": "2026-09"}).json()
    assert summary["duplicates"]["count"] == 1
    assert summary["duplicates"]["items"][0]["记录编号"] == "RESO-0002"


# ---------- 列表与导出吻合：同一周期筛选条件，小计/总计可核对 ----------

def test_list_and_export_agree_on_count_and_amount():
    params = {"engine": "Flink"}
    listed = client.get("/api/resource", params={**params, "size": 200}).json()
    export = client.get("/api/resource/export", params=params).json()
    assert export["total"] == listed["total"]
    assert [item["id"] for item in export["items"]] == [item["id"] for item in listed["items"]]
    amounts = [item["金额"] for item in export["items"] if item["金额"] is not None]
    assert export["grand_total"]["金额"] == pytest.approx(round(sum(amounts), 2))


def test_period_filter_applies_to_list_and_export_alike():
    # 列表入口不混入旧周期，导出入口不重复显示同一作业：同一个周期筛选条件
    params = {"period": "2026-10"}
    listed = client.get("/api/resource", params={**params, "size": 200}).json()
    export = client.get("/api/resource/export", params=params).json()
    assert all(item["计费周期"] == "2026-10" for item in listed["items"])
    assert all(item["计费周期"] == "2026-10" for item in export["items"])
    assert export["total"] == listed["total"] == 7
    jobs = [(item["计算引擎"], item["作业名称"]) for item in export["items"]]
    assert len(jobs) == len(set(jobs))


def test_export_subtotals_reconcile_with_items():
    export = client.get("/api/resource/export").json()
    items = export["items"]
    assert export["grand_total"]["count"] == export["total"] == len(items)
    item_sum = round(sum(i["金额"] for i in items if i["金额"] is not None), 2)
    assert export["grand_total"]["金额"] == pytest.approx(item_sum)
    subtotal_sum = round(sum(s["金额"] for s in export["subtotals"]), 2)
    assert subtotal_sum == pytest.approx(export["grand_total"]["金额"])
    assert sum(s["count"] for s in export["subtotals"]) == export["total"]
    for subtotal in export["subtotals"]:
        group = [
            i for i in items
            if i["计费周期"] == subtotal["计费周期"] and i["计算引擎"] == subtotal["计算引擎"]
        ]
        assert subtotal["count"] == len(group)
        group_sum = round(sum(i["金额"] for i in group if i["金额"] is not None), 2)
        assert subtotal["金额"] == pytest.approx(group_sum)


# ---------- 跨模块写入资源队列 ----------

def test_cross_module_queue_ingest():
    resp = client.post("/api/resource/queue", json={"values": {
        "记录编号": "RESO-9001", "计算引擎": "SparkSQL", "作业名称": "即席查询",
        "CPU核秒": 1000, "内存GB秒": 2000, "存储占用GB": 10, "扫描数据量": 50,
        "计费周期": "2026-11", "来源模块": "stream",
    }}).json()
    assert resp["ok"]
    assert resp["entry"]["status"] == "待汇总"
    _, total = list_ids({"engine": "SparkSQL", "period": "2026-11"})
    assert total == 1
    # 异常扫描同样入队并保留失败原因
    bad = client.post("/api/resource/queue", json={"values": {
        "记录编号": "RESO-9002", "计算引擎": "SparkSQL", "作业名称": "迟到扫描",
        "CPU核秒": 5, "内存GB秒": 5, "存储占用GB": 5, "扫描数据量": 5,
        "计费周期": "2026-13", "来源模块": "batch",
    }}).json()
    assert bad["entry"]["abnormal"] is True
    assert "非法" in bad["entry"]["失败原因"]
    summary = client.get("/api/resource/summary", params={"engine": "SparkSQL"}).json()
    reasons = {item["记录编号"]: item["失败原因"] for item in summary["pending"]["items"]}
    assert "非法" in reasons["RESO-9002"]


# ---------- 登记 ----------

def test_create_entry_validation_and_abnormal_path():
    ok = client.post("/api/resource", json={"values": {
        "记录编号": "RESO-8001", "计算引擎": "Spark", "作业名称": "新作业",
        "CPU核秒": 100, "内存GB秒": 100, "存储占用GB": 10, "扫描数据量": 10,
        "计费周期": "2026-11",
    }}).json()
    assert ok["ok"] and ok["entry"]["status"] == "草稿"
    abnormal = client.post("/api/resource", json={"values": {
        "记录编号": "RESO-8002", "计算引擎": "Spark", "作业名称": "坏周期作业",
        "CPU核秒": 100, "内存GB秒": 100, "存储占用GB": 10, "扫描数据量": 10,
        "计费周期": "2026年11月",
    }}).json()
    assert abnormal["entry"]["status"] == "待汇总"
    assert abnormal["entry"]["abnormal"] is True
    assert abnormal["entry"]["失败原因"]
    missing = client.post("/api/resource", json={"values": {"记录编号": "RESO-8003"}}).json()
    assert missing["ok"] is False
    assert "缺少必填字段" in missing["message"]
