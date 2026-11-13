<script setup lang="ts">
// =============================================================================
// 审查结果页 - 风险概览 + 逐条详情 + Word 报告下载
// =============================================================================
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { actionListUrl, applyRevisions, askClause, compareTasks, draftMissingClause, fetchAuditLogs, fetchHistory, fetchPromptPreview, fetchTaskResults, fetchTaskTrace, fetchTaskVersions, reportUrl, revisedReportUrl, updateDisposition } from '../api'
import type {
  AuditLogItem, Disposition, DraftClauseResult, HistoryTask, PromptPreviewResult, ReasonablenessIssue, ReviewItem,
  TaskResultsResult, TaskTraceResult, TaskVersionItem, VerdictLevel, VersionCompareChange, VersionCompareResult, ClauseAskResult,
  ApplyRevisionsResult,
} from '../api/types'
import { CHANNEL_LABEL, CHANNEL_TAG_TYPE, RISK_TAG_TYPE, sortByRisk } from '../utils/risk'

const props = defineProps<{ taskId: string }>()
const router = useRouter()

const data = ref<TaskResultsResult | null>(null)
const loadError = ref('')
const filter = ref<'全部' | VerdictLevel>('全部')
// 原文对照：当前定位的条款序号（用于高亮与滚动）
const selectedOrder = ref<number | null>(null)
// 处置闭环：每条结果的编辑态（未保存前只改本地）
const dispEdit = ref<Record<number, { disposition: Disposition; note: string; custom_revised_text: string }>>({})
const savingOrder = ref<number | null>(null)
const DISPOSITIONS: Disposition[] = ['未处理', '已采纳', '已修改', '接受风险', '已忽略']

// 处置状态 → emoji + 颜色 + 子标签
const DISP_META: Record<string, { emoji: string; label: string; sub: string; color: string; bg: string }> = {
  '未处理':   { emoji: '⚪', label: '未处理',   sub: '暂不处置',         color: '#909399', bg: '#f4f4f5' },
  '已采纳':   { emoji: '✅', label: '已采纳',   sub: '采纳 AI 改写',     color: '#10b981', bg: '#ecfdf5' },
  '已修改':   { emoji: '✏️', label: '已修改',   sub: '微调后采用',       color: '#3b82f6', bg: '#eff6ff' },
  '接受风险': { emoji: '⚠️', label: '接受风险', sub: '法务决策不改',     color: '#f59e0b', bg: '#fffbeb' },
  '已忽略':   { emoji: '🗑️', label: '已忽略',   sub: 'AI 误判',          color: '#9ca3af', bg: '#f9fafb' },
}

const emptySummary = { 高: 0, 中: 0, 低: 0, 无: 0 }

// ---------------- 缺失条款草案 ----------------
const draftDialog = ref(false)
const draftName = ref('')
const draftResult = ref<DraftClauseResult | null>(null)
const draftingIndex = ref<number | null>(null)
const accumulatedDrafts = ref<string[]>([])  // 选中要 append 到修订合同的草案文本

async function generateDraft(index: number) {
  draftingIndex.value = index
  try {
    const result = await draftMissingClause(props.taskId, index)
    draftResult.value = result
    draftName.value = result.name
    draftDialog.value = true
  } catch (e: any) {
    ElMessage.error(`草案生成失败：${e?.response?.data?.detail ?? e?.message ?? e}`)
  } finally {
    draftingIndex.value = null
  }
}

async function copyDraft() {
  if (!draftResult.value) return
  try {
    await navigator.clipboard.writeText(draftResult.value.draft)
    ElMessage.success('草案已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选择文本')
  }
}

function addDraftToRevisions() {
  if (!draftResult.value?.draft) return
  accumulatedDrafts.value.push(draftResult.value.draft)
  ElMessage.success(`✨ 已加入修订合同（累计 ${accumulatedDrafts.value.length} 条草案）`)
  draftDialog.value = false
}

// ---------------- 版本对比复审 ----------------
const compareDialog = ref(false)
const compareLoading = ref(false)
const compareResult = ref<VersionCompareResult | null>(null)
const compareTargetId = ref('')
const candidateTasks = ref<HistoryTask[]>([])

const VERSION_STATUS: Record<string, { label: string; tag: 'success' | 'danger' | 'info' | 'warning' | 'primary' }> = {
  fixed: { label: '已修复', tag: 'success' },
  remaining: { label: '仍存在', tag: 'danger' },
  new: { label: '新引入', tag: 'danger' },
  added: { label: '新增条款', tag: 'info' },
  removed: { label: '已删除', tag: 'info' },
}
function versionStatusLabel(s: string) {
  return VERSION_STATUS[s]?.label ?? s
}
function versionStatusTag(s: string): 'success' | 'danger' | 'info' | 'warning' | 'primary' {
  return VERSION_STATUS[s]?.tag ?? 'info'
}

async function openCompare() {
  compareDialog.value = true
  compareResult.value = null
  compareTargetId.value = ''
  try {
    const res = await fetchHistory(100, 0)
    // 仅已完成、非当前任务；同合同名的历史版本排在最前，便于选择旧版本
    const currentName = data.value?.contract_name
    candidateTasks.value = res.items
      .filter((t) => t.status === 'completed' && t.task_id !== props.taskId)
      .sort((a, b) => {
        const sameA = a.contract_name === currentName ? 0 : 1
        const sameB = b.contract_name === currentName ? 0 : 1
        return sameA - sameB
      })
  } catch {
    candidateTasks.value = []
  }
}

async function runCompare() {
  if (!compareTargetId.value) {
    ElMessage.warning('请选择要对比的历史版本')
    return
  }
  compareLoading.value = true
  try {
    // 选中的历史任务作为旧版本（基线），当前页任务作为新版本
    compareResult.value = await compareTasks(compareTargetId.value, props.taskId)
  } catch (e: any) {
    ElMessage.error(`对比失败：${e?.response?.data?.detail ?? e?.message ?? e}`)
  } finally {
    compareLoading.value = false
  }
}

function changeSummary(c: VersionCompareChange) {
  const parts = []
  if (c.base_issue) parts.push(`旧：${c.base_issue}`)
  if (c.target_issue) parts.push(`新：${c.target_issue}`)
  return parts.join('；') || '—'
}

// 质量问题类型 → 标签配色（错别字/数值冲突用红色醒目，格式问题用中性色）
const QI_TAG_TYPE: Record<string, 'danger' | 'warning' | 'info' | 'primary'> = {
  错别字: 'danger',
  数值冲突: 'danger',
  语义歧义: 'warning',
  表述空泛: 'warning',
  术语不一致: 'warning',
  指代不明: 'info',
  格式异常: 'info',
  缺失条款: 'danger',
  主体不一致: 'warning',
  期限矛盾: 'warning',
  权利义务失衡: 'danger',
  责任不对等: 'danger',
  付款交付不匹配: 'warning',
  条款不可执行: 'warning',
  期限不合理: 'warning',
  其他: 'info',
}
function qiTagType(type?: string) {
  return QI_TAG_TYPE[type ?? '其他'] ?? 'info'
}

/** 从质量问题中筛出注入告警：单独渲染成红色安全条，与普通文本瑕疵区分 */
function injectionItems(item: ReviewItem) {
  return (item.quality_issues ?? []).filter((q) => q.type?.includes('注入'))
}

/** 普通文本质量问题（排除注入告警，避免重复展示） */
function normalQualityItems(item: ReviewItem) {
  return (item.quality_issues ?? []).filter((q) => !q.type?.includes('注入'))
}

onMounted(async () => {
  try {
    const result = await fetchTaskResults(props.taskId)
    data.value = result
    initDisposition()
    loadVersions()
    // 任务仍在运行：跳到进度页继续观察
    if (result.status === 'running' || result.status === 'pending') {
      router.replace({ name: 'review', params: { taskId: props.taskId } })
    }
  } catch (e) {
    const status = (e as { response?: { status?: number } })?.response?.status
    loadError.value =
      status === 404 ? '审查任务不存在（任务记录可能已清理）' : '无法加载审查结果，请确认后端服务已启动'
  }
})

const contractTitle = computed(
  () => data.value?.contract_name || data.value?.filename || '未命名合同'
)

const summary = computed(() => data.value?.summary ?? emptySummary)
const riskTotal = computed(() => summary.value['高'] + summary.value['中'] + summary.value['低'])
const checkedTotal = computed(() =>
  Object.values(summary.value).reduce((a: number, b) => a + (Number(b) || 0), 0)
)

// 概览卡片：高/中/低/无
const cards = computed(() =>
  (['高', '中', '低', '无'] as VerdictLevel[]).map((lv) => ({
    level: lv,
    count: summary.value[lv],
    desc:
      lv === '无'
        ? '未发现风险条款'
        : lv === '高'
          ? '需优先处置'
          : lv === '中'
            ? '建议关注'
            : '轻微提示',
  }))
)

// 按严重程度过滤 + 降序排序
const visibleItems = computed<ReviewItem[]>(() => {
  const items = data.value?.items ?? []
  const matched = filter.value === '全部' ? items : items.filter((i) => i.risk_level === filter.value)
  return sortByRisk(matched)
})

const failed = computed(() => data.value?.status === 'failed')

