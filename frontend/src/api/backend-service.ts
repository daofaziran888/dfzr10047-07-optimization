import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---------------------------------------------------------------------------
// 格式改派工作流：人工确认改派、在途核对、诊断请求、正式解析入口均走专用接口，
// 与通用资源动作分开，避免从旧格式操作入口提交正式结果。
// ---------------------------------------------------------------------------

export type InflightSnapshot = {
  intake_batch: string
  probes: EntryRow[]
  resolved_format: string
  attempts: EntryRow[]
  effective_route: EntryRow | null
  official_batch: EntryRow | null
  receipt: EntryRow | null
}

export type RecoveryView = {
  诊断编号: string
  可恢复: boolean
  恢复方式: string
  字段结果: Record<string, string>[]
  恢复动作: string
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    const detail = (await response.json().catch(() => ({}))) as { detail?: string }
    throw new Error(detail.detail ?? `接口返回 ${response.status}`)
  }
  return (await response.json()) as T
}

export async function fetchInflightSnapshot(intakeBatch: string): Promise<InflightSnapshot> {
  const response = await request(`/api/format_probe/inflight/${encodeURIComponent(intakeBatch)}`)
  if (!response.ok) throw new Error('在途解析核对失败')
  return await response.json()
}

export async function confirmAndReassign(
  probeId: number,
  payload: { confirmed_format: string; confirm_time: string; operator: string },
): Promise<ActionResult> {
  return postJson(`/api/format_probe/${probeId}/confirm-reassign`, payload)
}

export async function openCompareRequest(payload: {
  probe_no: string
  intake_batch: string
  attempt_format: string
  sample_rows: Record<string, string>[]
  create_time: string
}): Promise<ActionResult> {
  return postJson('/api/probe_diagnosis/open-compare', payload)
}

export async function submitCompare(
  diagnosisId: number,
  payload: { compare_fields: string[]; submit_time: string },
): Promise<ActionResult> {
  return postJson(`/api/probe_diagnosis/${diagnosisId}/compare`, payload)
}

export async function deliverAttemptFields(
  diagnosisId: number,
  fields: Record<string, string>[],
): Promise<ActionResult> {
  return postJson(`/api/probe_diagnosis/${diagnosisId}/attempt-fields`, { fields })
}

export async function fetchRecoveryView(diagnosisId: number): Promise<RecoveryView> {
  const response = await request(`/api/probe_diagnosis/${diagnosisId}/recovery`)
  if (!response.ok) throw new Error('诊断恢复视图读取失败')
  return await response.json()
}

export async function fetchReassignReceipt(receiptNo: string): Promise<EntryRow> {
  const response = await request(`/api/reassign-receipt/${encodeURIComponent(receiptNo)}`)
  if (!response.ok) throw new Error('改派回执读取失败')
  return await response.json()
}

export async function submitFormalFields(payload: {
  receipt_no: string
  confirmed_format: string
  template_no: string
  rows: Record<string, string | number | boolean>[]
}): Promise<ActionResult> {
  return postJson('/api/field_parse/formal-submit', payload)
}
