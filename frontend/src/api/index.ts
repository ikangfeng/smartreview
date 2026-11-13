// =============================================================================
// API 接口函数 - 与 backend/main.py 路由一一对应
// =============================================================================
import { httpDelete, httpGet, httpPost, httpPut } from './http'
import type {
  AuditLogResult,
  CanaryConfig,
  CanaryConfigPayload,
  CanaryMetrics,
  HealthResult,
  HistoryResult,
  KbDocumentDetail,
  KbDocumentsResult,
  KbIndexResult,
  KbSearchResult,
  KbStats,
  KbUploadResult,
  LlmConfig,
  LlmConfigPayload,
  LlmProbePayload,
  LlmProbeResult,
  Playbook,
  PlaybookPayload,
  PlaybooksResult,
  RiskRule,
  RuleCreatePayload,
  RulePatch,
  RulesResult,
  LearningInsights,
  StartReviewOptions,
  StartReviewResult,
  DraftClauseResult,
  VersionCompareResult,
  TaskResultsResult,
  TaskStatusResult,
  TaskTraceResult,
  TaskVersionsResult,
  UploadResult,
  PromptPreviewResult,
  ClauseAskResult,
  ApplyRevisionsPayload,
  ApplyRevisionsResult,
} from './types'

// ---------------- 上传 / 审查 ----------------
/** 上传合同文件（PDF/DOCX/TXT） */
export function uploadContract(file: File): Promise<UploadResult> {
  const form = new FormData()
  form.append('file', file)
  // 注意：不能手动设 Content-Type，axios 检测到 FormData 会自动带 boundary
  return httpPost<UploadResult>('/upload', form)
}

/** 开始审查（后台异步执行）：可携带方案与人工确认的合同类型/立场 */
export function startReview(
  fileId: string,
  options: StartReviewOptions = {}
): Promise<StartReviewResult> {
  return httpPost<StartReviewResult>('/review', {
    file_id: fileId,
    playbook_id: options.playbookId ?? null,
    contract_type: options.contractType ?? null,
    stance: options.stance ?? null,
  })
}

/** 查询审查进度与已发现风险（前端 2s 轮询） */
export function fetchTaskStatus(taskId: string): Promise<TaskStatusResult> {
  return httpGet<TaskStatusResult>(`/tasks/${taskId}/status`, { silent: true })
}

/** 结果页明细：任务元信息 + 汇总 + 逐条完整结果 */
export function fetchTaskResults(taskId: string): Promise<TaskResultsResult> {
  return httpGet<TaskResultsResult>(`/tasks/${taskId}/results`)
}

/** 回溯一次审查的结构化日志（trace_id 即 taskId）：耗时、通道、token、降级原因 */
export function fetchTaskTrace(taskId: string): Promise<TaskTraceResult> {
  return httpGet<TaskTraceResult>(`/tasks/${taskId}/trace`)
}

/** Word 报告下载地址（window.open 直链） */
export function reportUrl(taskId: string): string {
  return `/tasks/${taskId}/report`
}

/** 修订对照报告下载地址（原文 vs 改写，红删蓝增） */
export function revisedReportUrl(taskId: string): string {
  return `/tasks/${taskId}/revised-report`
}

/** 整改处置清单下载地址（含最新处置状态） */
export function actionListUrl(taskId: string): string {
  return `/tasks/${taskId}/action-list`
}

/** 更新单条问题的处置状态与备注 */
export function updateDisposition(
  taskId: string,
  clauseOrder: number,
  disposition: string,
  note?: string,
  customRevisedText?: string
): Promise<{ status: string; disposition: string; note: string }> {
  return httpPut(`/tasks/${taskId}/results/${clauseOrder}/disposition`, {
    disposition,
    note,
    custom_revised_text: customRevisedText ?? null,
  })
}