// 原文对照：按 char_index 把全文切成"条款块 + 间隙块"，保证定位与原文顺序一致
interface DocBlock {
  type: 'gap' | 'clause'
  text: string
  order?: number
}
const docBlocks = computed<DocBlock[]>(() => {
  const text = data.value?.full_text || ''
  const items = data.value?.items ?? []
  if (!text) return []
  const sorted = items
    .filter((i) => i.char_index != null)
    .sort((a, b) => (a.char_index ?? 0) - (b.char_index ?? 0))
  if (!sorted.length) return [{ type: 'gap', text }]

  const blocks: DocBlock[] = []
  let cursor = 0
  sorted.forEach((item, idx) => {
    const start = Math.max(0, item.char_index ?? 0)
    const nextStart = idx + 1 < sorted.length ? (sorted[idx + 1].char_index ?? text.length) : text.length
    const end = Math.max(start, Math.min(nextStart, text.length))
    if (start > cursor) blocks.push({ type: 'gap', text: text.slice(cursor, start) })
    blocks.push({ type: 'clause', text: text.slice(start, end), order: item.clause_order ?? 0 })
    cursor = end
  })
  if (cursor < text.length) blocks.push({ type: 'gap', text: text.slice(cursor) })
  return blocks
})

function scrollToClause(order?: number | null) {
  if (order == null) return
  selectedOrder.value = order
  nextTick(() => {
    document.getElementById(`doc-clause-${order}`)
      ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  })
}

// 合同级合理性：把各条款的合理性问题汇总带出条款号，便于定位原文
const reasonIssues = computed(() => {
  const out: Array<ReasonablenessIssue & { clause_id?: string | null }> = []
  for (const item of data.value?.items ?? []) {
    for (const r of item.reasonableness_issues ?? []) {
      out.push({ ...r, clause_id: item.clause_id })
    }
  }
  return out
})
const reasonScore = computed(() => data.value?.reasonableness?.score ?? 100)
const reasonColor = computed(() =>
  reasonScore.value >= 85 ? '#67c23a' : reasonScore.value >= 70 ? '#e6a23c' : '#f56c6c'
)

async function downloadDoc(url: string, fallbackName: string) {
  // 用 fetch 拿二进制 + Blob 下载，绕过 Vite proxy 对 window.open 的 Accept:text/html bypass
  try {
    const resp = await fetch(url)
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const blob = await resp.blob()
    const fname = (resp.headers.get('content-disposition')?.match(/filename="?([^";]+)"?/)?.[1]) || fallbackName
    const blobUrl = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = blobUrl; a.download = fname; a.style.display = 'none'
    document.body.appendChild(a); a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(blobUrl)
  } catch (e: any) {
    ElMessage.error(`下载失败：${e.message ?? e}`)
  }
}

const downloadReport = () =>
  downloadDoc(reportUrl(props.taskId), `合同审查报告_${props.taskId.slice(0, 8)}.docx`)
const downloadRevised = () =>
  downloadDoc(revisedReportUrl(props.taskId), `修订对照_${props.taskId.slice(0, 8)}.docx`)
const downloadActionList = () =>
  downloadDoc(actionListUrl(props.taskId), `整改清单_${props.taskId.slice(0, 8)}.docx`)

// ---------------- 运行日志（结构化 trace，可观测性）----------------
const traceDialog = ref(false)
const traceLoading = ref(false)
const traceData = ref<TaskTraceResult | null>(null)

async function openTrace() {
  traceDialog.value = true
  traceLoading.value = true
  try {
    traceData.value = await fetchTaskTrace(props.taskId)
  } catch (e: any) {
    ElMessage.error(`读取日志失败：${e?.message ?? e}`)
  } finally {
    traceLoading.value = false
  }
}

// ---------------- 版本管理（同一合同族的修订链） ----------------
const versionChain = ref<TaskVersionItem[]>([])

async function loadVersions() {
  try {
    versionChain.value = (await fetchTaskVersions(props.taskId)).versions
  } catch {
    versionChain.value = []
  }
}

function goVersion(v: TaskVersionItem) {
  if (!v.is_current) router.push({ name: 'result', params: { taskId: v.task_id } })
}

// ---------------- 审计留痕 ----------------
const auditDrawer = ref(false)
const auditLoading = ref(false)
const auditItems = ref<AuditLogItem[]>([])
const operator = ref(localStorage.getItem('sr_operator') || '演示用户')

const ACTION_LABEL: Record<string, string> = {
  task_created: '创建审查任务',
  disposition_updated: '更新处置状态',
  revision_applied: '生成修订合同',
  report_downloaded: '下载报告',
  rule_created: '新增风控规则',
  rule_updated: '修改风控规则',
  rule_deleted: '删除风控规则',
  playbook_created: '新增审查方案',
  playbook_updated: '修改审查方案',
  playbook_deleted: '删除审查方案',
  llm_config_saved: '保存模型配置',
  canary_config_saved: '保存灰度配置',
}

function actionLabel(action: string) {
  return ACTION_LABEL[action] || action
}

/** 把 detail 里的关键字段拼成一句人话，避免直接展示原始 JSON */
function auditDetail(detail: Record<string, unknown>) {
  const d = detail || {}
  const parts: string[] = []
  if (d.clause_order != null) parts.push(`第 ${d.clause_order} 条`)
  if (d.from != null || d.to != null) parts.push(`${d.from ?? '—'} → ${d.to ?? '—'}`)
  if (d.name) parts.push(String(d.name))
  if (Array.isArray(d.clause_orders) && d.clause_orders.length) {
    parts.push(`合并 ${d.clause_orders.length} 条改写`)
  }
  if (d.new_task_id) parts.push(`新版本 ${String(d.new_task_id).slice(-8)}`)
  if (d.percent != null && d.percent !== 0) parts.push(`分流 ${d.percent}%`)
  if (d.contract_type) parts.push(String(d.contract_type))
  return parts.join(' · ')
}

async function openAudit() {
  auditDrawer.value = true
  await loadAudit()
}

async function loadAudit() {
  auditLoading.value = true
  try {
    const res = await fetchAuditLogs({ task_id: props.taskId, limit: 100 })
    auditItems.value = res.items ?? []
  } catch (e: any) {
    ElMessage.error(`读取审计日志失败：${e?.message ?? e}`)
  } finally {
    auditLoading.value = false
  }
}

function saveOperator() {
  const name = (operator.value || '').trim() || '演示用户'
  operator.value = name
  localStorage.setItem('sr_operator', name)
  ElMessage.success(`当前操作人：${name}`)
  loadAudit()
}

/** 事件 → 中文说明，让非工程背景的人也能读懂这次审查发生了什么 */const TRACE_LABEL: Record<string, string> = {
  task_started: '开始审查',
  task_completed: '审查完成',
  task_failed: '审查失败',
  clause_done: '条款完成',
  clause_fallback: '条款降级兜底',
  clause_exception: '条款异常',
  parse_failed: 'JSON 解析失败重试',
  provider_failed: '模型端点失败切换',
  retrieval_degraded: '法条检索降级',
  quality_check_degraded: '质量检查降级',
  fewshot_degraded: '先例召回降级',
}

// ---------------- 处置闭环 ----------------
function initDisposition() {
  const map: Record<number, { disposition: Disposition; note: string; custom_revised_text: string }> = {}
  for (const item of data.value?.items ?? []) {
    if (item.clause_order == null) continue
    map[item.clause_order] = {
      disposition: (item.disposition as Disposition) || '未处理',
      note: item.disposition_note || '',
      custom_revised_text: (item as any).custom_revised_text || '',
    }
  }
  dispEdit.value = map
}

// 安全获取/初始化某条款的处置编辑状态
function getDispEdit(order: number | null | undefined) {
  if (order == null) return { disposition: '未处理' as Disposition, note: '', custom_revised_text: '' }
  if (!dispEdit.value[order]) {
    dispEdit.value[order] = { disposition: '未处理', note: '', custom_revised_text: '' }
  }
  return dispEdit.value[order]
}

function setDisp(order: number | null | undefined, d: Disposition, itemRevisedText?: string) {
  if (order == null) return
  if (!dispEdit.value[order]) {
    dispEdit.value[order] = { disposition: '未处理', note: '', custom_revised_text: '' }
  }
  dispEdit.value[order].disposition = d
  // 选"已修改"且还没填过自定义改写 → 预填 AI 原始改写稿
  if (d === '已修改' && !dispEdit.value[order].custom_revised_text && itemRevisedText) {
    dispEdit.value[order].custom_revised_text = itemRevisedText
  }
  // 切到"已采纳"时清理自定义改写（用回模型原始的）
  if (d === '已采纳') {
    dispEdit.value[order].custom_revised_text = ''
  }
}

function dispTagType(value?: string | null) {
  if (value === '已采纳' || value === '已修改') return 'success'
  if (value === '接受风险') return 'warning'
  return 'info'
}

async function saveDisposition(item: ReviewItem) {
  const order = item.clause_order
  if (order == null) return
  const edit = dispEdit.value[order]
  if (!edit) return
  savingOrder.value = order
  try {
    // "已修改"时传用户自定义改写稿，否则传 null
    const customRev = edit.disposition === '已修改' && edit.custom_revised_text?.trim()
      ? edit.custom_revised_text
      : undefined
    await updateDisposition(props.taskId, order, edit.disposition, edit.note, customRev)
    item.disposition = edit.disposition
    item.disposition_note = edit.note
    // 本地重算分布，避免为一次保存重新拉整页
    const counts: Record<string, number> = {}
    for (const it of data.value?.items ?? []) {
      const key = it.disposition || '未处理'
      counts[key] = (counts[key] || 0) + 1
    }
    if (data.value) data.value.disposition_summary = counts
    ElMessage.success('处置状态已保存')
  } catch (e: any) {
    ElMessage.error(`保存失败：${e?.message ?? e}`)
  } finally {
    savingOrder.value = null
  }
}

async function copyText(text?: string | null) {
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制修订条款')
  } catch {
    ElMessage.warning('复制失败，请手动选中文本复制')
  }
}

