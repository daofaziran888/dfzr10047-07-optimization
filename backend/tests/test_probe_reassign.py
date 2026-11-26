"""人工格式改判与探测结论改派的端到端场景测试。

复现值班场景：旧 CSV 尝试读完样本并提交差异比对但未拿到字段时，另一窗口确认
正确格式为 XLSX；旧 CSV 字段随后迟到。断言在途作业状态一致、回执可回查、
唯一正式批次、重复确认幂等、旧字段不凭空增加成功行。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routers.probe_flow import (
    CompareOpenPayload,
    CompareSubmitPayload,
    ConfirmReassignPayload,
    AttemptFieldsPayload,
    FormalFieldsPayload,
    confirm_and_reassign,
    deliver_attempt_fields,
    formal_submit,
    get_receipt,
    inflight_snapshot,
    open_compare,
    recovery_view,
    submit_compare,
)
from app.services import SERVICES
from app.store import store


BATCH = "FI-RT-001"
PROBE_NO = "FP-RT-001"
RECEIPT_NO = f"RR-{PROBE_NO}"


@pytest.fixture()
def fresh_store():
    store.reload_seed()
    yield store
    store.reload_seed()


def _seed_csv_verdict_probe() -> None:
    """造一条魔数为 XLSX、但服务端初判 CSV 的存疑探测结论（改判前状态）。"""
    store.rows("file_intake").append({
        "id": 910, "status": "待探测", "pending": True, "abnormal": False,
        "批次编号": BATCH, "来源渠道": "web-upload",
        "原始文件名": "mixed.bin", "字节大小": "2048",
        "投递时间": "2026-09-10T08:00:00", "文件后缀": ".csv",
        "声明格式": "csv", "接收节点": "node-01",
    })
    store.rows("format_probe").append({
        "id": 910, "status": "存疑", "pending": True, "abnormal": True,
        "探测编号": PROBE_NO, "关联批次": BATCH,
        "魔数特征": "504B0304", "推断格式": "csv-utf8",
        "原推断格式": "csv-utf8", "置信度": "0.72",
        "结构层级": "1", "是否压缩": "是", "探测耗时": "66",
        "声明格式": "csv", "判定版本": "AUTO-1", "改派回执编号": "",
    })
    # 改判前已存在的在途 CSV 路由决策（改派后应失效但保留可回查）
    store.rows("parser_route").append({
        "id": 910, "status": "已选定", "pending": False, "abnormal": False,
        "决策编号": "PR-OLD-CSV", "关联批次": BATCH,
        "推断格式": "csv-utf8", "实际使用格式": "csv-utf8",
        "命中解析器": "csv_parser_v2", "备选解析器": "csv_parser_v1",
        "路由依据": "初判 CSV 自动路由", "回退次数": "0",
        "决策时间": "2026-09-10T08:04:00",
        "判定版本": "AUTO-1", "是否有效": "是", "回执编号": "",
    })


class TestReassignScenario:
    def test_full_csv_to_xlsx_reassign_with_late_old_fields(self, fresh_store):
        _seed_csv_verdict_probe()
        diag_svc = SERVICES["probe_diagnosis"]
        probe_svc = SERVICES["format_probe"]
        batch_svc = SERVICES["parse_batch"]
        field_svc = SERVICES["field_parse"]

        # 1) 值班员从旧 CSV 尝试打开差异比对，并提交比对（字段尚未回来）
        ok, msg, old_diag = diag_svc.open_compare_request(
            "PD-OLD-CSV", PROBE_NO, BATCH, "csv-utf8",
            sample_rows=[{"a": "1"}, {"a": "2"}], create_time="2026-09-10T08:05:00")
        assert ok is True
        assert old_diag["诊断状态"] == "在途"
        ok, _msg, old_diag = diag_svc.submit_compare(
            "PD-OLD-CSV", ["rec_id", "data_value"], "2026-09-10T08:06:00")
        assert ok is True
        assert old_diag["status"] == "诊断中"

        # 2) 另一窗口确认正确格式为 XLSX → 拿到改派回执
        ok, msg, receipt = probe_svc.confirm_format(
            PROBE_NO, "xlsx", "2026-09-10T08:10:00", "reviewer-09")
        assert ok is True
        assert receipt["回执编号"] == RECEIPT_NO
        assert receipt["确认格式"] == "xlsx"
        assert receipt["原推断格式"] == "csv-utf8"
        # 人工 XLSX 确认可查到原先的魔数字节、CSV 判定和时间
        assert receipt["魔数字节"] == "504B0304"
        assert receipt["确认时间"] == "2026-09-10T08:10:00"

        # 旧比对请求立刻被标为待恢复，并给出可执行恢复动作
        old_diag = diag_svc._find("PD-OLD-CSV")
        assert old_diag["status"] == "待恢复"
        assert "改派回执" in old_diag["恢复动作"]

        # 3) 改判界面仍可从旧尝试打开差异比对请求（带旧尝试标记）
        ok, _msg, reopened = diag_svc.open_compare_request(
            "PD-OLD-CSV-2", PROBE_NO, BATCH, "csv-utf8",
            sample_rows=[{"a": "9"}], create_time="2026-09-10T08:11:00")
        assert ok is True
        assert reopened["诊断状态"] == "旧尝试"
        assert reopened["status"] == "旧尝试留档"

        # 4) 旧 CSV 字段迟到：只能留档，明确标为旧尝试
        ok, _msg, old_diag = diag_svc.deliver_attempt_fields(
            "PD-OLD-CSV", [{"字段名称": "rec_id", "解析取值": "R-1", "解析行号": "2"}])
        assert ok is True
        assert old_diag["status"] == "旧尝试留档"
        assert old_diag["字段结果"][0]["尝试标记"] == "旧尝试"
        assert old_diag["字段结果"][0]["字段来源"] == "旧尝试:csv-utf8"

        # 同样字段进字段解析台账时是非正式结果
        legacy = field_svc.record_old_attempt_field(
            "FP-LEGACY-1", BATCH, "ET-001", "csv-utf8",
            "rec_id", "R-1", "R-1", 2, "PD-OLD-CSV")
        assert legacy["是否正式结果"] == "否"

        # 5) 唯一正式 XLSX 批次已登记，旧 CSV 字段不增加成功行
        official = batch_svc.official_batch(BATCH)
        assert official is not None
        assert official["实际使用格式"] == "xlsx"
        assert official["回执编号"] == RECEIPT_NO
        tally = batch_svc.tally_from_field_parse(BATCH)
        assert tally["success_rows"] == 0  # 旧尝试字段被排除

        # 6) 重新登录/再次进入：回查得到旧字段（明确旧尝试）与恢复说明
        view = diag_svc.recovery_view("PD-OLD-CSV")
        assert view["可恢复"] is True
        assert view["恢复方式"] == "取回旧尝试字段"
        assert view["字段结果"][0]["尝试标记"] == "旧尝试"

        # 字段解析详情页再次进入时能打开同一份改派回执
        again = diag_svc.get_receipt(receipt_no=RECEIPT_NO)
        assert again["回执编号"] == RECEIPT_NO
        assert again["正式批次记录编号"] == official["批次记录编号"]

    def test_recovery_action_when_old_fields_never_arrive(self, fresh_store):
        _seed_csv_verdict_probe()
        diag_svc = SERVICES["probe_diagnosis"]
        diag_svc.open_compare_request(
            "PD-NG", PROBE_NO, BATCH, "csv-utf8", [], "2026-09-10T08:05:00")
        diag_svc.submit_compare("PD-NG", ["rec_id"], "2026-09-10T08:06:00")
        SERVICES["format_probe"].confirm_format(
            PROBE_NO, "xlsx", "2026-09-10T08:10:00")
        # 字段始终没回来 → 工作台看到的是可执行恢复动作而非静默失败
        view = diag_svc.recovery_view("PD-NG")
        assert view["可恢复"] is False
        assert "重新发起" in view["恢复动作"]
        assert view["字段结果"] == []
        # 按恢复动作从 XLSX 重新发起的诊断是在途尝试，字段可正常取回
        ok, _msg, new_diag = diag_svc.open_compare_request(
            "PD-NEW-XLSX", PROBE_NO, BATCH, "xlsx", [], "2026-09-10T08:12:00")
        assert ok is True
        assert new_diag["诊断状态"] == "在途"
        diag_svc.deliver_attempt_fields(
            "PD-NEW-XLSX", [{"字段名称": "sheet_name", "解析取值": "Sheet1"}])
        assert diag_svc._find("PD-NEW-XLSX")["status"] == "字段可取"

    def test_duplicate_confirmation_is_idempotent_no_new_job(self, fresh_store):
        _seed_csv_verdict_probe()
        probe_svc = SERVICES["format_probe"]
        route_svc = SERVICES["parser_route"]
        batch_svc = SERVICES["parse_batch"]

        ok1, _m1, receipt1 = probe_svc.confirm_format(
            PROBE_NO, "xlsx", "2026-09-10T08:10:00", "reviewer-09")
        ok2, _m2, receipt2 = probe_svc.confirm_format(
            PROBE_NO, "xlsx", "2026-09-10T08:15:00", "reviewer-10")
        assert ok1 and ok2
        assert receipt1["回执编号"] == receipt2["回执编号"]
        assert receipt2["重复确认次数"] == "2"

        # 路由只有一条有效 XLSX 决策，重复确认不另排作业
        xlsx_routes = [
            r for r in store.rows("parser_route")
            if r.get("关联批次") == BATCH and r.get("是否有效") == "是"
        ]
        assert len(xlsx_routes) == 1
        assert xlsx_routes[0]["实际使用格式"] == "xlsx"
        assert xlsx_routes[0]["判定版本"].startswith("MANUAL-")
        # 批次台账只有一个正式批次
        officials = [
            r for r in store.rows("parse_batch")
            if r.get("关联投递批次") == BATCH
            and r.get("是否正式批次") == "是" and r.get("批次状态") != "已作废"
        ]
        assert len(officials) == 1
        # 服务级幂等：路由与批次重复调用同样不新增
        ok_r, _m, d1 = route_svc.reassign_route(
            BATCH, "xlsx", receipt1["判定版本"], RECEIPT_NO, "t2")
        ok_b, _m2, b1 = batch_svc.register_official_batch(
            BATCH, "xlsx_parser_v3", "xlsx", receipt1["判定版本"], RECEIPT_NO, "t2")
        assert ok_r and ok_b
        assert d1["决策编号"] == xlsx_routes[0]["决策编号"]
        assert b1["批次记录编号"] == officials[0]["批次记录编号"]

    def test_confirmed_probe_cannot_flip_to_another_format(self, fresh_store):
        _seed_csv_verdict_probe()
        probe_svc = SERVICES["format_probe"]
        probe_svc.confirm_format(PROBE_NO, "xlsx", "2026-09-10T08:10:00")
        ok, msg, _r = probe_svc.confirm_format(
            PROBE_NO, "jsonl", "2026-09-10T08:20:00")
        assert ok is False
        assert "不得再改判" in msg

    def test_generic_action_entry_blocks_confirm_format(self, fresh_store):
        _seed_csv_verdict_probe()
        probe_id = next(
            r["id"] for r in store.rows("format_probe") if r["探测编号"] == PROBE_NO)
        entry, msg = SERVICES["format_probe"].run_action(probe_id, "确认格式")
        assert entry is None
        assert "改派回执" in msg


class TestFormalEntry:
    def test_formal_xlsx_entry_accepted_and_csv_entry_rejected(self, fresh_store):
        _seed_csv_verdict_probe()
        probe_svc = SERVICES["format_probe"]
        field_svc = SERVICES["field_parse"]
        _ok, _msg, receipt = probe_svc.confirm_format(
            PROBE_NO, "xlsx", "2026-09-10T08:10:00")

        rows = [
            {"解析编号": "FML-1", "字段名称": "sheet_name", "解析取值": "Sheet1",
             "解析行号": 1, "success": True},
            {"解析编号": "FML-2", "字段名称": "row_count", "解析取值": "120",
             "解析行号": 2, "success": True},
        ]
        # 正式 XLSX 入口与诊断操作分开：凭回执提交
        ok, msg, created = field_svc.submit_formal_fields(
            BATCH, RECEIPT_NO, "xlsx", "ET-005", rows)
        assert ok is True
        assert len(created) == 2
        assert all(c["回执编号"] == RECEIPT_NO for c in created)

        # 结论改判后从旧 CSV 入口提交正式结果必须被拒绝
        ok, msg, _c = field_svc.submit_formal_fields(
            BATCH, RECEIPT_NO, "csv-utf8", "ET-001", rows)
        assert ok is False
        assert "拒绝从 csv-utf8 入口" in msg

        # 重复提交幂等：不凭空增加成功行
        ok, _msg, created2 = field_svc.submit_formal_fields(
            BATCH, RECEIPT_NO, "xlsx", "ET-005", rows)
        assert ok is True
        assert created2 == []
        official = SERVICES["parse_batch"].official_batch(BATCH)
        assert official["成功行数"] == "2"
        assert official["实际读取行数"] == "2"

        # 旧 CSV 迟到字段仍不能增加正式成功行
        SERVICES["field_parse"].record_old_attempt_field(
            "FP-LEGACY-X", BATCH, "ET-001", "csv-utf8",
            "rec_id", "R-9", "R-9", 99, "PD-OLD-CSV")
        tally = SERVICES["parse_batch"].tally_from_field_parse(BATCH)
        assert tally["success_rows"] == 2

    def test_formal_submit_requires_receipt(self, fresh_store):
        _seed_csv_verdict_probe()
        ok, msg, _c = SERVICES["field_parse"].submit_formal_fields(
            BATCH, "RR-NOPE", "xlsx", "ET-005", [{"字段名称": "x"}])
        assert ok is False
        assert "改派回执 RR-NOPE 不存在" in msg


class TestUniqueOfficialBatch:
    def test_csv_xlsx_concurrency_keeps_single_official(self, fresh_store):
        _seed_csv_verdict_probe()
        batch_svc = SERVICES["parse_batch"]
        # 模拟并发：CSV 先抢到正式批次，XLSX 确认随后到达
        ok1, _m1, csv_batch = batch_svc.register_official_batch(
            BATCH, "csv_parser_v2", "csv-utf8", "AUTO-1", "", "2026-09-10T08:05:00")
        ok2, _m2, xlsx_batch = batch_svc.register_official_batch(
            BATCH, "xlsx_parser_v3", "xlsx", "MANUAL-910-2", RECEIPT_NO,
            "2026-09-10T08:10:00")
        assert ok1 and ok2
        # 返回的是登记时快照；台账里同编号记录已被改派作废
        stored_csv = next(r for r in store.rows("parse_batch")
                          if r["批次记录编号"] == csv_batch["批次记录编号"])
        assert stored_csv["批次状态"] == "已作废"
        assert "格式人工改判" in stored_csv["作废原因"]
        officials = [
            r for r in store.rows("parse_batch")
            if r.get("关联投递批次") == BATCH
            and r.get("是否正式批次") == "是" and r.get("批次状态") != "已作废"
        ]
        assert len(officials) == 1
        assert officials[0]["批次记录编号"] == xlsx_batch["批次记录编号"]
        # 作废 CSV 批与正式 XLSX 批编号不同，台账各留各的、不互相覆盖
        assert csv_batch["批次记录编号"] != xlsx_batch["批次记录编号"]
        assert batch_svc.official_batch(BATCH)["实际使用格式"] == "xlsx"

        # 又一个迟到的 CSV 派发直接落库，再次确认时仍只认 XLSX 正式批
        store.rows("parse_batch").append({
            "id": 955, "status": "解析中", "pending": True, "abnormal": False,
            "批次记录编号": "PB-LATE-CSV", "关联投递批次": BATCH,
            "解析器标识": "csv_parser_v2", "总行数": "0", "成功行数": "0",
            "隔离行数": "0", "批次状态": "解析中", "完成时间": "",
            "是否正式批次": "是", "实际使用格式": "csv-utf8",
            "判定版本": "AUTO-1", "回执编号": "", "实际读取行数": "0",
        })
        ok3, _m3, still_xlsx = batch_svc.register_official_batch(
            BATCH, "xlsx_parser_v3", "xlsx", "MANUAL-910-2", RECEIPT_NO,
            "2026-09-10T08:20:00")
        assert ok3 is True
        assert still_xlsx["批次记录编号"] == xlsx_batch["批次记录编号"]
        late = next(r for r in store.rows("parse_batch")
                    if r["批次记录编号"] == "PB-LATE-CSV")
        assert late["批次状态"] == "已作废"

    def test_delivered_old_batch_remains_untouched(self, fresh_store):
        """已经交给目标的旧批（RD-001 已送达）在改判后保持原样。"""
        batch_svc = SERVICES["parse_batch"]
        probe_svc = SERVICES["format_probe"]
        # seed: FI-20260901001 → PB-001 → OC-001 → RD-001 已送达
        ok, _msg, receipt = probe_svc.confirm_format(
            "FP-001", "xlsx", "2026-09-05T00:00:00", "reviewer-01")
        assert ok is True
        old = next(r for r in store.rows("parse_batch")
                   if r["批次记录编号"] == "PB-001")
        assert old["批次状态"] in ("闭合", "已闭合")
        rd1 = next(r for r in store.rows("result_deliver")
                   if r["投递记录编号"] == "RD-001")
        assert rd1["status"] == "已送达"
        # 新正式批另行登记，不动旧批
        official = batch_svc.official_batch("FI-20260901001")
        assert official["批次记录编号"] != "PB-001"
        assert official["实际使用格式"] == "xlsx"


class TestRouterWorkflow:
    def test_workflow_endpoints_end_to_end(self, fresh_store):
        _seed_csv_verdict_probe()
        probe_id = next(
            r["id"] for r in store.rows("format_probe") if r["探测编号"] == PROBE_NO)

        # 改判前先核对服务端识别到的在途解析
        snap = inflight_snapshot(BATCH)
        assert snap["resolved_format"] == "csv-utf8"
        assert snap["receipt"] is None

        # 旧 CSV 尝试打开差异比对并提交
        r1 = open_compare(CompareOpenPayload(
            probe_no=PROBE_NO, intake_batch=BATCH, attempt_format="csv-utf8",
            sample_rows=[{"a": "1"}], create_time="2026-09-10T08:05:00"))
        assert r1.ok is True
        diag_id = r1.entry["id"]
        r2 = submit_compare(diag_id, CompareSubmitPayload(
            compare_fields=["rec_id"], submit_time="2026-09-10T08:06:00"))
        assert r2.ok is True

        # 人工确认并改派
        r3 = confirm_and_reassign(probe_id, ConfirmReassignPayload(
            confirmed_format="xlsx", confirm_time="2026-09-10T08:10:00",
            operator="reviewer-09"))
        assert r3.ok is True
        assert r3.entry["回执编号"] == RECEIPT_NO

        # 旧 CSV 字段迟到
        r4 = deliver_attempt_fields(diag_id, AttemptFieldsPayload(fields=[
            {"字段名称": "rec_id", "解析取值": "R-1", "解析行号": "2"}]))
        assert r4.ok is True
        assert r4.entry["字段结果"][0]["尝试标记"] == "旧尝试"

        # 重新登录后的恢复视图
        view = recovery_view(diag_id)
        assert view["可恢复"] is True

        # 字段解析详情页回查同一份回执
        receipt = get_receipt(RECEIPT_NO)
        assert receipt["确认格式"] == "xlsx"

        # 正式 XLSX 解析入口
        r5 = formal_submit(FormalFieldsPayload(
            receipt_no=RECEIPT_NO, confirmed_format="xlsx", template_no="ET-005",
            rows=[{"解析编号": "FML-R1", "字段名称": "sheet_name",
                   "解析取值": "Sheet1", "解析行号": 1}]))
        assert r5.ok is True

        # 再看在途快照：唯一正式 XLSX 作业 + 回执齐全
        snap2 = inflight_snapshot(BATCH)
        assert snap2["resolved_format"] == "xlsx"
        assert snap2["official_batch"]["实际使用格式"] == "xlsx"
        assert snap2["receipt"]["回执编号"] == RECEIPT_NO
        assert len(snap2["attempts"]) == 1
        assert snap2["attempts"][0]["诊断状态"].startswith("旧尝试")

    def test_unknown_receipt_returns_404(self, fresh_store):
        with pytest.raises(HTTPException) as exc:
            get_receipt("RR-MISSING")
        assert exc.value.status_code == 404


class TestAuditAndLedger:
    def test_audit_keeps_magic_csv_verdict_and_time(self, fresh_store):
        _seed_csv_verdict_probe()
        SERVICES["format_probe"].confirm_format(
            PROBE_NO, "xlsx", "2026-09-10T08:10:00", "reviewer-09")
        audit = next(
            r for r in store.rows("intake_audit")
            if r.get("审计编号") == f"IA-{RECEIPT_NO}")
        assert "504B0304" in audit["变更前值"]
        assert "csv-utf8" in audit["变更前值"]
        assert "xlsx" in audit["变更后值"]
        assert audit["操作时间"] == "2026-09-10T08:10:00"
        # 探测结论自身保留原魔数字节与原判定
        probe = next(r for r in store.rows("format_probe")
                     if r["探测编号"] == PROBE_NO)
        assert probe["魔数特征"] == "504B0304"
        assert probe["原推断格式"] == "csv-utf8"
        assert probe["判定版本"] == "MANUAL-910-2"
        assert probe["改派回执编号"] == RECEIPT_NO

    def test_route_and_batch_ledger_fields_traceable(self, fresh_store):
        _seed_csv_verdict_probe()
        SERVICES["format_probe"].confirm_format(
            PROBE_NO, "xlsx", "2026-09-10T08:10:00")
        decision = SERVICES["parser_route"].effective_decision(BATCH)
        assert decision["实际使用格式"] == "xlsx"
        assert decision["判定版本"].startswith("MANUAL-")
        assert decision["回执编号"] == RECEIPT_NO
        # 失效的旧 CSV 路由仍可回查
        invalid = [r for r in store.rows("parser_route")
                   if r.get("关联批次") == BATCH and r.get("是否有效") == "否"]
        assert any("失效" in r.get("失效原因", "") for r in invalid)

        official = SERVICES["parse_batch"].official_batch(BATCH)
        assert official["判定版本"] == decision["判定版本"]
        # 字段来源与实际读取行数随正式字段落台账，可回查
        SERVICES["field_parse"].submit_formal_fields(
            BATCH, RECEIPT_NO, "xlsx", "ET-005",
            [{"解析编号": "FML-T1", "字段名称": "sheet_name",
              "解析取值": "Sheet1", "解析行号": 1}])
        fp = next(r for r in store.rows("field_parse")
                  if r["解析编号"] == "FML-T1")
        assert fp["字段来源"] == "正式批次:xlsx"
        assert fp["回执编号"] == RECEIPT_NO
        assert fp["实际读取行数"] == "1"