/** 为一条缺失条款生成可并入合同的草案 */
export function draftMissingClause(taskId: string, index: number): Promise<DraftClauseResult> {
  return httpPost<DraftClauseResult>(`/tasks/${taskId}/clauses/draft`, { index })
}

/** 合同版本对比复审：baseId=旧版本，targetId=新版本 */
export function compareTasks(baseId: string, targetId: string): Promise<VersionCompareResult> {
  return httpGet<VersionCompareResult>(`/tasks/${baseId}/compare/${targetId}`)
}

// ---------------- 历史 ----------------
export function fetchHistory(limit = 20, offset = 0): Promise<HistoryResult> {
  return httpGet<HistoryResult>('/history', { params: { limit, offset } })
}

/** 删除一条历史记录（级联删除结果与磁盘报告文件） */
export function deleteHistoryTask(taskId: string): Promise<{ status: string; removed_files: number }> {
  return httpDelete(`/history/${taskId}`)
}

// ---------------- 审查方案（Playbook） ----------------
export function fetchPlaybooks(enabledOnly = false): Promise<PlaybooksResult> {
  return httpGet<PlaybooksResult>('/playbooks', { params: { enabled_only: enabledOnly } })
}

export function createPlaybook(payload: PlaybookPayload): Promise<Playbook> {
  return httpPost<Playbook>('/playbooks', payload)
}

export function updatePlaybook(playbookId: number, payload: PlaybookPayload): Promise<Playbook> {
  return httpPut<Playbook>(`/playbooks/${playbookId}`, payload)
}

export function deletePlaybook(playbookId: number): Promise<{ status: string; deleted: number }> {
  return httpDelete(`/playbooks/${playbookId}`)
}

// ---------------- 风控规则 ----------------
export function fetchRules(): Promise<RulesResult> {
  return httpGet<RulesResult>('/rules')
}

/** 手动新增一条规则 */
export function createRule(payload: RuleCreatePayload): Promise<RiskRule> {
  return httpPost<RiskRule>('/rules', payload)
}

export function updateRule(ruleId: number, patch: RulePatch): Promise<RiskRule> {
  return httpPut<RiskRule>(`/rules/${ruleId}`, patch)
}

/** 删除一条规则（删除后不参与审查；重启不会自动复活） */
export function deleteRule(ruleId: number): Promise<{ status: string; deleted: number }> {
  return httpDelete(`/rules/${ruleId}`)
}

// ---------------- 学习闭环（越用越聪明）----------------/** 读取企业样本池规模与"由历史处置归纳出的规则固化建议" */
export function fetchLearningInsights(minSupport = 3): Promise<LearningInsights> {
  return httpGet<LearningInsights>(`/learning/insights?min_support=${minSupport}`)
}

/** 把一条学习建议固化为企业规则（固化为确定性规则后零 token 生效） */
export function applyLearningRule(rule: RuleCreatePayload): Promise<{ status: string; rule: RiskRule }> {
  return httpPost('/learning/apply-rule', rule)
}

// ---------------- 运行时自定义 LLM（模型配置页）----------------
/** 读取运行时自定义 LLM 配置（Key 脱敏） */
export function fetchLlmConfig(): Promise<LlmConfig> {
  return httpGet<LlmConfig>('/llm/config')
}

/** 保存运行时自定义 LLM 配置并即时生效 */
export function saveLlmConfig(payload: LlmConfigPayload): Promise<LlmConfig> {
  return httpPut<LlmConfig>('/llm/config', payload)
}

/** 连通性测试（不落库） */
export function testLlmConfig(payload: LlmProbePayload): Promise<LlmProbeResult> {
  return httpPost<LlmProbeResult>('/llm/test', payload)
}

// ---------------- 灰度发布（canary）----------------
export function fetchCanaryConfig(): Promise<CanaryConfig> {
  return httpGet<CanaryConfig>('/llm/canary')
}

export function saveCanaryConfig(payload: CanaryConfigPayload): Promise<CanaryConfig> {
  return httpPut<CanaryConfig>('/llm/canary', payload)
}

