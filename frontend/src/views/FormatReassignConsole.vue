<template>
  <section class="reassign-console" data-testid="reassign-console">
    <header class="console-head">
      <div>
        <h3>人工格式改派控制台</h3>
        <p class="page-desc">
          确认前先核对服务端识别到的在途解析；改派后凭回执进入字段解析，旧尝试字段仅留档。
        </p>
      </div>
      <div class="console-actions">
        <input
          v-model="intakeBatch"
          data-testid="inflight-batch-input"
          placeholder="输入投递批次编号核对在途解析"
        />
        <button class="btn" data-testid="load-inflight" type="button" @click="loadInflight">
          核对在途解析
        </button>
      </div>
    </header>

    <p v-if="consoleError" class="error-text" data-testid="console-error">{{ consoleError }}</p>
    <p v-if="consoleMessage" class="ok-text" data-testid="console-message">{{ consoleMessage }}</p>

    <!-- 1. 在途解析核对 -->
    <div v-if="snapshot" class="console-card" data-testid="inflight-panel">
      <h4>在途解析核对 · {{ snapshot.intake_batch }}</h4>
      <dl class="dag-detail">
        <dt>当前认定格式</dt>
        <dd>{{ snapshot.resolved_format || '—' }}</dd>
        <dt>有效路由</dt>
        <dd>
          <template v-if="snapshot.effective_route">
            {{ snapshot.effective_route['命中解析器'] }}
            （{{ snapshot.effective_route['实际使用格式'] || snapshot.effective_route['推断格式'] }}，
            {{ snapshot.effective_route['判定版本'] }}）
          </template>
          <template v-else>暂无有效路由</template>
        </dd>
        <dt>唯一正式批次</dt>
        <dd>
          <template v-if="snapshot.official_batch">
            {{ snapshot.official_batch['批次记录编号'] }} ·
            {{ snapshot.official_batch['实际使用格式'] }} ·
            {{ snapshot.official_batch['批次状态'] }}
          </template>
          <template v-else>暂无正式批次</template>
        </dd>
        <dt>改派回执</dt>
        <dd>{{ snapshot.receipt ? snapshot.receipt['回执编号'] : '尚无回执' }}</dd>
      </dl>

      <table v-if="snapshot.attempts.length" class="data-table inner-table">
        <thead>
          <tr>
            <th>诊断编号</th><th>尝试格式</th><th>尝试状态</th><th>样本行数</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="attempt in snapshot.attempts" :key="String(attempt.id)" :data-status="String(attempt.诊断状态)">
            <td>{{ attempt['诊断编号'] }}</td>
            <td>{{ attempt['尝试格式'] }}</td>
            <td>
              <span :class="{ 'legacy-tag': String(attempt.诊断状态).startsWith('旧尝试') }">
                {{ attempt.诊断状态 }}
              </span>
            </td>
            <td>{{ attempt['样本行数'] }}</td>
            <td>
              <button
                class="link"
                type="button"
                :data-testid="`open-attempt-${attempt.id}`"
                @click="inspectAttempt(attempt)"
              >
                打开差异比对
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 2. 人工确认并改派 -->
    <form class="console-card" data-testid="confirm-panel" @submit.prevent="submitConfirm">
      <h4>人工确认格式并改派</h4>
      <div class="analysis-form">
        <label>
          探测结论 ID
          <input v-model="confirmForm.probeId" data-testid="confirm-probe-id" required />
        </label>
        <label>
          确认格式
          <input v-model="confirmForm.confirmedFormat" data-testid="confirm-format" placeholder="xlsx" required />
        </label>
        <label>
          确认时间
          <input v-model="confirmForm.confirmTime" data-testid="confirm-time" required />
        </label>
        <label>
          值班员
          <input v-model="confirmForm.operator" data-testid="confirm-operator" />
        </label>
      </div>
      <button class="btn primary" data-testid="submit-confirm" type="submit">
        确认并取得改派回执
      </button>
    </form>

    <!-- 3. 差异比对请求（可从旧尝试打开） -->
    <form class="console-card" data-testid="compare-panel" @submit.prevent="submitOpenCompare">
      <h4>打开差异比对请求（含旧尝试）</h4>
      <div class="analysis-form">
        <label>探测编号<input v-model="compareForm.probeNo" data-testid="compare-probe-no" required /></label>
        <label>投递批次<input v-model="compareForm.intakeBatch" data-testid="compare-batch" required /></label>
        <label>尝试格式<input v-model="compareForm.attemptFormat" data-testid="compare-format" required /></label>
        <label>样本行（每行 a=1 形式）<input v-model="compareForm.sampleText" data-testid="compare-sample" /></label>
      </div>
      <button class="btn" data-testid="submit-open-compare" type="button" @click="submitOpenCompare">
        打开差异比对
      </button>
      <button
        v-if="openedDiagnosisId"
        class="btn primary"
        data-testid="submit-compare-fields"
        type="button"
        @click="submitCompareFields"
      >
        提交差异比对（{{ openedDiagnosisNo }}）
      </button>
      <button
        v-if="openedDiagnosisId"
        class="btn"
        data-testid="simulate-late-fields"
        type="button"
        @click="simulateLateFields"
      >
        模拟在途/迟到字段回到工作台
      </button>
    </form>

    <!-- 4. 重新登录后的恢复视图 -->
    <div v-if="recovery" class="console-card" data-testid="recovery-panel">
      <h4>诊断恢复视图 · {{ recovery.诊断编号 }}</h4>
      <p :class="recovery.可恢复 ? 'ok-text' : 'warn-text'">
        {{ recovery.可恢复 ? '可恢复' : '需重新发起' }}：{{ recovery.恢复方式 }}
      </p>
      <p v-if="recovery.恢复动作" class="warn-text">恢复动作：{{ recovery.恢复动作 }}</p>
      <table v-if="recovery.字段结果.length" class="data-table inner-table">
        <thead>
          <tr><th>字段名称</th><th>解析取值</th><th>来源标记</th></tr>
        </thead>
        <tbody>
          <tr v-for="(field, index) in recovery.字段结果" :key="index">
            <td>{{ field.字段名称 }}</td>
            <td>{{ field.解析取值 }}</td>
            <td>
              <span class="legacy-tag">{{ field.尝试标记 || field.字段来源 }}</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 5. 改派回执回查 -->
    <div class="console-card" data-testid="receipt-panel">
      <h4>改派回执回查（字段解析详情页再次进入复用同一份）</h4>
      <div class="analysis-form">
        <label>回执编号<input v-model="receiptNo" data-testid="receipt-input" placeholder="RR-FP-002" /></label>
      </div>
      <button class="btn" data-testid="load-receipt" type="button" @click="loadReceipt">打开回执</button>
      <dl v-if="receipt" class="dag-detail receipt-detail">
        <dt>回执编号</dt><dd>{{ receipt['回执编号'] }}</dd>
        <dt>原推断 → 确认格式</dt>
        <dd>{{ receipt['原推断格式'] }} → {{ receipt['确认格式'] }}</dd>
        <dt>魔数字节</dt><dd>{{ receipt['魔数字节'] }}</dd>
        <dt>判定版本</dt><dd>{{ receipt['判定版本'] }}</dd>
        <dt>路由决策</dt><dd>{{ receipt['路由决策编号'] }}</dd>
        <dt>正式批次</dt><dd>{{ receipt['正式批次记录编号'] }}</dd>
        <dt>确认人/时间</dt><dd>{{ receipt['确认人'] }} · {{ receipt['确认时间'] }}</dd>
        <dt>重复确认次数</dt><dd>{{ receipt['重复确认次数'] }}</dd>
      </dl>
    </div>

    <!-- 6. XLSX 正式解析入口（与诊断操作分别处理） -->
    <form class="console-card" data-testid="formal-panel" @submit.prevent="submitFormal">
      <h4>正式解析入口（凭改派回执，拒绝旧格式入口）</h4>
      <div class="analysis-form">
        <label>回执编号<input v-model="formalForm.receiptNo" data-testid="formal-receipt" required /></label>
        <label>确认格式<input v-model="formalForm.confirmedFormat" data-testid="formal-format" required /></label>
        <label>模板编号<input v-model="formalForm.templateNo" data-testid="formal-template" required /></label>
        <label>字段行（JSON）<input v-model="formalForm.rowsText" data-testid="formal-rows" required /></label>
      </div>
      <button class="btn primary" data-testid="submit-formal" type="submit">提交正式字段</button>
    </form>
  </section>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'

