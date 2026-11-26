"""格式改派工作流路由：把人工确认、在途核对、诊断请求与正式解析入口与通用资源动作分开。

通用 /api/{module}/actions 只承载无业务判断的状态流转；凡是会改动在途作业、
派发正式批次或产出改派回执的操作，都收敛到这里，避免值班员从旧格式入口误交正式结果。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services import service_for
from app.store import store

router = APIRouter(prefix="/api", tags=["格式改派工作流"])


class ConfirmReassignPayload(BaseModel):
    confirmed_format: str
    confirm_time: str
    operator: str = "值班员"


class CompareOpenPayload(BaseModel):
    probe_no: str
    intake_batch: str
    attempt_format: str
    sample_rows: list[dict[str, Any]] = Field(default_factory=list)
    create_time: str


class CompareSubmitPayload(BaseModel):
    compare_fields: list[Any] = Field(default_factory=list)
    submit_time: str


class AttemptFieldsPayload(BaseModel):
    fields: list[dict[str, Any]] = Field(default_factory=list)


class FormalFieldsPayload(BaseModel):
    receipt_no: str
    confirmed_format: str
    template_no: str
    rows: list[dict[str, Any]] = Field(default_factory=list)


def _probe_by_id(probe_id: int) -> dict[str, Any]:
    row = store.find("format_probe", probe_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"探测结论 {probe_id} 不存在或已归档")
    return row


@router.get("/format_probe/inflight/{intake_batch}", response_model=dict)
def inflight_snapshot(intake_batch: str) -> dict[str, Any]:
    """核对服务端识别到的在途解析：探测结论、各格式尝试、有效路由与正式批次。"""
    probe_svc = service_for("format_probe")
    diag_svc = service_for("probe_diagnosis")
    route_svc = service_for("parser_route")
    batch_svc = service_for("parse_batch")

    probes = [
        dict(r) for r in store.rows("format_probe")
        if r.get("关联批次") == intake_batch
    ]
    return {
        "intake_batch": intake_batch,
        "probes": probes,
        "resolved_format": probe_svc.resolved_format(intake_batch),
        "attempts": diag_svc.attempts_of_batch(intake_batch),
        "effective_route": route_svc.effective_decision(intake_batch),
        "official_batch": batch_svc.official_batch(intake_batch),
        "receipt": diag_svc.get_receipt(intake_batch=intake_batch),
    }


@router.post("/format_probe/{probe_id}/confirm-reassign", response_model=ActionResult)
def confirm_and_reassign(probe_id: int, payload: ConfirmReassignPayload) -> ActionResult:
    """人工确认格式并改派：返回带正式批次与路由信息的改派回执。"""
    probe = _probe_by_id(probe_id)
    probe_svc = service_for("format_probe")
    ok, message, receipt = probe_svc.confirm_format(
        str(probe.get("探测编号")),
        payload.confirmed_format.strip(),
        payload.confirm_time,
        payload.operator,
    )
    return ActionResult(ok=ok, message=message, entry=receipt)


@router.post("/probe_diagnosis/open-compare", response_model=ActionResult)
def open_compare(payload: CompareOpenPayload) -> ActionResult:
    """从一次格式尝试（含已被替代的旧尝试）打开差异比对请求。"""
    diag_svc = service_for("probe_diagnosis")
    diagnosis_no = f"PD-{payload.probe_no}-{payload.attempt_format}"
    ok, message, entry = diag_svc.open_compare_request(
        diagnosis_no, payload.probe_no, payload.intake_batch,
        payload.attempt_format, payload.sample_rows, payload.create_time,
    )
    return ActionResult(ok=ok, message=message, entry=entry)


@router.post("/probe_diagnosis/{diagnosis_id}/compare", response_model=ActionResult)
def submit_compare(diagnosis_id: int, payload: CompareSubmitPayload) -> ActionResult:
    """值班员提交差异比对；旧尝试未取得字段时进入待恢复并给出恢复动作。"""
    row = store.find("probe_diagnosis", diagnosis_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"探测诊断 {diagnosis_id} 不存在或已归档")
    diag_svc = service_for("probe_diagnosis")
    ok, message, entry = diag_svc.submit_compare(
        str(row.get("诊断编号")), payload.compare_fields, payload.submit_time)
    return ActionResult(ok=ok, message=message, entry=entry)


@router.post("/probe_diagnosis/{diagnosis_id}/attempt-fields", response_model=ActionResult)
def deliver_attempt_fields(diagnosis_id: int, payload: AttemptFieldsPayload) -> ActionResult:
    """在途/迟到的尝试字段回到工作台；旧尝试字段只留档，不进正式批次。"""
    row = store.find("probe_diagnosis", diagnosis_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"探测诊断 {diagnosis_id} 不存在或已归档")
    diag_svc = service_for("probe_diagnosis")
    ok, message, entry = diag_svc.deliver_attempt_fields(
        str(row.get("诊断编号")), payload.fields)
    return ActionResult(ok=ok, message=message, entry=entry)


@router.get("/probe_diagnosis/{diagnosis_id}/recovery", response_model=dict)
def recovery_view(diagnosis_id: int) -> dict[str, Any]:
    """重新登录后回看：拿到旧尝试字段，或拿到可执行的恢复动作。"""
    row = store.find("probe_diagnosis", diagnosis_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"探测诊断 {diagnosis_id} 不存在或已归档")
    diag_svc = service_for("probe_diagnosis")
    view = diag_svc.recovery_view(str(row.get("诊断编号")))
    if view is None:
        raise HTTPException(status_code=404, detail="诊断恢复视图不存在")
    return view


@router.get("/reassign-receipt/{receipt_no}", response_model=dict)
def get_receipt(receipt_no: str) -> dict[str, Any]:
    """按回执编号回查同一份改派回执（字段解析详情页再次进入时复用）。"""
    diag_svc = service_for("probe_diagnosis")
    receipt = diag_svc.get_receipt(receipt_no=receipt_no)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"改派回执 {receipt_no} 不存在")
    return receipt


@router.post("/field_parse/formal-submit", response_model=ActionResult)
def formal_submit(payload: FormalFieldsPayload) -> ActionResult:
    """XLSX 等正式解析入口：凭改派回执提交正式字段，与诊断操作分别处理。"""
    field_svc = service_for("field_parse")
    ok, message, entries = field_svc.submit_formal_fields(
        intake_batch=_intake_of_receipt(payload.receipt_no),
        receipt_no=payload.receipt_no,
        confirmed_format=payload.confirmed_format,
        template_no=payload.template_no,
        rows_in=payload.rows,
    )
    return ActionResult(
        ok=ok, message=message,
        entry={"created": len(entries), "receipt_no": payload.receipt_no} if entries else None,
    )


def _intake_of_receipt(receipt_no: str) -> str:
    receipt = service_for("probe_diagnosis").get_receipt(receipt_no=receipt_no)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"改派回执 {receipt_no} 不存在")
    return str(receipt.get("关联批次") or "")