export function fetchCanaryMetrics(): Promise<CanaryMetrics> {
  return httpGet<CanaryMetrics>('/metrics/canary')
}

// ---------------- 审计留痕 + 版本管理 ----------------
export function fetchAuditLogs(params: {
  limit?: number
  offset?: number
  task_id?: string
  action?: string
  target_type?: string
}): Promise<AuditLogResult> {
  return httpGet<AuditLogResult>('/audit/logs', { params })
}

export function fetchTaskVersions(taskId: string): Promise<TaskVersionsResult> {
  return httpGet<TaskVersionsResult>(`/tasks/${taskId}/versions`)
}

// ---------------- 健康 / 知识库 ----------------
export function fetchHealth(): Promise<HealthResult> {
  return httpGet<HealthResult>('/health')
}

export function fetchKbStats(): Promise<KbStats> {
  return httpGet<KbStats>('/knowledge/stats')
}

// ---------------- 知识库管理（知识库页，参照 smartbrief /knowledge 接口）----------------
/** 文档列表：按 (category, source) 聚合的"文件"条目 */
export function fetchKbDocuments(): Promise<KbDocumentsResult> {
  return httpGet<KbDocumentsResult>('/knowledge/documents')
}

/** 单个文档的全部分块（"查看"弹窗） */
export function fetchKbDocumentDetail(source: string, category: string): Promise<KbDocumentDetail> {
  return httpGet<KbDocumentDetail>('/knowledge/document', { params: { source, category } })
}

/** 删除单个文档（全部分块 + 源文件） */
export function deleteKbDocument(source: string, category: string): Promise<{ deleted: number }> {
  return httpDelete<{ deleted: number }>('/knowledge/document', { params: { source, category } })
}

/** 上传法律语料：存盘 → 解析 → 分块 → 向量化入库 */
export function uploadKbDocument(file: File, category: string): Promise<KbUploadResult> {
  const form = new FormData()
  form.append('file', file)
  form.append('category', category)
  // 注意：不能手动设 Content-Type，axios 检测到 FormData 会自动带 boundary
  return httpPost<KbUploadResult>('/knowledge/documents', form)
}

/** 语义检索测试（知识库页"检索测试"数据源） */
export function searchKb(query: string, category?: string, k = 5): Promise<KbSearchResult> {
  return httpPost<KbSearchResult>('/knowledge/search', { query, category: category || null, k })
}

/** 从 data/kb 重建/增量索引（md5 幂等） */
export function rebuildKbIndex(): Promise<KbIndexResult> {
  return httpPost<KbIndexResult>('/knowledge/index')
}

/** 清空知识库 */
export function clearKb(): Promise<{ status: string; message: string }> {
  return httpDelete<{ status: string; message: string }>('/knowledge')
}

// ---------------- Prompt 预览 + 对话式追问 ----------------
/** Prompt Debug：重建指定条款的完整 LLM messages（双通道） */
export function fetchPromptPreview(taskId: string, clauseOrder: number): Promise<PromptPreviewResult> {
  return httpPost<PromptPreviewResult>(`/tasks/${taskId}/prompt-preview`, { clause_order: clauseOrder })
}

/** 对话式追问：基于条款上下文 + 审查结论 + 用户问题，即时回答 */
export function askClause(taskId: string, clauseOrder: number, question: string): Promise<ClauseAskResult> {
  return httpPost<ClauseAskResult>(`/tasks/${taskId}/clauses/${clauseOrder}/ask`, { question })
}

/** 合并已采纳的改写 → 完整修订后合同；可选直接发起复审 */
export function applyRevisions(taskId: string, payload: ApplyRevisionsPayload): Promise<ApplyRevisionsResult> {
  return httpPost<ApplyRevisionsResult>(`/tasks/${taskId}/apply-revisions`, payload)
}
