"""解析路由领域服务：按探测出的目标格式选择解析器并管理回退策略，产出路由决策数据集。"""
from __future__ import annotations

from typing import Any, Callable

from app.services.base import BaseModuleService
from app.store import store


class ParserRouteService(BaseModuleService):
    MODULE = "parser_route"
    ENTITY = "路由决策"
    OPERATION_TERM = "调度"
    LIST_FIELDS = [
        "决策编号", "关联批次", "推断格式", "命中解析器", "备选解析器", "路由依据",
        "回退次数", "决策时间", "实际使用格式", "判定版本", "是否有效", "回执编号",
    ]
    REQUIRED_FIELDS = ["决策编号", "关联批次", "推断格式"]
    STATUS_ORDER = ["待路由", "已选定", "已回退", "路由失败"]
    ACTION_RULES = {"选定解析器": "已选定", "执行回退": "已回退", "判定失败": "路由失败"}
    NEGATIVE_ACTIONS = ["判定失败"]

    # 每种格式允许的解析器优先级清单，首个为首选，其余按顺序回退
    PARSER_CHAINS: dict[str, list[str]] = {
        "csv-utf8": ["csv_parser_v2", "csv_parser_v1", "generic_delimiter"],
        "csv-utf16le": ["csv_parser_v2", "generic_delimiter"],
        "tsv": ["tsv_parser_v1", "generic_delimiter"],
        "xlsx": ["xlsx_parser_v3", "xlsx_parser_v2", "generic_xml_parser"],
        "jsonl": ["jsonl_parser_v1", "generic_jsonl"],
        "jsonl-crlf": ["jsonl_parser_v1", "generic_jsonl"],
        "xml": ["xml_parser_v1"],
    }

    def _find(self, decision_no: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if row.get("决策编号") == decision_no:
                return row
        return None

    def reassign_route(
        self, intake_batch: str, resolved_format: str, judgement_version: str,
        receipt_no: str, decide_time: str,
    ) -> tuple[bool, str, dict[str, Any]]:
        """人工改派后重写解析路由，实际使用格式与判定版本写入路由决策。

        同批次重复确认同一格式时幂等返回同一决策，不新增路由记录；
        旧格式在途决策标记为失效，保证 CSV/XLSX 并发时只有一条有效路由。
        """
        rows = store.rows(self.MODULE)
        # 先失效全部异格式在途决策，保证 CSV/XLSX 并发时只剩一条有效路由
        for row in rows:
            if row.get("关联批次") != intake_batch or row.get("是否有效") == "否":
                continue
            if str(row.get("实际使用格式") or row.get("推断格式")) == resolved_format \
                    and row.get("判定版本") == judgement_version:
                continue
            row["是否有效"] = "否"
            row["pending"] = False
            row["abnormal"] = True
            row["失效原因"] = (
                f"格式人工改判为 {resolved_format}（{judgement_version}），"
                f"旧 {row.get('推断格式')} 路由失效"
            )

        # 同格式同版本决策已存在：幂等返回，不新增路由记录
        for row in rows:
            if row.get("关联批次") != intake_batch or row.get("是否有效") == "否":
                continue
            if str(row.get("实际使用格式") or row.get("推断格式")) == resolved_format \
                    and row.get("判定版本") == judgement_version:
                return True, (
                    f"批次 {intake_batch} 的 {resolved_format} 路由决策已存在，"
                    "重复确认不另排作业"
                ), dict(row)

        chain = self.PARSER_CHAINS.get(resolved_format, [])
        decision_no = f"PR-RT-{intake_batch}"
        entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": entry_id,
            "status": "已选定" if chain else "路由失败",
            "pending": False,
            "abnormal": not chain,
            "决策编号": decision_no,
            "关联批次": intake_batch,
            "推断格式": resolved_format,
            "实际使用格式": resolved_format,
            "命中解析器": chain[0] if chain else "",
            "备选解析器": ",".join(chain[1:]),
            "路由依据": (
                f"人工格式改派（判定版本 {judgement_version}），"
                f"实际使用格式 {resolved_format}，回执 {receipt_no}"
            ),
            "回退次数": "0",
            "决策时间": decide_time,
            "判定版本": judgement_version,
            "是否有效": "是",
            "回执编号": receipt_no,
        }
        rows.append(entry)
        if not chain:
            return False, f"格式 {resolved_format} 无注册解析器链，改派路由失败", dict(entry)
        return True, (
            f"批次 {intake_batch} 已改派至 {resolved_format} 解析器 {chain[0]}"
        ), dict(entry)

    def effective_decision(self, intake_batch: str) -> dict[str, Any] | None:
        """返回批次当前唯一有效的路由决策（供字段解析入口核对格式）。"""
        candidates = [
            r for r in store.rows(self.MODULE)
            if r.get("关联批次") == intake_batch and r.get("是否有效") != "否"
        ]
        return dict(candidates[-1]) if candidates else None

    def choose_by_probe(self, decision_no: str, intake_batch: str,
                        probe_no: str, decide_time: str) -> tuple[bool, str, dict[str, Any] | None]:
        """按格式探测的真实格式选定首选解析器。

        探测结论为存疑或无法识别时不得选定解析器，批次转为路由失败。
        """
        probe = next(
            (r for r in store.rows("format_probe") if r.get("探测编号") == probe_no), None)
        if probe is None:
            return False, f"探测结论 {probe_no} 不存在，无法路由", None

        resolved_format = str(probe.get("推断格式") or "")
        confidence = str(probe.get("置信度") or "0")
        try:
            confidence_value = float(confidence)
        except ValueError:
            confidence_value = 0.0

        rows = store.rows(self.MODULE)
        entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        chain = self.PARSER_CHAINS.get(resolved_format, [])

        if probe.get("status") in ("存疑", "无法识别") or confidence_value < 0.5:
            entry = {
                "id": entry_id, "status": "路由失败", "pending": False, "abnormal": True,
                "决策编号": decision_no, "关联批次": intake_batch,
                "推断格式": resolved_format, "命中解析器": "",
                "备选解析器": ",".join(chain),
                "路由依据": (
                    f"探测状态 {probe.get('status')}、置信度 {confidence}，"
                    f"不足以选定解析器"
                ),
                "回退次数": str(len(chain)), "决策时间": decide_time,
            }
            rows.append(entry)
            return False, (
                f"批次 {intake_batch} 探测置信度 {confidence} 不足，路由失败"
            ), dict(entry)

        if not chain:
            entry = {
                "id": entry_id, "status": "路由失败", "pending": False, "abnormal": True,
                "决策编号": decision_no, "关联批次": intake_batch,
                "推断格式": resolved_format, "命中解析器": "", "备选解析器": "",
                "路由依据": f"格式 {resolved_format} 无注册解析器链",
                "回退次数": "0", "决策时间": decide_time,
            }
            rows.append(entry)
            return False, f"格式 {resolved_format} 没有注册解析器，路由失败", dict(entry)

        entry = {
            "id": entry_id, "status": "已选定", "pending": False, "abnormal": False,
            "决策编号": decision_no, "关联批次": intake_batch,
            "推断格式": resolved_format, "命中解析器": chain[0],
            "备选解析器": ",".join(chain[1:]),
            "路由依据": f"探测格式 {resolved_format}（置信度 {confidence}）命中解析器链首项",
            "回退次数": "0", "决策时间": decide_time,
        }
        rows.append(entry)
        return True, (
            f"批次 {intake_batch} 选定解析器 {chain[0]}，备选 {chain[1:]}"
        ), dict(entry)

    def run_with_fallback(self, decision_no: str,
                          try_parse: Callable[[str], bool],
                          decide_time: str) -> tuple[bool, str, dict[str, Any] | None]:
        """执行首选解析器，失败时按备选顺序回退，回退次数写回决策与解析指标。"""
        decision = self._find(decision_no)
        if decision is None:
            return False, f"路由决策 {decision_no} 不存在", None
        if decision.get("status") == "路由失败":
            return False, (
                f"决策 {decision_no} 已判定路由失败，无解析器可执行"
            ), dict(decision)

        primary = str(decision.get("命中解析器") or "")
        fallbacks = [
            p.strip() for p in str(decision.get("备选解析器") or "").split(",") if p.strip()
        ]
        candidates = ([primary] if primary else []) + fallbacks
        if not candidates:
            decision["status"] = "路由失败"
            decision["abnormal"] = True
            decision["路由依据"] = "无首选也无备选解析器"
            return False, f"决策 {decision_no} 没有可用解析器", dict(decision)

        for index, parser in enumerate(candidates):
            if try_parse(parser):
                decision["命中解析器"] = parser
                decision["回退次数"] = str(index)
                decision["决策时间"] = decide_time
                decision["status"] = "已回退" if index > 0 else "已选定"
                decision["pending"] = False
                decision["abnormal"] = index > 0
                if index > 0:
                    decision["路由依据"] = (
                        f"首选 {primary} 解析失败，按备选顺序回退 {index} 次命中 {parser}"
                    )
                    return True, (
                        f"决策 {decision_no} 回退 {index} 次后由 {parser} 解析成功"
                    ), dict(decision)
                return True, f"决策 {decision_no} 首选 {parser} 命中，无需回退", dict(decision)

        decision["回退次数"] = str(len(candidates))
        decision["status"] = "路由失败"
        decision["pending"] = False
        decision["abnormal"] = True
        decision["路由依据"] = f"全部 {len(candidates)} 个解析器均失败"
        decision["决策时间"] = decide_time
        return False, (
            f"决策 {decision_no} 的 {len(candidates)} 个解析器全部失败，判定路由失败"
        ), dict(decision)

    def fallback_counts(self) -> tuple[int, int]:
        """返回 (发生回退的决策数, 决策总数)，解析指标据此计算真实回退率。"""
        rows = store.rows(self.MODULE)
        fallbacks = 0
        for row in rows:
            text = str(row.get("回退次数") or "0").strip()
            if text.isdigit() and int(text) > 0:
                fallbacks += 1
        return fallbacks, len(rows)

    def failed_route_batches(self) -> list[str]:
        """输出路由失败的投递批次，供解析水位登记滞后原因。"""
        return sorted({
            str(r.get("关联批次")) for r in store.rows(self.MODULE)
            if r.get("status") == "路由失败"
        })
