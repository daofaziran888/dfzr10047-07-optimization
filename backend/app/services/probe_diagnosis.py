"""探测诊断领域服务：承载人工格式改判前后的在途诊断样本、旧尝试与改派回执，产出探测诊断数据集。

每条诊断请求都绑定一次格式尝试（attempt）：
- 旧 CSV 尝试可以在结论改判为 XLSX 后继续打开差异比对并取回明确标注「旧尝试」的字段；
- 改派回执按探测结论落一份、按批次可回查，重复确认只返回同一份回执，不另外排作业；
- 改判后再回到工作台时，旧请求要么拿到旧字段，要么拿到可执行的恢复动作而不是静默失败。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ProbeDiagnosisService(BaseModuleService):
    MODULE = "probe_diagnosis"
    ENTITY = "探测诊断"
    OPERATION_TERM = "诊断"
    LIST_FIELDS = [
        "诊断编号", "关联批次", "探测编号", "尝试格式", "尝试序号", "诊断类型",
        "诊断状态", "样本行数", "比对字段数", "回执编号", "创建时间",
    ]
    REQUIRED_FIELDS = ["诊断编号", "关联批次", "探测编号", "尝试格式"]
    STATUS_ORDER = ["诊断中", "字段可取", "旧尝试留档", "待恢复", "已恢复", "已冲突"]
    ACTION_RULES = {
        "提交差异比对": "诊断中",
        "标记字段可取": "字段可取",
        "留档旧尝试": "旧尝试留档",
        "标记待恢复": "待恢复",
        "确认恢复": "已恢复",
    }
    NEGATIVE_ACTIONS: list[str] = []

    def _find(self, diagnosis_no: str) -> dict[str, Any] | None:
        return next(
            (r for r in store.rows(self.MODULE) if r.get("诊断编号") == diagnosis_no),
            None,
        )

    def _find_receipt(self, receipt_no: str) -> dict[str, Any] | None:
        return next(
            (r for r in store.rows(self.MODULE)
             if r.get("诊断类型") == "改派回执" and r.get("回执编号") == receipt_no),
            None,
        )

    def _next_attempt_seq(self, intake_batch: str) -> int:
        """同批次下一次尝试序号：CSV 与 XLSX 并发时靠序号区分先后。"""
        used = [
            int(r.get("尝试序号") or 0)
            for r in store.rows(self.MODULE)
            if r.get("关联批次") == intake_batch
            and r.get("诊断类型") != "改派回执"
            and str(r.get("尝试序号") or "").isdigit()
        ]
        return (max(used) + 1) if used else 1

    def attempts_of_batch(self, intake_batch: str) -> list[dict[str, Any]]:
        """列出批次下全部格式尝试，供改判界面核对在途解析。"""
        return [
            dict(r) for r in store.rows(self.MODULE)
            if r.get("关联批次") == intake_batch and r.get("诊断类型") != "改派回执"
        ]

    def open_compare_request(
        self, diagnosis_no: str, probe_no: str, intake_batch: str,
        attempt_format: str, sample_rows: list[dict[str, Any]],
        create_time: str,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """从一次格式尝试打开差异比对请求。

        改判界面需要能从旧尝试直接打开，因此这里允许对已被新结论替代的旧尝试建单，
        但行内会带上「旧尝试」标记，避免旧诊断与新改派结果混用。
        """
        probe = next(
            (r for r in store.rows("format_probe") if r.get("探测编号") == probe_no),
            None,
        )
        if probe is None:
            return False, f"探测结论 {probe_no} 不存在，无法打开诊断", None
        existing = self._find(diagnosis_no)
        if existing is not None:
            return False, f"诊断请求 {diagnosis_no} 已存在，不能重复打开", None

        current_format = str(probe.get("推断格式") or "")
        superseded = bool(current_format) and current_format != attempt_format
        seq = self._next_attempt_seq(intake_batch)
        entry_id = max(
            (int(r.get("id", 0)) for r in store.rows(self.MODULE)), default=0) + 1
        entry = {
            "id": entry_id,
            "status": "旧尝试留档" if superseded else "诊断中",
            "pending": not superseded,
            "abnormal": superseded,
            "诊断编号": diagnosis_no,
            "关联批次": intake_batch,
            "探测编号": probe_no,
            "尝试格式": attempt_format,
            "尝试序号": str(seq),
            "诊断类型": "差异比对",
            "诊断状态": "旧尝试" if superseded else "在途",
            "样本行数": str(len(sample_rows)),
            "比对字段数": "0",
            "回执编号": "",
            "创建时间": create_time,
            "恢复动作": "",
            "样本数据": [dict(r) for r in sample_rows],
            "比对结果": [],
            "字段结果": [],
        }
        store.rows(self.MODULE).append(entry)
        if superseded:
            entry["恢复动作"] = (
                f"该请求基于旧格式 {attempt_format}，当前认定格式已改判为 {current_format}；"
                "可仅取回旧尝试留档字段，正式结果请走改派回执指向的新格式入口"
            )
            msg = (
                f"诊断 {diagnosis_no} 已按旧尝试 {attempt_format} 打开并留档，"
                f"当前认定格式为 {current_format}，字段仅作旧尝试样本回查"
            )
        else:
            msg = f"诊断 {diagnosis_no} 已按 {attempt_format} 打开差异比对请求"
        return True, msg, dict(entry)

    def submit_compare(
        self, diagnosis_no: str, compare_fields: list[str], submit_time: str,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """值班员提交差异比对：先登记比对请求，字段是否能取到取决于尝试是否仍有效。"""
        entry = self._find(diagnosis_no)
        if entry is None:
            return False, f"诊断请求 {diagnosis_no} 不存在", None
        if entry.get("诊断类型") != "差异比对":
            return False, f"{diagnosis_no} 不是差异比对请求", None

        probe = next(
            (r for r in store.rows("format_probe")
             if r.get("探测编号") == entry.get("探测编号")),
            None,
        )
        current_format = str(probe.get("推断格式") or "") if probe else ""
        attempt_format = str(entry.get("尝试格式") or "")
        superseded = bool(current_format) and current_format != attempt_format

        entry["比对字段数"] = str(len(compare_fields))
        entry["比对结果"] = [dict(f) if isinstance(f, dict) else {"字段名称": f}
                            for f in compare_fields]
        entry["提交时间"] = submit_time

        if superseded:
            # 旧 CSV 尝试的字段尚未回来，先把请求挂成待恢复，给出可执行恢复动作
            entry["status"] = "待恢复"
            entry["pending"] = True
            entry["abnormal"] = True
            entry["诊断状态"] = "旧尝试待恢复"
            entry["恢复动作"] = (
                f"格式已改判为 {current_format}，旧 {attempt_format} 字段回来后可留档取回；"
                "若旧尝试无法继续，请按改派回执重新发起 XLSX 诊断"
            )
            return True, (
                f"诊断 {diagnosis_no} 的差异比对已登记，但该尝试已被 {current_format} 改判替代，"
                "字段回来前处于待恢复状态"
            ), dict(entry)

        entry["status"] = "诊断中"
        entry["诊断状态"] = "在途"
        return True, f"诊断 {diagnosis_no} 差异比对已提交，等待解析字段", dict(entry)

    def deliver_attempt_fields(
        self, diagnosis_no: str, fields: list[dict[str, Any]],
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """在途解析字段回到工作台：迟到的旧尝试字段只允许留档，不允许进入正式链路。"""
        entry = self._find(diagnosis_no)
        if entry is None:
            return False, f"诊断请求 {diagnosis_no} 不存在", None
        if entry.get("诊断类型") != "差异比对":
            return False, f"{diagnosis_no} 不是差异比对请求", None

        probe = next(
            (r for r in store.rows("format_probe")
             if r.get("探测编号") == entry.get("探测编号")),
            None,
        )
        current_format = str(probe.get("推断格式") or "") if probe else ""
        attempt_format = str(entry.get("尝试格式") or "")
        superseded = bool(current_format) and current_format != attempt_format

        # 同一份诊断的字段重复回来不叠加，避免迟到重放凭空增加行数
        if entry.get("字段结果"):
            entry["恢复动作"] = (
                "字段已回放过一次，重复到达被忽略；如需正式结果请使用改派回执的新格式入口"
                if superseded else "字段已可取，重复到达被忽略"
            )
            return True, f"诊断 {diagnosis_no} 字段已存在，迟到回放未重复计入", dict(entry)

        stamped = []
        for f in fields:
            row = dict(f)
            row["字段来源"] = f"旧尝试:{attempt_format}" if superseded else f"在途尝试:{attempt_format}"
            row["尝试标记"] = "旧尝试" if superseded else "在途"
            stamped.append(row)
        entry["字段结果"] = stamped

        if superseded:
            entry["status"] = "旧尝试留档"
            entry["pending"] = False
            entry["abnormal"] = True
            entry["诊断状态"] = "旧尝试字段留档"
            entry["恢复动作"] = (
                f"旧 {attempt_format} 字段已留档可取（明确标记为旧尝试）；"
                f"正式解析只认当前 {current_format} 改派批次，旧字段不进入正式结果"
            )
            return True, (
                f"诊断 {diagnosis_no} 的旧 {attempt_format} 字段迟到，已标记为旧尝试留档，"
                "不计入正式批次"
            ), dict(entry)

        entry["status"] = "字段可取"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["诊断状态"] = "字段可取"
        return True, f"诊断 {diagnosis_no} 字段可取，可继续字段解析", dict(entry)

    def recovery_view(self, diagnosis_no: str) -> dict[str, Any] | None:
        """重新登录后回看旧请求：要么给出旧字段，要么给出可执行恢复动作。"""
        entry = self._find(diagnosis_no)
        if entry is None:
            return None
        fields = entry.get("字段结果") or []
        if fields:
            return {
                "诊断编号": diagnosis_no,
                "可恢复": True,
                "恢复方式": "取回旧尝试字段" if entry.get("诊断状态", "").startswith("旧尝试") else "继续字段解析",
                "字段结果": [dict(f) for f in fields],
                "恢复动作": entry.get("恢复动作") or "",
            }
        if entry.get("status") == "待恢复":
            return {
                "诊断编号": diagnosis_no,
                "可恢复": False,
                "恢复方式": "按改派回执重新发起诊断",
                "字段结果": [],
                "恢复动作": entry.get("恢复动作") or "本次请求已失效，请重新发起",
            }
        return {
            "诊断编号": diagnosis_no,
            "可恢复": entry.get("status") in ("诊断中", "字段可取"),
            "恢复方式": "等待在途字段" if entry.get("status") == "诊断中" else "继续字段解析",
            "字段结果": [],
            "恢复动作": entry.get("恢复动作") or "",
        }

    # ------------------------------------------------------------------
    # 改派回执：一次人工确认只落一份，重复确认幂等返回同一份
    # ------------------------------------------------------------------
    def save_reassign_receipt(
        self, receipt_no: str, probe_no: str, intake_batch: str,
        old_format: str, confirmed_format: str, magic_hex: str,
        judgement_version: str, decision_no: str, batch_no: str,
        operator: str, confirm_time: str,
    ) -> dict[str, Any]:
        """落改派回执；同探测结论重复确认时返回既有回执，绝不另外排作业。"""
        existing = next(
            (r for r in store.rows(self.MODULE)
             if r.get("诊断类型") == "改派回执" and r.get("探测编号") == probe_no),
            None,
        )
        if existing is not None:
            existing["重复确认次数"] = str(int(existing.get("重复确认次数") or "1") + 1)
            return dict(existing)

        entry_id = max(
            (int(r.get("id", 0)) for r in store.rows(self.MODULE)), default=0) + 1
        entry = {
            "id": entry_id,
            "status": "字段可取",
            "pending": False,
            "abnormal": False,
            "诊断编号": receipt_no,
            "回执编号": receipt_no,
            "关联批次": intake_batch,
            "探测编号": probe_no,
            "尝试格式": confirmed_format,
            "尝试序号": "0",
            "诊断类型": "改派回执",
            "诊断状态": "正式改派",
            "样本行数": "0",
            "比对字段数": "0",
            "创建时间": confirm_time,
            "原推断格式": old_format,
            "确认格式": confirmed_format,
            "魔数字节": magic_hex,
            "判定版本": judgement_version,
            "路由决策编号": decision_no,
            "正式批次记录编号": batch_no,
            "确认人": operator,
            "确认时间": confirm_time,
            "重复确认次数": "1",
        }
        store.rows(self.MODULE).append(entry)
        return dict(entry)

    def get_receipt(self, *, receipt_no: str = "", probe_no: str = "",
                    intake_batch: str = "") -> dict[str, Any] | None:
        """按回执编号 / 探测编号 / 投递批次回查同一份改派回执。"""
        for row in store.rows(self.MODULE):
            if row.get("诊断类型") != "改派回执":
                continue
            if receipt_no and row.get("回执编号") == receipt_no:
                return dict(row)
            if probe_no and row.get("探测编号") == probe_no:
                return dict(row)
            if intake_batch and row.get("关联批次") == intake_batch:
                return dict(row)
        return None

    def mark_superseded_attempts(self, probe_no: str, confirmed_format: str) -> list[str]:
        """改判生效后把同探测结论下的旧格式尝试全部标成旧尝试留档/待恢复。"""
        marked: list[str] = []
        for row in store.rows(self.MODULE):
            if row.get("探测编号") != probe_no or row.get("诊断类型") != "差异比对":
                continue
            if str(row.get("尝试格式") or "") == confirmed_format:
                continue
            row["诊断状态"] = (
                "旧尝试字段留档" if row.get("字段结果") else "旧尝试待恢复"
            )
            if row.get("字段结果"):
                row["status"] = "旧尝试留档"
                row["pending"] = False
            else:
                row["status"] = "待恢复"
                row["pending"] = True
            row["abnormal"] = True
            row["恢复动作"] = (
                f"格式已改判为 {confirmed_format}，旧 {row.get('尝试格式')} 尝试不得再提交正式结果；"
                "字段回来仅作旧尝试留档，或按改派回执重新发起诊断"
            )
            marked.append(str(row.get("诊断编号")))
        return marked