import {
  confirmAndReassign,
  deliverAttemptFields,
  fetchInflightSnapshot,
  fetchReassignReceipt,
  fetchRecoveryView,
  openCompareRequest,
  submitCompare,
  submitFormalFields,
} from '@/api/backend-service'
import type {
  ActionResult,
  EntryRow,
} from '@/data/types'
import type {
  InflightSnapshot,
  RecoveryView,
} from '@/api/backend-service'

const emit = defineEmits<{ (event: 'changed'): void }>()

const intakeBatch = ref('')
const snapshot = ref<InflightSnapshot | null>(null)
const receipt = ref<EntryRow | null>(null)
const receiptNo = ref('')
const recovery = ref<RecoveryView | null>(null)
const consoleError = ref('')
const consoleMessage = ref('')
const openedDiagnosisId = ref<number | null>(null)
const openedDiagnosisNo = ref('')

const confirmForm = reactive({
  probeId: '',
  confirmedFormat: 'xlsx',
  confirmTime: '',
  operator: '值班员',
})
const compareForm = reactive({
  probeNo: '',
  intakeBatch: '',
  attemptFormat: 'csv-utf8',
  sampleText: '',
})
const formalForm = reactive({
  receiptNo: '',
  confirmedFormat: 'xlsx',
  templateNo: '',
  rowsText: '',
})

