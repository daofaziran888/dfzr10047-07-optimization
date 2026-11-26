"""解析批次领域服务：汇聚每次解析的行级统计与闭合判定，产出批次记录数据集。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ParseBatchService(BaseModuleService):
    MODULE = "parse_batch"
    ENTITY = "批次记录"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = [
        "批次记录编号", "关联投递批次", "解析器标识", "总行数", "成功行数",
        "隔离行数", "批次状态", "完成时间", "是否正式批次", "实际使用格式",
        "判定版本", "回执编号", "实际读取行数",
    ]
    REQUIRED_FIELDS = ["批次记录编号", "关联投递批次", "解析器标识"]
    STATUS_ORDER = ["解析中", "已闭合", "存在隔离", "已作废"]
    ACTION_RULES = {"闭合批次": "已闭合", "登记隔离": "存在隔离", "作废批次": "已作废"}
    NEGATIVE_ACTIONS = ["作废批次"]

    @staticmethod
    def _int(row: dict[str, Any], field: str) -> int:
        text = str(row.get(field) or "").strip()
        return int(text) if text.isdigit() else 0

    def _find_by_intake(self, intake_batch: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if row.get("关联投递批次") == intake_batch:
                return row
        return None

    def _is_delivered(self, batch_no: str) -> bool:
        """批次记录编号是否已挂到已送达的投递记录上（经输出契约关联）。"""
        contract_nos = [
            str(r.get("契约编号")) for r in store.rows("output_contract")
            if r.get("关联批次") == batch_no
        ]
        if not contract_nos:
            return False
        return any(
            r.get("status") == "已送达" and str(r.get("关联契约")) in contract_nos
            for r in store.rows("result_deliver")
        )

    def official_batch(self, intake_batch: str) -> dict[str, Any] | None:
        """返回批次当前唯一有效的正式批次记录（已作废与已交付旧批不算在途正式批）。"""
        for row in store.rows(self.MODULE):
            if row.get("关联投递批次") != intake_batch:
                continue
            if row.get("是否正式批次") != "是":
                continue
            if row.get("批次状态") == "已作废":
                continue
            if self._is_delivered(str(row.get("批次记录编号"))):
                continue
            return row
        return None

    def register_official_batch(
        self, intake_batch: str, parser_id: str, resolved_format: str,
        judgement_version: str, receipt_no: str, create_time: str,
    ) -> tuple[bool, str, dict[str, Any]]:
        """人工改派后登记唯一正式解析批次。

        CSV 与 XLSX 派发并发时只认一个正式批次：同格式重复确认幂等复用，
        不同格式的在途旧批一律作废；已经交给目标的旧批保持原样、不再改动。
        """
        rows = store.rows(self.MODULE)
        # 先作废所有仍在途的异格式正式批（含迟到派发），已交付旧批保持原样
        for row in rows:
            if row.get("关联投递批次") != intake_batch:
                continue
            if row.get("是否正式批次") != "是" or row.get("批次状态") == "已作废":
                continue
            if str(row.get("实际使用格式") or "") == resolved_format:
                continue
            if self._is_delivered(str(row.get("批次记录编号"))):
                # 已交付旧批保持原样，新正式批另行登记
                continue
            row["批次状态"] = "已作废"
            row["status"] = "已作废"
            row["pending"] = False
            row["abnormal"] = True
            row["作废原因"] = (
                f"格式人工改判为 {resolved_format}（{judgement_version}），"
                f"旧 {row.get('实际使用格式')} 在途批次失去正式资格"
            )

        # 同格式正式批已存在：幂等复用，重复确认不另排作业
        for row in rows:
            if row.get("关联投递批次") != intake_batch:
                continue
            if row.get("是否正式批次") != "是" or row.get("批次状态") == "已作废":
                continue
            if str(row.get("实际使用格式") or "") == resolved_format:
                return True, (
                    f"批次 {intake_batch} 已存在唯一正式 {resolved_format} 批次"
                    f"{row.get('批次记录编号')}，重复确认不另排作业"
                ), dict(row)

        # 同一批次不同格式各自留一条台账，编号带自增序号防止 CSV/XLSX 记录互相覆盖
        existing_numbers = {str(r.get("批次记录编号")) for r in rows}
        seq = 1
        batch_no = f"PB-OFF-{intake_batch}-{resolved_format}-{seq}"
        while batch_no in existing_numbers:
            seq += 1
            batch_no = f"PB-OFF-{intake_batch}-{resolved_format}-{seq}"
        entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": entry_id,
            "status": "解析中",
            "pending": True,
            "abnormal": False,
            "批次记录编号": batch_no,
            "关联投递批次": intake_batch,
            "解析器标识": parser_id,
            "总行数": "0",
            "成功行数": "0",
            "隔离行数": "0",
            "批次状态": "解析中",
            "完成时间": "",
            "是否正式批次": "是",
            "实际使用格式": resolved_format,
            "判定版本": judgement_version,
            "回执编号": receipt_no,
            "实际读取行数": "0",
            "改派时间": create_time,
        }
        rows.append(entry)
        return True, (
            f"批次 {intake_batch} 已登记唯一正式 {resolved_format} 批次 {batch_no}"
        ), dict(entry)

    def tally_from_field_parse(self, intake_batch: str) -> dict[str, int]:
        """从字段解析明细聚合行级统计：成功行数与部分失败行数。

        旧尝试留档字段（是否正式结果=否）不允许计入正式批次，
        因此迟到的 CSV 字段不会凭空增加成功行。
        """
        parsed = store.rows("field_parse")
        rows_of_batch = [
            r for r in parsed
            if r.get("关联批次") == intake_batch
            and str(r.get("是否正式结果") or "是") == "是"
        ]
        row_numbers: dict[str, bool] = {}
        for r in rows_of_batch:
            line = str(r.get("解析行号") or "")
            if not line:
                continue
            ok = r.get("status") == "解析成功"
            row_numbers[line] = row_numbers.get(line, True) and ok
        success = sum(1 for ok in row_numbers.values() if ok)
        failed = sum(1 for ok in row_numbers.values() if not ok)
        return {"total_rows": len(row_numbers), "success_rows": success, "failed_rows": failed}

    def tally_from_quarantine(self, intake_batch: str) -> dict[str, int]:
        """从坏行隔离明细统计真实隔离行数与已回补行数。"""
        rows = [
            r for r in store.rows("row_quarantine")
            if r.get("关联批次") == intake_batch
        ]
        return {
            "quarantined": len(rows),
            "reinstated": sum(1 for r in rows if r.get("status") == "已回补"),
            "discarded": sum(1 for r in rows if r.get("status") == "已废弃"),
            "unreviewed": sum(1 for r in rows if r.get("status") in ("已隔离", "复核中")),
        }

    def refresh_batch_counts(self, intake_batch: str) -> tuple[bool, str, dict[str, Any] | None]:
        """按字段解析与坏行隔离的真实计数刷新批次行数，不允许人工填数。"""
        batch = self._find_by_intake(intake_batch)
        if batch is None:
            return False, f"投递批次 {intake_batch} 没有对应批次记录", None
        if batch.get("status") == "已作废":
            return False, f"批次记录 {batch.get('批次记录编号')} 已作废，不再刷新计数", None

        parse_tally = self.tally_from_field_parse(intake_batch)
        quarantine_tally = self.tally_from_quarantine(intake_batch)
        quarantined = quarantine_tally["quarantined"] - quarantine_tally["reinstated"]
        success = parse_tally["success_rows"] + quarantine_tally["reinstated"]

        batch["总行数"] = str(parse_tally["total_rows"])
        batch["成功行数"] = str(success)
        batch["隔离行数"] = str(max(0, quarantined))
        return True, (
            f"批次 {intake_batch} 计数已按真实明细刷新："
            f"总 {parse_tally['total_rows']} / 成功 {success} / 隔离 {max(0, quarantined)}"
        ), dict(batch)

    def close_batch(self, intake_batch: str, close_time: str) -> tuple[bool, str, dict[str, Any] | None]:
        """闭合批次：存在未复核隔离行时不得闭合，计数必须先与明细一致。"""
        batch = self._find_by_intake(intake_batch)
        if batch is None:
            return False, f"投递批次 {intake_batch} 没有对应批次记录", None
        if batch.get("status") == "已闭合":
            return False, f"批次 {intake_batch} 已闭合，不能重复闭合", None
        if batch.get("status") == "已作废":
            return False, f"批次 {intake_batch} 已作废，不能闭合", None

        quarantine_tally = self.tally_from_quarantine(intake_batch)
        if quarantine_tally["unreviewed"] > 0:
            batch["批次状态"] = "存在隔离"
            batch["status"] = "存在隔离"
            batch["pending"] = True
            batch["abnormal"] = True
            return False, (
                f"批次 {intake_batch} 仍有 {quarantine_tally['unreviewed']} 行隔离未复核，"
                f"不得闭合"
            ), dict(batch)

        ok, _msg, _row = self.refresh_batch_counts(intake_batch)
        total = self._int(batch, "总行数")
        success = self._int(batch, "成功行数")
        if total > 0 and success == 0:
            batch["批次状态"] = "存在隔离"
            batch["status"] = "存在隔离"
            batch["pending"] = True
            batch["abnormal"] = True
            return False, f"批次 {intake_batch} 成功行数为 0，不得闭合", dict(batch)

        batch["批次状态"] = "已闭合"
        batch["status"] = "已闭合"
        batch["完成时间"] = close_time
        batch["pending"] = False
        batch["abnormal"] = False
        return True, f"批次 {intake_batch} 已闭合，成功 {success}/{total} 行", dict(batch)

    def invalidate_batch(self, intake_batch: str, reason: str) -> tuple[bool, str]:
        """作废批次：格式无法识别或路由失败时使用，作废后不得再进入契约。"""
        batch = self._find_by_intake(intake_batch)
        if batch is None:
            return False, f"投递批次 {intake_batch} 没有对应批次记录"
        if batch.get("status") == "已作废":
            return False, f"批次 {intake_batch} 已作废"
        batch["批次状态"] = "已作废"
        batch["status"] = "已作废"
        batch["pending"] = False
        batch["abnormal"] = True
        batch["作废原因"] = reason
        return True, f"批次 {intake_batch} 已作废：{reason}"

    def open_batches(self) -> list[dict[str, Any]]:
        """列出全部未闭合批次，供水位推进前置校验消费。"""
        return [
            {
                "批次记录编号": r.get("批次记录编号"),
                "关联投递批次": r.get("关联投递批次"),
                "批次状态": r.get("批次状态"),
                "隔离行数": r.get("隔离行数"),
            }
            for r in store.rows(self.MODULE)
            if r.get("批次状态") not in ("已闭合", "已作废", "闭合")
        ]

    def closed_ratio(self) -> float:
        """批次闭合率：已闭合 / 非作废总数。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("批次状态") != "已作废"]
        if not rows:
            return 0.0
        closed = sum(1 for r in rows if r.get("批次状态") in ("已闭合", "闭合"))
        return closed / len(rows)