function copyPromptJson() {
  if (!promptResult.value) return
  navigator.clipboard.writeText(JSON.stringify(promptResult.value, null, 2))
    .then(() => ElMessage.success('已复制'))
    .catch(() => ElMessage.warning('复制失败'))
}

// ---------------- Prompt 预览（Debug 模式） ----------------
const promptDialog = ref(false)
const promptLoading = ref(false)
const promptResult = ref<PromptPreviewResult | null>(null)
const promptChannel = ref<'rule' | 'llm'>('llm')
async function openPromptPreview(item: ReviewItem) {
  if (item.clause_order == null) { ElMessage.warning('该条款无序号'); return }
  promptLoading.value = true
  promptDialog.value = true
  promptResult.value = null
  try {
    const result = await fetchPromptPreview(props.taskId, item.clause_order)
    promptResult.value = result
    // 默认展示实际命中的通道
    promptChannel.value = result.channel_hit
  } catch (e: any) {
    ElMessage.error(`加载失败：${e?.response?.data?.detail ?? e?.message ?? e}`)
    promptDialog.value = false
  } finally {
    promptLoading.value = false
  }
}

// ---------------- 对话式追问 ----------------
const askDialog = ref(false)
const askOrder = ref<number | null>(null)
const askClauseId = ref<string>('')
const askQuestion = ref('')
const askAnswer = ref<ClauseAskResult | null>(null)
const askLoading = ref(false)
const askHistory = ref<Array<{ q: string; a: string }>>([])
async function openAsk(item: ReviewItem) {
  if (item.clause_order == null) { ElMessage.warning('该条款无序号'); return }
  askOrder.value = item.clause_order
  askClauseId.value = item.clause_id || ''
  askQuestion.value = ''
  askAnswer.value = null
  askHistory.value = []
  askDialog.value = true
}
const SAMPLE_QUESTIONS = [
  '这条的核心风险点有哪些？企业通常怎么处理？',
  '改成20%上限的话，法务要注意什么？',
  '帮我起草一个乙方违约责任的对等版本',
  '行业里类似合同通常会怎么写？',
]
async function submitAsk() {
  if (!askQuestion.value.trim() || askOrder.value == null) return
  askLoading.value = true
  const q = askQuestion.value.trim()
  askQuestion.value = ''
  try {
    const result = await askClause(props.taskId, askOrder.value, q)
    askHistory.value.push({ q, a: result.answer })
    askAnswer.value = result
  } catch (e: any) {
    ElMessage.error(`追问失败：${e?.response?.data?.detail ?? e?.message ?? e}`)
  } finally {
    askLoading.value = false
  }
}

const levelKey = (lv: VerdictLevel) => RISK_TAG_TYPE[lv]

// ---------------- 修订合同合并（建议→采纳→产出闭环）----------------
const reviseDialog = ref(false)
const reviseLoading = ref(false)
const reviseResult = ref<ApplyRevisionsResult | null>(null)
const reviseStartReview = ref(false)
const reviseManual = ref(false)
const reviseManualOrders = ref<number[]>([])

// 统计"已采纳/已修改"的条款数 + 有改写的条款列表
const adoptableItems = computed(() => (data.value?.items || []).filter(
  (i) => i.revised_text && ['已采纳', '已修改'].includes(i.disposition || '')
))
const editableItems = computed(() => (data.value?.items || []).filter(
  (i) => i.revised_text
))

function openRevise() {
  reviseManual.value = adoptableItems.value.length === 0 && editableItems.value.length > 0
  reviseManualOrders.value = []
  reviseStartReview.value = false
  reviseResult.value = null
  reviseDialog.value = true
}

async function doApplyRevisions() {
  reviseLoading.value = true
  reviseResult.value = null
  const payload: any = { start_new_review: reviseStartReview.value }
  if (reviseManual.value) {
    payload.auto = false
    payload.clause_orders = reviseManualOrders.value
  } else {
    payload.auto = true
  }
  // 完整性草案回写
  if (accumulatedDrafts.value.length > 0) {
    payload.append_drafts = accumulatedDrafts.value
  }
  try {
    const result = await applyRevisions(props.taskId, payload)
    reviseResult.value = result
    const parts: string[] = []
    if (result.replacements > 0) parts.push(`替换 ${result.replacements} 条条款`)
    if (result.appended_drafts > 0) parts.push(`追加 ${result.appended_drafts} 条补充草案`)
    if (parts.length === 0) {
      ElMessage.warning('没有可合并的改写条款')
    } else {
      ElMessage.success(`成功：${parts.join('，')}`)
    }
    // 合并后清空已累计的草案（防止重复 append）
    accumulatedDrafts.value = []
  } catch (e: any) {
    ElMessage.error(`合并失败：${e?.response?.data?.detail ?? e?.message ?? e}`)
  } finally {
    reviseLoading.value = false
  }
}

