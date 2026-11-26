"""字段解析领域服务：按命中模板逐字段抽取取值并记录解析质量，产出解析字段数据集。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class FieldParseService(BaseModuleService):
    MODULE = "field_parse"
    ENTITY = "解析字段"
    OPERATION_TERM = "解析"
    LIST_FIELDS = [
        "解析编号", "关联批次", "命中模板", "字段名称", "原始取值", "解析取值",
        "解析耗时", "解析行号", "是否正式结果", "字段来源", "尝试格式",
        "回执编号", "实际读取行数",
    ]
    REQUIRED_FIELDS = ["解析编号", "关联批次", "命中模板"]
    STATUS_ORDER = ["待解析", "解析中", "解析成功", "部分失败"]
    ACTION_RULES = {"启动解析": "解析中", "确认成功": "解析成功", "登记部分失败": "部分失败"}
    NEGATIVE_ACTIONS = ["登记部分失败"]

    @staticmethod
    def _is_official(row: dict[str, Any]) -> bool:
        """只把改派后正式批次的字段计入台账；旧尝试字段默认非正式且不参与统计。"""
        return str(row.get("是否正式结果") or "是") == "是"

    def record_parsed_field(self, parse_no: str, intake_batch: str, template_no: str,
                            field_name: str, raw_value: str, parsed_value: str,
                            elapsed_ms: int, row_num: int,
                            success: bool) -> dict[str, Any]:
        """登记单字段解析结果，解析失败的字段连同行号可供坏行隔离消费。"""
        rows = store.rows(self.MODULE)
        entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": entry_id,
            "status": "解析成功" if success else "部分失败",
            "pending": not success,
            "abnormal": not success,
            "解析编号": parse_no,
            "关联批次": intake_batch,
            "命中模板": template_no,
            "字段名称": field_name,
            "原始取值": raw_value,
            "解析取值": parsed_value,
            "解析耗时": str(elapsed_ms),
            "解析行号": str(row_num),
            "是否正式结果": "是",
            "字段来源": "",
            "尝试格式": "",
            "回执编号": "",
            "实际读取行数": "",
        }
        rows.append(entry)
        return entry

    def record_old_attempt_field(self, parse_no: str, intake_batch: str,
                                 template_no: str, attempt_format: str,
                                 field_name: str, raw_value: str, parsed_value: str,
                                 row_num: int, diagnosis_no: str) -> dict[str, Any]:
        """登记旧尝试迟到的字段：明确标记旧尝试，永远不进入正式批次统计。"""
        rows = store.rows(self.MODULE)
        entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": entry_id,
            "status": "解析成功",
            "pending": False,
            "abnormal": False,
            "解析编号": parse_no,
            "关联批次": intake_batch,
            "命中模板": template_no,
            "字段名称": field_name,
            "原始取值": raw_value,
            "解析取值": parsed_value,
            "解析耗时": "0",
            "解析行号": str(row_num),
            "是否正式结果": "否",
            "字段来源": f"旧尝试:{attempt_format}",
            "尝试格式": attempt_format,
            "回执编号": "",
            "实际读取行数": "",
            "关联诊断": diagnosis_no,
        }
        rows.append(entry)
        return entry

    def submit_formal_fields(
        self, intake_batch: str, receipt_no: str, confirmed_format: str,
        template_no: str, rows_in: list[dict[str, Any]],
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        """XLSX 正式解析入口：只接受改派回执指向格式的字段，与诊断操作分开处理。

        旧 CSV 入口（尝试格式不一致）提交会被拒绝，避免结论改变后仍从旧格式入口
        提交正式结果；同一解析编号重复提交幂等，不凭空增加成功行。
        """
        from app.services import SERVICES

        receipt = SERVICES["probe_diagnosis"].get_receipt(receipt_no=receipt_no)
        if receipt is None:
            return False, f"改派回执 {receipt_no} 不存在，不得提交正式字段", []
        if str(receipt.get("关联批次")) != intake_batch:
            return False, (
                f"回执 {receipt_no} 属于批次 {receipt.get('关联批次')}，"
                f"与当前批次 {intake_batch} 不一致"
            ), []
        if str(receipt.get("确认格式")) != confirmed_format:
            return False, (
                f"回执 {receipt_no} 指向格式 {receipt.get('确认格式')}，"
                f"拒绝从 {confirmed_format} 入口提交正式结果"
            ), []

        official = SERVICES["parse_batch"].official_batch(intake_batch)
        if official is None:
            return False, f"批次 {intake_batch} 没有有效的正式解析批次，先完成格式改派", []
        if str(official.get("实际使用格式") or "") != confirmed_format:
            return False, (
                f"正式批次 {official.get('批次记录编号')} 实际格式为"
                f"{official.get('实际使用格式')}，{confirmed_format} 入口不得提交"
            ), []

        existing_nos = {str(r.get("解析编号")) for r in store.rows(self.MODULE)}
        created: list[dict[str, Any]] = []
        read_rows: dict[str, bool] = {}
        for index, item in enumerate(rows_in, start=1):
            parse_no = str(item.get("解析编号") or
                           f"FML-{receipt_no}-{index}")
            row_num = int(item.get("解析行号") or index)
            read_rows[str(row_num)] = True
            if parse_no in existing_nos:
                # 重复/迟到提交：幂等跳过，不增加成功行
                continue
            entry = self.record_parsed_field(
                parse_no, intake_batch, template_no,
                str(item.get("字段名称") or ""),
                str(item.get("原始取值") or ""),
                str(item.get("解析取值") or ""),
                int(item.get("解析耗时") or 0), row_num,
                success=bool(item.get("success", True)),
            )
            entry["是否正式结果"] = "是"
            entry["字段来源"] = f"正式批次:{confirmed_format}"
            entry["尝试格式"] = confirmed_format
            entry["回执编号"] = receipt_no
            entry["实际读取行数"] = str(len(rows_in))
            created.append(entry)
            existing_nos.add(parse_no)

        official["实际读取行数"] = str(
            max(int(official.get("实际读取行数") or 0), len(read_rows)))
        SERVICES["parse_batch"].refresh_batch_counts(intake_batch)
        if created:
            return True, (
                f"批次 {intake_batch} 正式 {confirmed_format} 入口已写入 "
                f"{len(created)} 条字段，实际读取 {len(read_rows)} 行"
            ), created
        return True, (
            f"批次 {intake_batch} 本次提交均为重复字段，已幂等忽略，成功行未增加"
        ), []

    def failed_fields_of_batch(self, intake_batch: str) -> list[dict[str, Any]]:
        """输出解析失败字段清单（含行号），坏行隔离据此建隔离行。

        旧尝试留档字段不属于正式批次，不得触发隔离。
        """
        return [
            {
                "解析编号": r.get("解析编号"),
                "字段名称": r.get("字段名称"),
                "原始取值": r.get("原始取值"),
                "解析行号": r.get("解析行号"),
            }
            for r in store.rows(self.MODULE)
            if r.get("关联批次") == intake_batch
            and r.get("status") == "部分失败"
            and self._is_official(r)
        ]

    def parsed_rows_of_batch(self, intake_batch: str) -> dict[str, dict[str, Any]]:
        """按行号聚合解析成功的取值，供抽样复核与输出契约取数（仅正式结果）。"""
        grouped: dict[str, dict[str, Any]] = {}
        for r in store.rows(self.MODULE):
            if r.get("关联批次") != intake_batch or not self._is_official(r):
                continue
            line = str(r.get("解析行号") or "")
            if not line:
                continue
            bucket = grouped.setdefault(line, {})
            bucket[str(r.get("字段名称"))] = r.get("解析取值")
            bucket["__status"] = (
                "部分失败" if r.get("status") == "部分失败"
                else bucket.get("__status", "解析成功")
            )
        return grouped

    def success_rate(self, intake_batch: str) -> float:
        """批次字段级解析成功率，供解析指标交叉核对（旧尝试字段不参与）。"""
        rows = [r for r in store.rows(self.MODULE)
                if r.get("关联批次") == intake_batch and self._is_official(r)]
        if not rows:
            return 0.0
        return sum(1 for r in rows if r.get("status") == "解析成功") / len(rows)

    def template_hit_stats(self) -> dict[str, int]:
        """按命中模板统计解析字段数，供模板版本过渡期命中分布核对。"""
        stats: dict[str, int] = {}
        for r in store.rows(self.MODULE):
            key = str(r.get("命中模板") or "")
            if key:
                stats[key] = stats.get(key, 0) + 1
        return stats
