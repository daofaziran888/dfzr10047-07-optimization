"""格式探测领域服务：通过文件头魔数特征辨识真实文件格式，产出探测结论数据集。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class FormatProbeService(BaseModuleService):
    MODULE = "format_probe"
    ENTITY = "探测结论"
    OPERATION_TERM = "读取"
    LIST_FIELDS = [
        "探测编号", "关联批次", "魔数特征", "推断格式", "置信度", "结构层级",
        "是否压缩", "探测耗时", "原推断格式", "判定版本", "改派回执编号",
        "确认人", "确认时间",
    ]
    REQUIRED_FIELDS = ["探测编号", "关联批次", "魔数特征"]
    STATUS_ORDER = ["待探测", "已确认", "存疑", "无法识别"]
    ACTION_RULES = {"确认格式": "已确认", "标记存疑": "存疑", "判定无法识别": "无法识别"}
    NEGATIVE_ACTIONS = ["判定无法识别"]

    # 魔数前缀 → (推断格式, 基础置信度, 是否压缩)
    MAGIC_TABLE: dict[str, tuple[str, float, bool]] = {
        "EFBBBF": ("csv-utf8", 0.98, False),
        "FFFE": ("csv-utf16le", 0.95, False),
        "FEFF": ("csv-utf16be", 0.95, False),
        "504B0304": ("xlsx", 0.90, True),
        "7B0A": ("jsonl", 0.80, False),
        "7B0D": ("jsonl-crlf", 0.80, False),
        "3C3F786D6C": ("xml", 0.92, False),
        "1F8B08": ("gzip", 0.97, True),
        "25504446": ("pdf", 0.96, False),
    }

    CONFIRM_THRESHOLD = 0.70

    def probe(self, probe_no: str, intake_batch: str, magic_hex: str,
              declared_format: str, elapsed_ms: int) -> tuple[bool, str, dict[str, Any] | None]:
        """执行魔数探测并写入探测结论。

        真实格式与声明格式不一致时置信度打折并标记存疑，
        改写解析路由输入的推断格式字段，同时回写投递批次。
        """
        upper = magic_hex.upper().strip()
        matched: tuple[str, float, bool] | None = None
        matched_len = 0
        for magic, meta in self.MAGIC_TABLE.items():
            if upper.startswith(magic) and len(magic) > matched_len:
                matched = meta
                matched_len = len(magic)

        if matched is None:
            inferred, confidence, compressed = "text-unknown", 0.15, False
        else:
            inferred, confidence, compressed = matched

        mismatch = (
            bool(declared_format)
            and inferred.split("-")[0] != declared_format.split("-")[0]
        )
        if mismatch:
            confidence = min(confidence, 0.72)

        if confidence >= self.CONFIRM_THRESHOLD and not mismatch:
            status = "已确认"
        elif confidence >= 0.5:
            status = "存疑"
        else:
            status = "无法识别"

        rows = store.rows(self.MODULE)
        entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": entry_id,
            "status": status,
            "pending": status != "已确认",
            "abnormal": status == "无法识别",
            "探测编号": probe_no,
            "关联批次": intake_batch,
            "魔数特征": upper,
            "推断格式": inferred,
            "原推断格式": inferred,
            "置信度": f"{confidence:.2f}",
            "结构层级": "1" if inferred != "text-unknown" else "0",
            "是否压缩": "是" if compressed else "否",
            "探测耗时": str(elapsed_ms),
            "声明格式": declared_format,
            "判定版本": "AUTO-1",
            "改派回执编号": "",
        }
        rows.append(entry)

        # 回写投递批次的探测结果，声明格式与真实格式不一致时留痕
        for batch in store.rows("file_intake"):
            if batch.get("批次编号") != intake_batch:
                continue
            if status == "无法识别":
                batch["status"] = "探测失败"
                batch["pending"] = False
                batch["abnormal"] = True
            else:
                batch["status"] = "待探测"
                batch["pending"] = True
            batch["真实格式"] = inferred
            if mismatch:
                batch["格式改写"] = f"声明 {declared_format} → 探测 {inferred}"
            break

        message = (
            f"批次 {intake_batch} 魔数 {upper} 推断格式 {inferred}"
            f"（置信度 {confidence:.2f}，{status}）"
        )
        if mismatch:
            message += f"，与声明格式 {declared_format} 不一致，解析路由按探测格式改写"
        return status == "已确认", message, dict(entry)

    def resolved_format(self, intake_batch: str) -> str:
        """输出解析路由消费的认定格式：最新探测结论的推断格式。"""
        candidates = [
            r for r in store.rows(self.MODULE)
            if r.get("关联批次") == intake_batch
        ]
        if not candidates:
            return "text-unknown"
        latest = max(candidates, key=lambda r: int(r.get("id", 0)))
        return str(latest.get("推断格式") or "text-unknown")

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """通用动作入口拦截「确认格式」：人工改判必须走带改派回执的专用流程。

        避免值班员在结论改变后仍从旧格式操作入口直接提交正式结果。
        """
        if action == "确认格式":
            return None, (
                "确认格式必须通过「人工确认并改派」专用入口提交，"
                "以取得可继续字段解析的改派回执"
            )
        return super().run_action(entry_id, action)

    def confirm_format(self, probe_no: str, confirmed_format: str,
                       confirm_time: str, operator: str = "值班员"
                       ) -> tuple[bool, str, dict[str, Any] | None]:
        """人工确认/改判格式并完成解析结论改派，返回可继续字段解析的改派回执。

        幂等规则：
        - 同探测结论重复确认同一格式：返回同一份回执，不另外排解析作业；
        - 已确认后改成别的格式：拒绝，防止在途作业状态不一致。
        改派时同步：旧尝试留档、旧路由失效、登记唯一正式解析批次、
        路由决策写入实际使用格式与判定版本、审计留痕（魔数字节/旧 CSV 判定/时间）。
        """
        # 延迟导入避免服务注册阶段的循环依赖
        from app.services import SERVICES

        row = next(
            (r for r in store.rows(self.MODULE) if r.get("探测编号") == probe_no),
            None,
        )
        if row is None:
            return False, f"探测结论 {probe_no} 不存在", None

        intake_batch = str(row.get("关联批次") or "")
        old_format = str(row.get("原推断格式") or row.get("推断格式") or "")
        magic_hex = str(row.get("魔数特征") or "")

        existing_receipt = SERVICES["probe_diagnosis"].get_receipt(probe_no=probe_no)
        if row.get("status") == "已确认" and existing_receipt is not None:
            if str(existing_receipt.get("确认格式")) == confirmed_format:
                # 重复确认：同一份回执计数 +1，路由/批次等作业一律不重新派发
                stored = SERVICES["probe_diagnosis"]._find_receipt(
                    str(existing_receipt.get("回执编号")))
                if stored is not None:
                    stored["重复确认次数"] = str(
                        int(stored.get("重复确认次数") or "1") + 1)
                    existing_receipt = dict(stored)
                return True, (
                    f"探测结论 {probe_no} 已确认为 {confirmed_format}，"
                    f"重复确认复用回执 {existing_receipt.get('回执编号')}，未另排作业"
                ), existing_receipt
            return False, (
                f"探测结论 {probe_no} 已确认格式 {existing_receipt.get('确认格式')}，"
                "不得再改判为其他格式，请新建探测结论处理"
            ), None

        receipt_no = f"RR-{probe_no}"
        judgement_version = f"MANUAL-{int(row.get('id', 0))}-2"

        # 1) 探测结论改判，保留原魔数字节、旧判定与时间，供事后回查
        row["推断格式"] = confirmed_format
        row["置信度"] = "0.95"
        row["status"] = "已确认"
        row["pending"] = False
        row["abnormal"] = False
        row["确认时间"] = confirm_time
        row["确认人"] = operator
        row["判定版本"] = judgement_version
        row["改派回执编号"] = receipt_no

        # 2) 旧格式在途尝试全部留档/置待恢复，旧 CSV 字段迟到也只能旧尝试取回
        marked = SERVICES["probe_diagnosis"].mark_superseded_attempts(
            probe_no, confirmed_format)

        # 3) 路由改派：实际使用格式与判定版本写入路由决策，旧路由失效
        ok_route, _route_msg, decision = SERVICES["parser_route"].reassign_route(
            intake_batch, confirmed_format, judgement_version,
            receipt_no, confirm_time)
        parser_id = str(decision.get("命中解析器") or "")
        decision_no = str(decision.get("决策编号") or "")

        # 4) 解析批次台账：只认唯一正式批次，在途旧批作废，已交付旧批保持原样
        _ok_batch, _batch_msg, batch = SERVICES["parse_batch"].register_official_batch(
            intake_batch, parser_id, confirmed_format, judgement_version,
            receipt_no, confirm_time)
        batch_no = str(batch.get("批次记录编号") or "")

        # 5) 落改派回执（重复确认时这一步天然幂等）
        receipt = SERVICES["probe_diagnosis"].save_reassign_receipt(
            receipt_no, probe_no, intake_batch, old_format, confirmed_format,
            magic_hex, judgement_version, decision_no, batch_no,
            operator, confirm_time)

        # 6) 投递批次回写 + 审计留痕：原先的魔数字节、CSV 判定和确认时间都可查
        for batch_row in store.rows("file_intake"):
            if batch_row.get("批次编号") == intake_batch:
                batch_row["真实格式"] = confirmed_format
                batch_row["判定版本"] = judgement_version
                batch_row["改派回执编号"] = receipt_no
                batch_row["status"] = "已入库"
                batch_row["pending"] = False
                break

        audit_rows = store.rows("intake_audit")
        audit_rows.append({
            "id": max((int(r.get("id", 0)) for r in audit_rows), default=0) + 1,
            "status": "已记录",
            "pending": True,
            "abnormal": False,
            "审计编号": f"IA-{receipt_no}",
            "关联对象": intake_batch,
            "操作类型": "人工格式改派",
            "变更前值": (
                f"魔数字节 {magic_hex}；服务端判定 {old_format}（AUTO-1）"
            ),
            "变更后值": (
                f"人工确认 {confirmed_format}（{judgement_version}）；"
                f"回执 {receipt_no}；正式批次 {batch_no}"
            ),
            "操作者标识": operator,
            "操作来源": "format_probe",
            "操作时间": confirm_time,
        })

        message = (
            f"探测结论 {probe_no} 已人工改判 {old_format}→{confirmed_format}，"
            f"回执 {receipt_no} 可继续字段解析；旧尝试 {len(marked)} 个已留档"
        )
        if not ok_route:
            message += "；注意：新格式暂无注册解析器，路由处于失败状态"
        return True, message, receipt

    def mismatch_batches(self) -> list[dict[str, Any]]:
        """列出声明格式与探测格式跨族不一致的批次，供投递审计留痕。"""
        return [
            {
                "探测编号": r.get("探测编号"),
                "关联批次": r.get("关联批次"),
                "声明格式": r.get("声明格式"),
                "推断格式": r.get("推断格式"),
                "置信度": r.get("置信度"),
            }
            for r in store.rows(self.MODULE)
            if r.get("声明格式")
            and str(r.get("声明格式")).split("-")[0] != str(r.get("推断格式") or "").split("-")[0]
        ]