function downloadRevisedText() {
  if (!reviseResult.value) return
  const blob = new Blob([reviseResult.value.revised_text], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${data.value?.contract_name || '合同'}_修订版.txt`
  a.click()
  URL.revokeObjectURL(url)
}

function jumpToRevisedResult() {
  if (reviseResult.value?.new_task_id) {
    router.push({ name: 'result', params: { taskId: reviseResult.value.new_task_id } })
  }
}
</script>

<template>
  <div class="page">
    <!-- 加载失败 -->
    <el-result v-if="loadError" icon="error" title="无法打开结果页" :sub-title="loadError">
      <template #extra>
        <el-button type="primary" @click="router.push({ name: 'home' })">返回首页</el-button>
      </template>
    </el-result>

    <template v-else-if="data">
      <!-- 任务失败提示 -->
      <el-alert
        v-if="failed"
        type="error"
        :closable="false"
        class="mb"
        title="审查任务失败"
        :description="data.error || '任务执行中断，请重新上传审查'"
      />

      <!-- 合同信息 -->
      <div class="sr-card">
        <div class="sr-card-title">
          {{ contractTitle }}
          <div class="head-actions">
            <el-button type="primary" @click="downloadReport">
              <el-icon style="margin-right: 4px"><Download /></el-icon>审查报告
            </el-button>
            <el-button @click="downloadRevised">修订对照</el-button>
            <el-button @click="downloadActionList">整改清单</el-button>
            <el-button @click="openTrace">运行日志</el-button>
            <el-button @click="openAudit">审计留痕</el-button>
            <el-button @click="openCompare">版本对比</el-button>
            <el-button
              v-if="editableItems.length > 0"
              type="success"
              @click="openRevise"
            >
              ✨ 生成修订合同
              <el-tag size="small" effect="dark" style="margin-left:6px;background:#10b981;border-color:#10b981">
                {{ adoptableItems.length > 0 ? adoptableItems.length + ' 条已采纳' : editableItems.length + ' 条可编辑' }}
              </el-tag>
            </el-button>
          </div>
        </div>
        <!-- 版本链：同一合同经"修订→复审"形成 v1→vN，点击可切换到任意版本 -->
        <div v-if="versionChain.length > 1" class="version-line">
          <span class="version-cap">版本</span>
          <el-tag
            v-for="v in versionChain"
            :key="v.task_id"
            :type="v.is_current ? 'success' : 'info'"
            :effect="v.is_current ? 'dark' : 'plain'"
            size="small"
            class="version-tag"
            @click="goVersion(v)"
          >
            v{{ v.version }} · {{ v.status === 'completed' ? `高${v.summary?.['高'] ?? 0}` : v.status }}
          </el-tag>
          <span class="version-hint">同一合同族的修订链，点击切换</span>
        </div>
        <el-descriptions :column="2" size="small" border>
          <el-descriptions-item label="甲方">{{ data.party_a || '—' }}</el-descriptions-item>
          <el-descriptions-item label="乙方">{{ data.party_b || '—' }}</el-descriptions-item>
          <el-descriptions-item label="合同金额">{{ data.total_amount || '—' }}</el-descriptions-item>
          <el-descriptions-item label="审查条款数">{{ data.total_clauses }} 条</el-descriptions-item>
          <el-descriptions-item label="审查方案">
            {{ data.playbook_name || '未指定' }}
            <el-tag v-if="data.playbook?.stance" size="small" effect="plain" style="margin-left: 6px">
              {{ data.playbook.stance }}立场
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="数据安全">
            <span v-if="data.masked_count">送审前已脱敏 {{ data.masked_count }} 处</span>
            <span v-else class="text-muted">未检测到需脱敏信息</span>
          </el-descriptions-item>
          <el-descriptions-item label="本次成本">
            <template v-if="data.token_usage && data.token_usage.total_tokens > 0">
              <el-tooltip
                :content="`输入 ${data.token_usage.prompt_tokens} + 输出 ${data.token_usage.completion_tokens} tokens`"
                placement="top"
              >
                <span class="cost-badge">
                  {{ data.token_usage.total_tokens.toLocaleString() }} tokens
                  <b>≈ ¥{{ data.token_usage.estimated_cost_cny.toFixed(3) }}</b>
                </span>
              </el-tooltip>
            </template>
            <span v-else class="text-muted">规则通道命中，未调用模型</span>
          </el-descriptions-item>
          <el-descriptions-item label="模型变体">
            <el-tag
              v-if="data.llm_variant === 'canary'"
              type="warning"
              size="small"
              effect="plain"
            >灰度 canary</el-tag>
            <el-tag
              v-else-if="data.llm_variant === 'mixed'"
              type="danger"
              size="small"
              effect="plain"
            >混合（canary 部分回落）</el-tag>
            <el-tag v-else type="info" size="small" effect="plain">基线 baseline</el-tag>
          </el-descriptions-item>
        </el-descriptions>
      </div>

      <!-- 风险概览 -->
      <div class="sr-card">
        <div class="sr-card-title">风险概览</div>
        <div class="overview">
          <div v-for="c in cards" :key="c.level" class="overview-card" :class="`ov-${c.level}`">
            <div class="ov-level">{{ c.level }}风险</div>
            <div class="ov-count">{{ c.count }}</div>
            <div class="ov-desc">{{ c.desc }}</div>
          </div>
        </div>
        <el-progress
          v-if="checkedTotal"
          :percentage="checkedTotal ? Math.round((riskTotal / checkedTotal) * 100) : 0"
          :stroke-width="10"
          class="risk-rate"
        >
          <span class="risk-rate-text">问题条款占比 {{ checkedTotal ? Math.round((riskTotal / checkedTotal) * 100) : 0 }}%</span>
        </el-progress>
      </div>

      <!-- 合同完整性检查：缺失条款 / 金额大小写 / 跨条款一致性（不依赖 LLM） -->
      <div v-if="data.contract_type || data.contract_issues?.length" class="sr-card">
        <div class="sr-card-title">
          合同完整性检查
          <el-tag v-if="data.contract_type" size="small" effect="plain">{{ data.contract_type }}</el-tag>
          <el-tag v-if="data.contract_issues?.length" size="small" type="danger" effect="dark">
            {{ data.contract_issues.length }} 项
          </el-tag>
          <el-tag v-else size="small" type="success" effect="plain">通过</el-tag>
        </div>
        <el-alert
          v-if="!data.contract_issues?.length"
          type="success"
          :closable="false"
          title="未发现缺失条款与跨条款数值/主体不一致问题"
          description="检查范围：按合同类型的必备条款清单、金额大小写、合同总额/分期/付款比例一致性、期限与主体名称一致性。"
        />
        <ul v-else class="qi-list">
          <li v-for="(q, i) in data.contract_issues" :key="i" class="qi-item">
            <el-tag size="small" :type="qiTagType(q.type)" effect="dark">{{ q.type }}</el-tag>
            <el-tag
              size="small"
              effect="plain"
              :type="q.severity === '高' ? 'danger' : q.severity === '中' ? 'warning' : 'info'"
            >
              {{ q.severity }}
            </el-tag>
            <span class="qi-issue">{{ q.issue }}</span>
            <span v-if="q.original" class="qi-original">原文：{{ q.original }}</span>
            <span v-if="q.suggestion" class="qi-suggest">→ {{ q.suggestion }}</span>
            <el-button
              v-if="q.type === '缺失条款'"
              link
              type="primary"
              size="small"
              :loading="draftingIndex === i"
              @click="generateDraft(i)"
            >
              生成草案
            </el-button>
          </li>
        </ul>
      </div>

      <!-- 合同合理性评估：甲方利益视角，商业失衡而非法律违规 -->
      <div v-if="data.reasonableness" class="sr-card">
        <div class="sr-card-title">
          合同合理性评估
          <span class="reason-score" :style="{ color: reasonColor }">
            {{ data.reasonableness.score }}<small> / 100</small>
          </span>
        </div>
        <el-progress
          :percentage="data.reasonableness.score"
          :stroke-width="10"
          :color="reasonColor"
          :show-text="false"
        />
        <p class="reason-conclusion">{{ data.reasonableness.conclusion }}</p>
        <div v-if="reasonIssues.length" class="reason-meta">
          <el-tag size="small" type="danger">高 {{ data.reasonableness.counts['高'] }}</el-tag>
          <el-tag size="small" type="warning">中 {{ data.reasonableness.counts['中'] }}</el-tag>
          <el-tag size="small" type="info">低 {{ data.reasonableness.counts['低'] }}</el-tag>
        </div>
        <ul v-if="reasonIssues.length" class="qi-list reason-list">
          <li v-for="(r, i) in reasonIssues" :key="i" class="qi-item">
            <el-tag size="small" :type="qiTagType(r.type)" effect="dark">{{ r.type }}</el-tag>
            <el-tag v-if="r.direction" size="small" effect="plain">{{ r.direction }}</el-tag>
            <span class="qi-issue">{{ r.issue }}</span>
            <span v-if="r.clause_id" class="qi-original">{{ r.clause_id }}</span>
            <span v-if="r.suggestion" class="qi-suggest">→ {{ r.suggestion }}</span>
          </li>
        </ul>
        <el-alert
          v-else
          type="success"
          :closable="false"
          title="未发现明显不合理的商业安排"
          description="判断立场：{{ data.playbook?.stance || '甲方' }}。已检查单方权利、责任对等、付款与交付匹配、可执行性与期限锁定。"
        />
      </div>

      <!-- 条款对照：左侧原文 / 右侧审查结果 -->
      <div class="review-split">
        <!-- 左：合同原文（按条款块渲染，点击可定位） -->
        <div class="sr-card doc-pane">
          <div class="sr-card-title">
            合同原文
            <el-tag size="small" effect="plain">点击条款可定位</el-tag>
          </div>
          <div v-if="docBlocks.length" class="doc-body">
            <template v-for="(b, bi) in docBlocks" :key="bi">
              <span v-if="b.type === 'gap'" class="doc-gap">{{ b.text }}</span>
              <span
                v-else
                :id="`doc-clause-${b.order}`"
                class="doc-clause"
                :class="{ active: selectedOrder === b.order }"
                @click="scrollToClause(b.order)"
              >{{ b.text }}</span>
            </template>
          </div>
          <div v-else class="doc-body">{{ data.full_text || '本任务未保存原文（历史任务）' }}</div>
        </div>

        <!-- 右：条款明细 -->
        <div class="sr-card findings-pane">
          <div class="sr-card-title">
            条款明细
            <el-radio-group v-model="filter" size="small">
              <el-radio-button value="全部">全部 ({{ checkedTotal }})</el-radio-button>
              <el-radio-button value="高">高 ({{ summary['高'] }})</el-radio-button>
              <el-radio-button value="中">中 ({{ summary['中'] }})</el-radio-button>
              <el-radio-button value="低">低 ({{ summary['低'] }})</el-radio-button>
              <el-radio-button value="无">无 ({{ summary['无'] }})</el-radio-button>
            </el-radio-group>
          </div>

        <el-empty v-if="!visibleItems.length" description="该分组下暂无条款" />

        <el-collapse v-else>
          <el-collapse-item v-for="(item, idx) in visibleItems" :key="item.clause_id ?? idx" :name="idx">
            <template #title>
              <div class="item-title">
                <span class="item-no mono">{{ item.clause_order ?? '—' }}</span>
                <span class="item-name">{{ item.title || `条款（第 ${item.clause_order ?? '—'} 条）` }}</span>
                <el-tag size="small" :type="levelKey((item.risk_level ?? '无') as VerdictLevel)">
                  {{ item.risk_level ?? '无' }}风险
                </el-tag>
                <el-tag
                  v-if="item.channel"
                  size="small"
                  effect="plain"
                  :type="CHANNEL_TAG_TYPE[item.channel]"
                >
                  {{ CHANNEL_LABEL[item.channel] }}
                </el-tag>
                <el-tag
                  v-if="item.disposition && item.disposition !== '未处理'"
                  size="small"
                  effect="dark"
                  :type="dispTagType(item.disposition)"
                >
                  {{ item.disposition }}
                </el-tag>
                <el-button
                  v-if="item.char_index != null"
                  link
                  type="primary"
                  size="small"
                  @click.stop="scrollToClause(item.clause_order)"
                >
                  定位原文
                </el-button>
                <el-button
                  link
                  type="warning"
                  size="small"
                  @click.stop="openPromptPreview(item)"
                  title="查看发给 LLM 的完整 Prompt"
                >
                  🔍 Prompt
                </el-button>
                <el-button
                  link
                  type="success"
                  size="small"
                  @click.stop="openAsk(item)"
                  title="基于此条款追问"
                >
                  💬 追问
                </el-button>
              </div>
            </template>

            <div class="detail">
              <div class="d-label">条款原文</div>
              <pre class="clause-text">{{ item.content }}</pre>

              <div class="d-grid">
                <div class="d-block">
                  <div class="d-label">风险描述</div>
                  <div class="d-text">{{ item.description || '—' }}</div>
                </div>
                <div class="d-block">
                  <div class="d-label">修改建议</div>
                  <div class="d-text">{{ item.suggestion || '—' }}</div>
                </div>
                <div class="d-block" :class="{ 'span-2': true }">
                  <div class="d-label">法律依据</div>
                  <div class="d-text law">{{ item.law_basis || '未引用（知识库语料为空时降级，规则命中不依赖法条）' }}</div>
                </div>
              </div>

              <!-- 修订后条款：模型改写结果，可直接复制替换原文 -->
              <div v-if="item.revised_text" class="revised-block">
                <div class="d-label">
                  修订后条款
                  <el-button link type="primary" size="small" @click="copyText(item.revised_text)">
                    复制
                  </el-button>
                </div>
                <pre class="revised-text">{{ item.revised_text }}</pre>
              </div>

              <!-- 安全告警：疑似 Prompt 注入（合同正文试图操纵审查结论） -->
              <div v-if="injectionItems(item).length" class="inj-block">
                <div class="inj-head">
                  🛡️ 安全告警 · 疑似指令注入
                  <span class="inj-count">{{ injectionItems(item).length }}</span>
                </div>
                <div v-for="(q, ii) in injectionItems(item)" :key="ii" class="inj-item">
                  <span class="inj-match">命中片段：{{ q.original }}</span>
                  <span class="inj-desc">{{ q.issue }}</span>
                </div>
                <div class="inj-foot">
                  该内容已按不可信文本处理、未被执行；请人工核实条款真实意图
                </div>
              </div>

              <!-- 文本质量问题：错别字/语义歧义/表述空泛/格式/字号，与法律风险分开展示 -->
              <div v-if="normalQualityItems(item).length" class="qi-block">
                <div class="d-label">
                  文本质量问题
                  <el-tag size="small" type="warning" effect="plain">{{ normalQualityItems(item).length }}</el-tag>
                </div>
                <ul class="qi-list">
                  <li v-for="(q, qi) in normalQualityItems(item)" :key="qi" class="qi-item">
                    <el-tag size="small" :type="qiTagType(q.type)" effect="dark">{{ q.type }}</el-tag>
                    <span class="qi-issue">{{ q.issue }}</span>
                    <span v-if="q.original" class="qi-original">原文：{{ q.original }}</span>
                    <span v-if="q.suggestion" class="qi-suggest">→ {{ q.suggestion }}</span>
                  </li>
                </ul>
              </div>

              <!-- 合理性提示：甲方利益视角，与法律风险/文本质量分开展示 -->
              <div v-if="item.reasonableness_issues?.length" class="reason-block">
                <div class="d-label">
                  合理性提示
                  <el-tag size="small" type="warning" effect="plain">
                    {{ item.reasonableness_issues.length }}
                  </el-tag>
                </div>
                <ul class="qi-list">
                  <li v-for="(r, ri) in item.reasonableness_issues" :key="ri" class="qi-item">
                    <el-tag size="small" :type="qiTagType(r.type)" effect="dark">{{ r.type }}</el-tag>
                    <el-tag v-if="r.direction" size="small" effect="plain">{{ r.direction }}</el-tag>
                    <span class="qi-issue">{{ r.issue }}</span>
                    <span v-if="r.original" class="qi-original">原文：{{ r.original }}</span>
                    <span v-if="r.suggestion" class="qi-suggest">→ {{ r.suggestion }}</span>
                  </li>
                </ul>
              </div>

              <!-- 处置闭环：卡片式按钮组 -->
              <div v-if="item.clause_order != null" class="disp-block">
                <div class="d-label">
                  处置状态
                  <el-tag size="small" type="info" effect="plain" style="margin-left: 6px">
                    {{ item.revised_text ? 'AI 已有改写稿' : '无 AI 改写' }}
                  </el-tag>
                </div>

                <!-- Emoji 卡片按钮组 -->
                <div class="disp-cards">
                  <button
                    v-for="d in DISPOSITIONS"
                    :key="d"
                    class="disp-card"
                    :class="{ active: getDispEdit(item.clause_order).disposition === d, saved: item.disposition === d }"
                    :style="{
                      '--d-color': DISP_META[d].color,
                      '--d-bg': DISP_META[d].bg,
                      borderColor: getDispEdit(item.clause_order).disposition === d ? DISP_META[d].color : undefined,
                    }"
                    @click="setDisp(item.clause_order, d, item.revised_text ?? undefined)"
                  >
                    <span class="disp-emoji">{{ DISP_META[d].emoji }}</span>
                    <span class="disp-label">{{ DISP_META[d].label }}</span>
                    <span class="disp-sub">{{ DISP_META[d].sub }}</span>
                    <span v-if="item.disposition === d" class="disp-saved">✓ 已保存</span>
                  </button>
                </div>

                <!-- "已修改"状态：可编辑的改写稿 textarea -->
                <div v-if="getDispEdit(item.clause_order).disposition === '已修改'" class="disp-edit-area">
                  <div class="disp-edit-label">✏️ 编辑改写稿（基于 AI 建议，可自由修改）</div>
                  <el-input
                    type="textarea"
                    :rows="10"
                    :autosize="{ minRows: 6, maxRows: 20 }"
                    :model-value="getDispEdit(item.clause_order).custom_revised_text"
                    @update:model-value="(v: string) => { if (item.clause_order != null) dispEdit[item.clause_order].custom_revised_text = v }"
                    placeholder="点击上面的「已修改」后，AI 改写稿会预填在这里，你可以直接编辑..."
                  />
                </div>

                <!-- 备注 + 保存（选了非"未处理"都显示） -->
                <div v-if="getDispEdit(item.clause_order).disposition && getDispEdit(item.clause_order).disposition !== '未处理'" class="disp-footer">
                  <el-input
                    :model-value="getDispEdit(item.clause_order).note"
                    @update:model-value="(v: string) => { if (item.clause_order != null) dispEdit[item.clause_order].note = v }"
                    size="small"
                    placeholder="备注（可选）：为何接受风险 / 与对方沟通结果"
                  >
                    <template #append>
                      <el-button
                        size="small"
                        type="primary"
                        :loading="savingOrder === item.clause_order"
                        @click="saveDisposition(item)"
                      >
                        💾 保存
                      </el-button>
                    </template>
                  </el-input>
                </div>
              </div>

              <div class="d-meta">
                <span v-if="item.page">页码 {{ item.page }}</span>
                <span v-if="item.confidence != null">置信度 {{ Math.round(item.confidence * 100) }}%</span>
              </div>
            </div>
          </el-collapse-item>
        </el-collapse>
        </div>
      </div>
    </template>

    <div v-else class="sr-card text-muted">正在加载审查结果…</div>

    <!-- 缺失条款草案 -->
    <el-dialog v-model="draftDialog" :title="`补充条款草案：${draftName}`" width="660px">
      <template v-if="draftResult">
        <div class="draft-meta">
          <el-tag size="small" :type="draftResult.source === 'llm' ? 'primary' : 'info'" effect="plain">
            {{ draftResult.source === 'llm' ? 'AI 起草' : '模板降级' }}
          </el-tag>
          <span class="text-muted small">{{ draftResult.suggestion }}</span>
        </div>
        <el-input v-model="draftResult.draft" type="textarea" :rows="12" />
        <p class="text-muted small">草案仅供参考，条款编号与具体数值需法务确认后并入合同。</p>
      </template>
      <div v-else class="text-muted">正在生成草案…</div>
      <template #footer>
        <div style="display: flex; justify-content: space-between; align-items: center; flex: 1">
          <span class="text-muted small">
            已累计 <b>{{ accumulatedDrafts.length }}</b> 条草案待加入修订合同
          </span>
          <div>
            <el-button @click="draftDialog = false">关闭</el-button>
            <el-button :disabled="!draftResult" @click="copyDraft">复制草案</el-button>
            <el-button type="success" :disabled="!draftResult" @click="addDraftToRevisions">
              ✨ 加入修订合同
            </el-button>
          </div>
        </div>
      </template>
    </el-dialog>

    <!-- 版本对比复审 -->
    <el-dialog v-model="compareDialog" title="合同版本对比复审" width="920px" top="5vh">
      <div class="compare-toolbar">
        <span class="text-muted">选择旧版本（当前页为新版本）：</span>
        <el-select v-model="compareTargetId" filterable placeholder="选择历史审查任务" style="width: 380px">
          <el-option
            v-for="t in candidateTasks"
            :key="t.task_id"
            :value="t.task_id"
            :label="`${t.contract_name || t.filename || t.task_id.slice(0, 8)} · ${t.created_at}`"
          />
        </el-select>
        <el-button type="primary" :loading="compareLoading" @click="runCompare">开始对比</el-button>
      </div>

      <template v-if="compareResult">
        <div class="compare-summary">
          <el-tag type="success" effect="dark">已修复 {{ compareResult.summary.fixed }}</el-tag>
          <el-tag type="danger" effect="dark">仍存在 {{ compareResult.summary.remaining }}</el-tag>
          <el-tag type="danger" effect="plain">新引入 {{ compareResult.summary.new }}</el-tag>
          <el-tag type="info" effect="plain">新增条款 {{ compareResult.summary.added }}</el-tag>
          <el-tag type="info" effect="plain">已删除 {{ compareResult.summary.removed }}</el-tag>
        </div>
        <p class="text-muted small">
          旧版本：{{ compareResult.base.contract_name }}（{{ compareResult.base.created_at }}，
          {{ compareResult.base.playbook_name || '未指定方案' }}）
          → 新版本：{{ compareResult.target.contract_name }}（{{ compareResult.target.created_at }}）
        </p>
        <el-table :data="compareResult.changes" size="small" max-height="460">
          <el-table-column label="条款" min-width="150">
            <template #default="{ row }">{{ row.clause_id }} {{ row.title }}</template>
          </el-table-column>
          <el-table-column label="变化" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="versionStatusTag(row.status)" effect="dark">
                {{ versionStatusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="风险" width="110">
            <template #default="{ row }">{{ row.base_risk || '—' }} → {{ row.target_risk || '—' }}</template>
          </el-table-column>
          <el-table-column label="说明" min-width="340">
            <template #default="{ row }">{{ changeSummary(row) }}</template>
          </el-table-column>
        </el-table>
        <div v-if="!compareResult.changes.length" class="text-muted">未发现风险变化。</div>
      </template>
    </el-dialog>

    <!-- Prompt 预览（Debug 模式 —— 面试 Demo 亮点）-->
    <el-dialog v-model="promptDialog" :title="promptResult ? `Prompt 预览 · 条款 ${promptResult.clause_order} ${promptResult.clause_id ?? ''}` : 'Prompt 预览'" width="880px" top="3vh">
      <div v-if="promptLoading" class="text-muted text-center" style="padding: 40px">正在重建 Prompt…</div>
      <template v-else-if="promptResult">
        <div class="prompt-meta">
          <el-tag :type="promptResult.channel_hit === 'rule' ? 'danger' : 'primary'" effect="dark">
            本次命中：{{ promptResult.channel_hit === 'rule' ? '规则通道' : 'LLM 通道' }}
          </el-tag>
          <el-tag size="small" effect="plain">立场：{{ promptResult.stance }}</el-tag>
          <el-tag size="small" type="info" effect="plain">规则数：{{ promptResult.rules.length }}</el-tag>
          <el-tag size="small" type="warning" effect="plain">法条上下文：{{ promptResult.law_context.length }} 字</el-tag>
        </div>
        <div class="prompt-tabs">
          <el-radio-group v-model="promptChannel" size="small">
            <el-radio-button value="rule" :disabled="!promptResult.rule_channel">规则→质量检查 Prompt{{ promptResult.rule_channel ? ` (${promptResult.rule_channel.length} msgs)` : '' }}</el-radio-button>
            <el-radio-button value="llm">LLM 审查 Prompt ({{ (promptResult.llm_channel || []).length }} msgs)</el-radio-button>
          </el-radio-group>
        </div>

        <template v-if="promptChannel === 'rule' && promptResult.rule_channel">
          <div v-for="(m, i) in promptResult.rule_channel" :key="'r-'+i" class="prompt-msg" :class="`msg-${m.role}`">
            <div class="msg-role">{{ m.role }}</div>
            <pre class="msg-body">{{ m.content }}</pre>
          </div>
        </template>
        <template v-else>
          <div v-for="(m, i) in promptResult.llm_channel" :key="'l-'+i" class="prompt-msg" :class="`msg-${m.role}`">
            <div class="msg-role">{{ m.role }}</div>
            <pre class="msg-body">{{ m.content }}</pre>
          </div>
        </template>

        <details class="prompt-details">
          <summary>📋 本次生效的企业风控规则（{{ promptResult.rules.length }} 条）</summary>
          <ul>
            <li v-for="r in promptResult.rules" :key="r.name">
              <el-tag size="small" :type="r.risk_level === '高' ? 'danger' : r.risk_level === '中' ? 'warning' : 'info'">{{ r.risk_level }}</el-tag>
              <b>{{ r.name }}</b> <span class="text-muted small">{{ r.description }}</span>
            </li>
          </ul>
        </details>
      </template>
      <div v-else class="text-muted">无法加载 Prompt</div>
      <template #footer>
        <el-button @click="promptDialog = false">关闭</el-button>
        <el-button v-if="promptResult" type="primary" @click="copyPromptJson">复制 JSON</el-button>
      </template>
    </el-dialog>

    <!-- 对话式追问（面试 Demo 亮点）-->
    <el-dialog v-model="askDialog" :title="`💬 追问 · 条款 ${askOrder ?? ''} ${askClauseId}`" width="560px" top="8vh">
      <div class="ask-chat">
        <!-- 历史消息流 -->
        <div v-if="askHistory.length" class="ask-history">
          <div v-for="(h, i) in askHistory" :key="i" class="ask-bubble-wrap">
            <div class="ask-bubble ask-question">{{ h.q }}</div>
            <div class="ask-bubble ask-answer">{{ h.a }}</div>
          </div>
        </div>
        <div v-else-if="!askLoading" class="ask-empty">
          <div class="ask-empty-title">关于这条条款，你可以问：</div>
          <div class="ask-samples">
            <el-button link type="primary" size="small" v-for="(s, i) in SAMPLE_QUESTIONS" :key="i" @click="askQuestion = s">{{ s }}</el-button>
          </div>
        </div>

        <!-- 加载态 -->
        <div v-if="askLoading" class="ask-loading">
          <span class="pulse-dot"></span> AI 正在思考…
        </div>
      </div>
      <div class="ask-input-row">
        <el-input
          v-model="askQuestion"
          type="textarea"
          :rows="2"
          placeholder="针对这条条款，你想问什么？回车发送"
          :disabled="askLoading"
          @keyup.enter.exact.prevent="submitAsk"
        />
        <el-button type="primary" :loading="askLoading" :disabled="!askQuestion.trim()" @click="submitAsk">发送</el-button>
      </div>
      <p class="text-muted small" style="margin-top: 8px">追问基于当前条款上下文 + 审查结论 + 相关法条即时生成，仅作法务参考。</p>
    </el-dialog>

    <!-- 修订合同合并弹窗（建议→采纳→产出闭环）-->
    <el-dialog v-model="reviseDialog" title="✨ 生成修订后合同" width="640px" top="5vh">
      <!-- Step 1: 选择采纳范围 -->
      <div v-if="!reviseResult" class="revise-step">
        <div class="revise-section-title">选择要合并的改写条款</div>

        <!-- 自动模式 -->
        <div v-if="adoptableItems.length > 0 && !reviseManual" class="revise-option">
          <el-alert
            type="success"
            :closable="false"
            :title="`已检测到 ${adoptableItems.length} 条「已采纳/已修改」的改写，将自动合并进原文`"
            show-icon
            style="margin-bottom: 10px"
          />
          <el-button link type="primary" size="small" @click="reviseManual = true; reviseManualOrders = editableItems.map(i => i.clause_order).filter((v): v is number => v != null)">→ 改为手动选择</el-button>
        </div>

        <!-- 手动模式 -->
        <div v-if="reviseManual || adoptableItems.length === 0" class="revise-manual">
          <div v-if="adoptableItems.length === 0" class="text-muted" style="margin-bottom: 8px">
            没有已采纳的条款，可以手动勾选要合并的改写：
          </div>
          <el-checkbox-group v-model="reviseManualOrders">
            <el-checkbox
              v-for="item in editableItems"
              :key="item.clause_order"
              :value="item.clause_order"
              class="revise-checkbox"
            >
              <el-tag size="small" :type="levelKey((item.risk_level ?? '无') as VerdictLevel)">{{ item.risk_level }}</el-tag>
              <span class="mono clause-id">{{ item.clause_id }}</span>
              <span class="clause-desc">{{ item.title || item.description || item.content?.slice(0, 40) }}</span>
              <span v-if="item.disposition" class="disp-tag">{{ item.disposition }}</span>
            </el-checkbox>
          </el-checkbox-group>
          <div v-if="editableItems.length === 0" class="text-muted">没有可合并的改写条款（所有条款都没有 AI 改写建议）</div>
        </div>

        <el-divider />

        <!-- 附加选项 -->
        <div class="revise-options">
          <el-checkbox v-model="reviseStartReview">
            ✅ 合并完成后，用修订版合同**自动发起复审**（可对比新旧两版的风险变化）
          </el-checkbox>
          <div class="text-muted small" style="margin-top: 4px">
            复审完成后可通过「版本对比」查看：哪些风险被修掉了、有没有引入新的风险
          </div>
        </div>
      </div>

      <!-- Step 2: 结果展示 -->
      <div v-else class="revise-result">
        <el-result icon="success" title="修订后合同已生成" :sub-title="`成功替换 ${reviseResult.replacements} 条条款`">
        </el-result>
        <div class="revise-result-meta">
          <div>采纳条款序号：
            <el-tag v-for="o in reviseResult.applied_clause_orders" :key="o" size="small" effect="plain" style="margin: 0 2px">
              {{ o }}
            </el-tag>
          </div>
          <div v-if="reviseResult.new_task_id" class="text-success">
            ✅ 复审任务已创建：{{ reviseResult.new_task_id.slice(-8) }}（{{ reviseResult.new_total_clauses }} 条条款）
          </div>
        </div>

        <!-- 修订后全文一致性校验：AI 改完，确定性规则再验一遍 -->
        <div v-if="reviseResult.consistency_issues?.length" class="consistency-block">
          <div class="consistency-title">
            ⚠️ 修订后一致性校验：发现 {{ reviseResult.consistency_issues.length }} 项待确认
          </div>
          <div v-for="(c, i) in reviseResult.consistency_issues" :key="i" class="consistency-item">
            <el-tag size="small" :type="c.severity === '高' ? 'danger' : 'info'" effect="plain">
              {{ c.type }}
            </el-tag>
            <span class="consistency-text">{{ c.issue }}</span>
          </div>
          <div class="consistency-hint">以上为确定性规则校验结果，请人工确认后再对外发布。</div>
        </div>
        <div v-else class="consistency-ok">✅ 修订后一致性校验通过：条款编号连续、交叉引用均有效</div>

        <el-divider />
        <details class="revise-preview">
          <summary>📄 修订后合同预览（前 600 字）</summary>
          <pre class="preview-text">{{ reviseResult.revised_text.slice(0, 600) }}...</pre>
        </details>
      </div>

      <template #footer>
        <template v-if="!reviseResult">
          <el-button @click="reviseDialog = false">取消</el-button>
          <el-button
            type="primary"
            :loading="reviseLoading"
            :disabled="(!reviseManual && adoptableItems.length === 0) || (reviseManual && reviseManualOrders.length === 0)"
            @click="doApplyRevisions"
          >
            {{ reviseManual ? `合并选中的 ${reviseManualOrders.length} 条改写` : '合并所有已采纳改写' }}
            <span v-if="reviseStartReview"> + 发起复审</span>
          </el-button>
        </template>
        <template v-else>
          <el-button @click="reviseDialog = false">关闭</el-button>
          <el-button @click="downloadRevisedText">⬇️ 下载修订版合同 (.txt)</el-button>
          <el-button v-if="reviseResult.new_task_id" type="primary" @click="jumpToRevisedResult">→ 查看复审结果</el-button>
        </template>
      </template>
    </el-dialog>
  <!-- 运行日志：一次审查的完整结构化 trace（耗时/通道/token/降级原因） -->
  <el-dialog v-model="traceDialog" title="运行日志 · 结构化追踪" width="880px">
    <div v-loading="traceLoading">
      <template v-if="traceData">
        <div class="trace-summary">
          <div class="trace-stat">
            <span class="trace-num">{{ traceData.summary.total_events }}</span>
            <span class="trace-cap">日志事件</span>
          </div>
          <div class="trace-stat">
            <span class="trace-num">{{ traceData.summary.fallbacks }}</span>
            <span class="trace-cap">降级兜底</span>
          </div>
          <div class="trace-stat">
            <span class="trace-num">{{ traceData.summary.parse_failed }}</span>
            <span class="trace-cap">解析失败重试</span>
          </div>
          <div class="trace-stat">
            <span class="trace-num">{{ traceData.summary.provider_failures }}</span>
            <span class="trace-cap">端点失败切换</span>
          </div>
          <div v-if="traceData.summary.slowest_clause" class="trace-stat">
            <span class="trace-num">{{ traceData.summary.slowest_clause.latency_ms }}ms</span>
            <span class="trace-cap">
              最慢条款 {{ traceData.summary.slowest_clause.clause_id || '-' }}
            </span>
          </div>
        </div>
        <div class="trace-code">
          trace_id = {{ traceData.trace_id }}
        </div>
        <el-table :data="traceData.events" size="small" height="420" class="trace-table">
          <el-table-column label="时间" width="96">
            <template #default="{ row }">{{ (row.ts || '').slice(11) }}</template>
          </el-table-column>
          <el-table-column label="事件" width="150">
            <template #default="{ row }">
              <el-tag
                size="small"
                :type="row.level === 'ERROR' ? 'danger' : row.level === 'WARNING' ? 'warning' : 'info'"
                effect="plain"
              >
                {{ TRACE_LABEL[row.event] || row.event }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="条款" width="70">
            <template #default="{ row }">{{ row.clause_id || '-' }}</template>
          </el-table-column>
          <el-table-column label="通道" width="76">
            <template #default="{ row }">{{ row.channel || '-' }}</template>
          </el-table-column>
          <el-table-column label="耗时" width="86">
            <template #default="{ row }">{{ row.latency_ms != null ? row.latency_ms + 'ms' : '-' }}</template>
          </el-table-column>
          <el-table-column label="tokens" width="80">
            <template #default="{ row }">{{ row.total_tokens || '-' }}</template>
          </el-table-column>
          <el-table-column label="风险" width="60">
            <template #default="{ row }">{{ row.risk_level || '-' }}</template>
          </el-table-column>
          <el-table-column label="模型 / 错误" min-width="180">
            <template #default="{ row }">
              <span class="text-muted">{{ row.provider || row.error || '' }}</span>
            </template>
          </el-table-column>
        </el-table>
      </template>
      <div v-else-if="!traceLoading" class="text-muted">暂无日志（该任务可能早于日志功能上线）</div>
    </div>
    <template #footer>
      <el-button @click="traceDialog = false">关闭</el-button>
    </template>
  </el-dialog>

  <!-- 审计留痕：谁、何时、对哪条做了什么（append-only，不做覆盖） -->
  <el-drawer v-model="auditDrawer" title="审计留痕 · 本任务操作时间线" size="520px">
    <div class="audit-operator">
      <span class="audit-operator-label">当前操作人</span>
      <el-input v-model="operator" size="small" style="width: 150px" placeholder="如：法务-张三" />
      <el-button size="small" @click="saveOperator">保存</el-button>
      <span class="audit-operator-hint">演示环境用本地姓名标识操作人</span>
    </div>
    <div v-loading="auditLoading" class="audit-body">
      <el-timeline v-if="auditItems.length">
        <el-timeline-item
          v-for="item in auditItems"
          :key="item.id"
          :timestamp="item.created_at.replace('T', ' ')"
          placement="top"
        >
          <div class="audit-item-head">
            <el-tag size="small" type="primary" effect="plain">{{ actionLabel(item.action) }}</el-tag>
            <span class="audit-actor">{{ item.actor }}</span>
          </div>
          <div v-if="auditDetail(item.detail)" class="audit-detail">
            {{ auditDetail(item.detail) }}
          </div>
        </el-timeline-item>
      </el-timeline>
      <el-empty v-else-if="!auditLoading" description="本任务暂无审计记录" :image-size="80" />
    </div>
  </el-drawer>

  </div>
</template>

<style scoped>
/* 版本链 */
.version-line {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 10px 0 4px;
}
.version-cap {
  font-size: 12px;
  color: #909399;
}
.version-tag {
  cursor: pointer;
}
.version-hint {
  font-size: 12px;
  color: #c0c4cc;
}

/* 审计留痕 */
.audit-operator {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding-bottom: 12px;
  border-bottom: 1px solid #f0f2f5;
  margin-bottom: 16px;
}
.audit-operator-label {
  font-size: 13px;
  color: #606266;
}
.audit-operator-hint {
  font-size: 12px;
  color: #c0c4cc;
}
.audit-body {
  min-height: 160px;
}
.audit-item-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.audit-actor {
  font-size: 12px;
  color: #606266;
}
.audit-detail {
  margin-top: 4px;
  font-size: 12px;
  color: #909399;
  line-height: 1.6;
}

/* 运行日志 */
.trace-summary {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.trace-stat {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  padding: 8px 16px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  min-width: 92px;
}
.trace-num {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 17px;
  font-weight: 700;
  color: #1e293b;
}
.trace-cap {
  font-size: 11px;
  color: #64748b;
}
.trace-code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: #475569;
  background: #f1f5f9;
  border-radius: 6px;
  padding: 6px 10px;
  margin-bottom: 10px;
  word-break: break-all;
}
.trace-table :deep(.el-table__cell) {
  padding: 4px 0;
}
.mb {
  margin-bottom: 16px;
}

/* 缺失条款草案 */
.draft-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

/* 版本对比 */
.compare-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}

.compare-summary {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.overview {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}

.overview-card {
  border-radius: 8px;
  padding: 14px 16px;
  border: 1px solid #eef0f4;
  background: #fafbfd;
}

.ov-level {
  font-size: 13px;
  color: #4c5157;
}

.ov-count {
  font-size: 34px;
  font-weight: 700;
  margin: 4px 0;
}

.ov-desc {
  font-size: 12px;
  color: #8a919f;
}

.ov-高 .ov-count {
  color: #f56c6c;
}

.ov-中 .ov-count {
  color: #e6a23c;
}

.ov-低 .ov-count {
  color: #909399;
}

.ov-无 .ov-count {
  color: #67c23a;
}

.risk-rate {
  margin-top: 20px;
}

.risk-rate-text {
  font-size: 12px;
  color: #4c5157;
}

.item-title {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 1;
  min-width: 0;
  padding-right: 12px;
}

.item-no {
  color: #8a919f;
  font-size: 13px;
}

.item-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 14px;
}

.detail {
  padding: 4px 8px 8px;
}

.d-label {
  font-size: 12px;
  color: #8a919f;
  margin-bottom: 6px;
}

.clause-text {
  margin: 0 0 16px;
  padding: 12px 14px;
  background: #f7f8fa;
  border-radius: 8px;
  font-family: inherit;
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  color: #3a3f45;
  max-height: 200px;
  overflow: auto;
}

.d-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.d-block.span-2 {
  grid-column: span 2;
}

.d-text {
  font-size: 13px;
  line-height: 1.7;
  color: #3a3f45;
}

.d-text.law {
  color: #4a6abf;
}

.d-meta {
  margin-top: 12px;
  font-size: 12px;
  color: #b0b6bf;
  display: flex;
  gap: 16px;
}

/* 文本质量问题区块 */
.qi-block {
  margin-top: 14px;
  padding: 12px 14px;
  background: #fffbf0;
  border: 1px solid #ffe0a3;
  border-radius: 8px;
}
.qi-block .d-label {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.qi-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.qi-item {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
  font-size: 13px;
  line-height: 1.6;
}
.qi-issue {
  color: #3a3f47;
  font-weight: 500;
}
.qi-original {
  color: #b26a00;
  background: #fff3d6;
  padding: 1px 6px;
  border-radius: 4px;
  font-family: ui-monospace, monospace;
  font-size: 12px;
}
.qi-suggest {
  color: #2e7d32;
}

/* 合同合理性评估 */
.reason-score {
  margin-left: auto;
  font-size: 26px;
  font-weight: 700;
  line-height: 1;
}
.reason-score small {
  font-size: 12px;
  color: #8a919f;
  font-weight: 400;
}
.reason-conclusion {
  margin: 12px 0 10px;
  font-size: 13px;
  color: #3a3f45;
}
.reason-meta {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}
.reason-list {
  margin-top: 4px;
}
.reason-block {
  margin-top: 12px;
  padding: 12px 14px;
  background: #fff7f0;
  border: 1px solid #ffd9b8;
  border-radius: 8px;
}
.reason-block .d-label {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

/* 原文对照：左原文 / 右结果 */
.head-actions {
  display: flex;
  gap: 8px;
}
.review-split {
  display: grid;
  grid-template-columns: 420px minmax(0, 1fr);
  gap: 18px;
  align-items: start;
}
.doc-pane {
  position: sticky;
  top: 16px;
}
.doc-body {
  max-height: 72vh;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 13px;
  line-height: 1.85;
  color: #3a3f45;
  padding-right: 6px;
}
.doc-clause {
  cursor: pointer;
  border-radius: 4px;
  transition: background-color 0.2s ease;
}
.doc-clause:hover {
  background: #f2f6ff;
}
.doc-clause.active {
  background: #fff3d6;
  box-shadow: inset 0 0 0 1px #ffd591;
}
.doc-gap {
  color: #8a919f;
}

/* 修订后条款 */
.revised-block {
  margin-top: 14px;
  padding: 14px 16px;
  background: #f2f7ff;
  border: 1px solid #cfe0ff;
  border-radius: 10px;
}
.revised-block .d-label {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 14px;
  font-weight: 600;
  color: #2563eb;
  margin-bottom: 10px;
}
.revised-text {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: inherit;
  font-size: 14px;
  line-height: 1.85;
  color: #1f4f8f;
  min-height: 80px;
  padding: 12px 14px;
  background: #ffffff;
  border-radius: 6px;
  border-left: 3px solid #2563eb;
}

/* 安全告警：疑似 Prompt 注入 */
.inj-block {
  margin-top: 14px;
  padding: 12px 14px;
  background: #fef2f2;
  border: 1px solid #fecaca;
  border-left: 4px solid #dc2626;
  border-radius: 8px;
}
.inj-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 700;
  color: #b91c1c;
  margin-bottom: 8px;
}
.inj-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 9px;
  background: #dc2626;
  color: #fff;
  font-size: 11px;
}
.inj-item {
  display: flex;
  flex-direction: column;
  gap: 3px;
  margin-bottom: 6px;
}
.inj-match {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: #7f1d1d;
  background: #fee2e2;
  border-radius: 4px;
  padding: 2px 6px;
  word-break: break-all;
}
.inj-desc {
  font-size: 12px;
  color: #991b1b;
  line-height: 1.6;
}
.inj-foot {
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed #fecaca;
  font-size: 12px;
  color: #b91c1c;
}

/* 成本徽标：本次审查 token 与估算费用 */
.cost-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: #0f766e;
  background: #ecfdf5;
  border: 1px solid #a7f3d0;
  border-radius: 6px;
  padding: 2px 8px;
  cursor: default;
}
.cost-badge b {
  color: #047857;
}

/* 处置闭环 */
.disp-block {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px dashed #e5e8ef;
}
.disp-block {
  margin-top: 14px;
  padding: 12px 14px;
  background: #fafbfc;
  border-radius: 10px;
  border: 1px solid #ebeef5;
}

.disp-cards {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 8px;
  margin-top: 8px;
}

.disp-card {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 10px 4px;
  border: 2px solid #e5e7eb;
  border-radius: 8px;
  background: #ffffff;
  cursor: pointer;
  transition: all 0.18s ease;
  text-align: center;
  line-height: 1.2;
}
.disp-card:hover {
  border-color: #c0c4cc;
  transform: translateY(-1px);
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.06);
}
.disp-card.active {
  border-color: var(--d-color, #409eff);
  background: var(--d-bg, #ecf5ff);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
}
.disp-card.saved {
  border-color: var(--d-color, #10b981);
  background: var(--d-bg, #ecfdf5);
}

.disp-emoji {
  font-size: 20px;
  line-height: 1;
  margin-bottom: 4px;
}
.disp-label {
  font-size: 12px;
  font-weight: 600;
  color: #303133;
}
.disp-card.active .disp-label,
.disp-card.saved .disp-label {
  color: var(--d-color, #303133);
}
.disp-sub {
  font-size: 10px;
  color: #9ca3af;
  margin-top: 2px;
}

.disp-saved {
  position: absolute;
  top: 4px;
  right: 6px;
  font-size: 9px;
  color: var(--d-color, #10b981);
  font-weight: 600;
}

.disp-footer {
  margin-top: 10px;
  animation: fadeIn 0.2s ease;
}
.disp-edit-area {
  margin-top: 10px;
  animation: fadeIn 0.2s ease;
}
.disp-edit-label {
  font-size: 12px;
  font-weight: 600;
  color: #3b82f6;
  margin-bottom: 6px;
}
.disp-edit-area :deep(.el-textarea__inner) {
  font-size: 13px;
  line-height: 1.7;
  border-color: #3b82f6;
  background: #f0f7ff;
}
.disp-edit-area :deep(.el-textarea__inner:focus) {
  border-color: #2563eb;
  box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.15);
}
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(-4px); }
  to { opacity: 1; transform: translateY(0); }
}

@media (max-width: 860px) {
  .disp-cards {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 1100px) {
  .review-split {
    grid-template-columns: 1fr;
  }
  .doc-pane {
    position: static;
  }
  .doc-body {
    max-height: 320px;
  }
}

@media (max-width: 720px) {
  .overview {
    grid-template-columns: repeat(2, 1fr);
  }

  .d-grid {
    grid-template-columns: 1fr;
  }

  .d-block.span-2 {
    grid-column: span 1;
  }
}

/* Prompt 预览弹窗 */
.prompt-meta {
  display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px;
}
.prompt-tabs { margin-bottom: 12px; }
.prompt-msg {
  display: flex;
  gap: 10px;
  margin-bottom: 10px;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid #ebeef5;
}
.prompt-msg.msg-system { border-color: #e6f0ff; background: #f5f9ff; }
.prompt-msg.msg-user { border-color: #e8f5e9; background: #f6fcf7; }
.prompt-msg.msg-assistant { border-color: #fff3e0; background: #fff9f0; }
.msg-role {
  min-width: 70px;
  font-size: 12px;
  font-weight: 600;
  padding: 10px 8px;
  text-align: center;
  color: #606266;
  background: #fafbfc;
  border-right: 1px solid #ebeef5;
}
.msg-system .msg-role { color: #1d4ed8; background: #e6f0ff; }
.msg-user .msg-role { color: #16a34a; background: #e8f5e9; }
.msg-assistant .msg-role { color: #ea580c; background: #fff3e0; }
.msg-body {
  margin: 0;
  padding: 10px 12px;
  font-size: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  color: #3a3f45;
  max-height: 420px;
  overflow: auto;
  font-family: inherit;
}
.prompt-details {
  margin-top: 10px;
  padding: 10px 14px;
  background: #f5f7fa;
  border-radius: 6px;
  font-size: 12px;
}
.prompt-details summary {
  cursor: pointer;
  font-weight: 500;
  margin-bottom: 6px;
  color: #606266;
}
.prompt-details ul { margin: 0; padding-left: 16px; }
.prompt-details li { margin-bottom: 4px; }

/* 追问弹窗 */
.ask-chat {
  min-height: 220px;
  max-height: 400px;
  overflow-y: auto;
  padding: 4px;
  margin-bottom: 12px;
}
.ask-history { display: flex; flex-direction: column; gap: 10px; }
.ask-bubble-wrap { display: flex; flex-direction: column; gap: 6px; }
.ask-bubble {
  padding: 10px 14px;
  border-radius: 12px;
  font-size: 13px;
  line-height: 1.6;
  max-width: 90%;
  white-space: pre-wrap;
  word-break: break-word;
}
.ask-question {
  align-self: flex-end;
  background: #409eff;
  color: #fff;
  border-bottom-right-radius: 2px;
}
.ask-answer {
  align-self: flex-start;
  background: #f5f7fa;
  color: #3a3f45;
  border: 1px solid #ebeef5;
  border-bottom-left-radius: 2px;
}
.ask-empty {
  text-align: center;
  color: #909399;
  padding: 30px 10px;
}
.ask-empty-title { font-size: 13px; margin-bottom: 12px; color: #606266; }
.ask-samples {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: center;
}
.ask-loading {
  text-align: center;
  color: #909399;
  padding: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  font-size: 13px;
}
.pulse-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: #409eff;
  animation: pulse 1.4s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.4; transform: scale(0.8); }
}
.ask-input-row {
  display: flex;
  gap: 10px;
  align-items: flex-end;
}

/* ===== 修订合同合并弹窗 ===== */
.revise-section-title {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 10px;
  color: #303133;
}
.revise-option {
  padding: 10px 14px;
  background: linear-gradient(135deg, #ecfdf5 0%, #ffffff 100%);
  border-radius: 8px;
  border: 1px solid #a7f3d0;
  margin-bottom: 8px;
}
.revise-manual {
  max-height: 260px;
  overflow-y: auto;
  padding: 10px 12px;
  background: #fafbfc;
  border-radius: 8px;
  border: 1px solid #ebeef5;
}
.revise-checkbox {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  font-size: 13px;
}
.revise-checkbox .clause-id {
  color: #909399;
  min-width: 48px;
}
.revise-checkbox .clause-desc {
  flex: 1;
  color: #4c5157;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.revise-checkbox .disp-tag {
  font-size: 12px;
  color: #10b981;
}
.revise-options {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.revise-result-meta {
  padding: 10px 14px;
  background: #f5f7fa;
  border-radius: 6px;
  font-size: 13px;
  color: #4c5157;
  line-height: 1.8;
}
.consistency-block {
  margin-top: 12px;
  padding: 12px 14px;
  background: #fef2f2;
  border: 1px solid #fecaca;
  border-radius: 6px;
}
.consistency-title {
  font-size: 13px;
  font-weight: 600;
  color: #b91c1c;
  margin-bottom: 8px;
}
.consistency-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 6px;
  font-size: 13px;
  color: #7f1d1d;
  line-height: 1.6;
}
.consistency-text { flex: 1; }
.consistency-hint {
  font-size: 12px;
  color: #9ca3af;
  margin-top: 6px;
}
.consistency-ok {
  margin-top: 12px;
  padding: 10px 14px;
  background: #ecfdf5;
  border: 1px solid #a7f3d0;
  border-radius: 6px;
  font-size: 13px;
  color: #047857;
}
.revise-preview summary {
  cursor: pointer;
  padding: 8px;
  font-size: 13px;
  color: #606266;
  background: #f5f7fa;
  border-radius: 6px;
  user-select: none;
}
.preview-text {
  margin: 8px 0;
  padding: 12px;
  font-size: 12px;
  line-height: 1.6;
  background: #ffffff;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  white-space: pre-wrap;
  max-height: 320px;
  overflow: auto;
}
</style>