function flash(message: string, ok = true) {
  consoleMessage.value = ok ? message : ''
  consoleError.value = ok ? '' : message
}

function parseSample(text: string): Record<string, string>[] {
  if (!text.trim()) return []
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const row: Record<string, string> = {}
      for (const pair of line.split(',')) {
        const [key, ...rest] = pair.split('=')
        if (key) row[key.trim()] = rest.join('=').trim()
      }
      return row
    })
}

async function loadInflight() {
  consoleError.value = ''
  try {
    snapshot.value = await fetchInflightSnapshot(intakeBatch.value.trim())
  } catch (error) {
    flash(error instanceof Error ? error.message : '在途核对失败', false)
  }
}

async function submitConfirm() {
  try {
    const result: ActionResult = await confirmAndReassign(Number(confirmForm.probeId), {
      confirmed_format: confirmForm.confirmedFormat.trim(),
      confirm_time: confirmForm.confirmTime.trim(),
      operator: confirmForm.operator.trim() || '值班员',
    })
    if (!result.ok) throw new Error(result.message)
    flash(result.message)
    emit('changed')
    if (intakeBatch.value) await loadInflight()
  } catch (error) {
    flash(error instanceof Error ? error.message : '改派失败', false)
  }
}

async function submitOpenCompare() {
  try {
    const result = await openCompareRequest({
      probe_no: compareForm.probeNo.trim(),
      intake_batch: compareForm.intakeBatch.trim(),
      attempt_format: compareForm.attemptFormat.trim(),
      sample_rows: parseSample(compareForm.sampleText),
      create_time: new Date().toISOString(),
    })
    if (!result.ok || !result.entry) throw new Error(result.message)
    const entry = result.entry as EntryRow
    openedDiagnosisId.value = Number(entry.id)
    openedDiagnosisNo.value = String(entry['诊断编号'])
    flash(`${result.message}（诊断状态：${entry['诊断状态']}）`)
    emit('changed')
  } catch (error) {
    flash(error instanceof Error ? error.message : '打开差异比对失败', false)
  }
}

async function submitCompareFields() {
  if (openedDiagnosisId.value === null) return
  try {
    const result = await submitCompare(openedDiagnosisId.value, {
      compare_fields: ['rec_id', 'data_value'],
      submit_time: new Date().toISOString(),
    })
    if (!result.ok) throw new Error(result.message)
    flash(result.message)
    emit('changed')
  } catch (error) {
    flash(error instanceof Error ? error.message : '差异比对提交失败', false)
  }
}

async function simulateLateFields() {
  if (openedDiagnosisId.value === null) return
  try {
    const result = await deliverAttemptFields(openedDiagnosisId.value, [
      { 字段名称: 'rec_id', 解析取值: 'R-LATE-1', 解析行号: '2' },
    ])
    if (!result.ok) throw new Error(result.message)
    recovery.value = await fetchRecoveryView(openedDiagnosisId.value)
    flash(result.message)
    emit('changed')
  } catch (error) {
    flash(error instanceof Error ? error.message : '字段回传失败', false)
  }
}

async function inspectAttempt(attempt: EntryRow) {
  try {
    openedDiagnosisId.value = Number(attempt.id)
    openedDiagnosisNo.value = String(attempt['诊断编号'])
    recovery.value = await fetchRecoveryView(Number(attempt.id))
    flash(`已从${attempt.诊断状态}打开差异比对请求 ${attempt['诊断编号']}`)
  } catch (error) {
    flash(error instanceof Error ? error.message : '打开旧尝试失败', false)
  }
}

async function loadReceipt() {
  try {
    receipt.value = await fetchReassignReceipt(receiptNo.value.trim())
  } catch (error) {
    receipt.value = null
    flash(error instanceof Error ? error.message : '回执读取失败', false)
  }
}

async function submitFormal() {
  try {
    const rows = JSON.parse(formalForm.rowsText) as Record<
      string,
      string | number | boolean
    >[]
    const result = await submitFormalFields({
      receipt_no: formalForm.receiptNo.trim(),
      confirmed_format: formalForm.confirmedFormat.trim(),
      template_no: formalForm.templateNo.trim(),
      rows,
    })
    if (!result.ok) throw new Error(result.message)
    flash(result.message)
    emit('changed')
  } catch (error) {
    flash(error instanceof Error ? error.message : '正式字段提交失败', false)
  }
}
</script>
