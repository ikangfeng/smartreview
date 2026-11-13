// =============================================================================
// API 类型定义 - 与 backend/main.py 的响应契约一一对应
// =============================================================================

export type RiskLevel = '高' | '中' | '低'
export type VerdictLevel = RiskLevel | '无'
export type TaskStatus = 'pending' | 'running' | 'completed' | 'failed'
/** 判定渠道：rule=确定性规则命中；llm=模型判定；fallback=LLM 不可用时降级提示 */
export type Channel = 'rule' | 'llm' | 'fallback'

/** 问题处置状态（处置闭环） */
export type Disposition = '未处理' | '已采纳' | '已修改' | '接受风险' | '已忽略'

/** 各风险等级统计（键为中文等级名） */
export interface Summary {
  高: number
  中: number
  低: number
  无: number
}

// ---------------- 上传 / 审查 ----------------
export interface UploadResult {
  file_id: string
  filename: string
  page_count: number
  estimate_clauses: number
  /** 自动识别的合同类型（上传页供用户确认/修正） */
  detected_contract_type?: string
  /** 后端支持的合同类型枚举 */
  contract_types?: string[]
  /** 无文字层、已走 OCR 的页数 */
  ocr_pages?: number
}

/** 发起审查的可选参数：方案 + 人工确认的类型/立场 */
export interface StartReviewOptions {
  playbookId?: number
  contractType?: string
  stance?: string
}

export interface StartReviewResult {
  task_id: string
  total_clauses: number
  /** 本次采用的审查方案（自动匹配或手动指定） */
  playbook_id?: number | null
  playbook_name?: string | null
}

// ---------------- 审查方案（Playbook） ----------------
/** 方案必备条款项（完整性检查数据源） */
export interface PlaybookRequiredClause {
  name: string
  /** 命中任一关键词即视为该条款已写入 */
  keywords: string[]
  severity: '高' | '中' | '低'
  suggestion: string
}

export interface Playbook {
  id: number
  name: string
  contract_type: string | null
  stance: string
  description: string | null
  required_clauses: PlaybookRequiredClause[]
  /** 参与审查的规则 id；空数组 = 全部启用规则 */
  rule_ids: number[]
  /** 限定 RAG 检索的知识库分类；空数组 = 不限 */
  kb_categories: string[]
  extra_prompt: string | null
  is_default: number
  enabled: number
  created_at?: string | null
  updated_at?: string | null
}

export interface PlaybooksResult {
  items: Playbook[]
  stances: string[]
  kb_categories: string[]
}

/** 方案创建/更新载荷（PUT 为全量替换） */
export interface PlaybookPayload {
  name: string
  contract_type?: string | null
  stance?: string
  description?: string
  required_clauses?: PlaybookRequiredClause[]
  rule_ids?: number[]
  kb_categories?: string[]
  extra_prompt?: string
  is_default?: boolean
  enabled?: boolean
}

/** 轮询进度返回的精简发现（status 接口） */
export interface Finding {
  clause_id: string | null
  title: string | null
  risk_level: VerdictLevel | null
  description: string | null
  channel: Channel | null
  confidence: number | null
}

export interface TaskStatusResult {
  task_id: string
  status: TaskStatus
  current: number
  total: number
  error: string | null
  summary: Summary | null
  findings: Finding[]
}

/** 逐条完整审查结果（results 接口） */
/** 文本质量问题（错别字/语义歧义/表述空泛/术语不一致/数值冲突/格式异常） */
export interface QualityIssue {
  type: string
  original: string
  issue: string
  suggestion: string
  severity: '高' | '中' | '低'
}

export interface ReviewItem {
  clause_order: number | null
  clause_id: string | null
  title: string | null
  content: string | null
  char_index?: number | null
  risk_level: VerdictLevel | null
  description: string | null
  law_basis: string | null
  suggestion: string | null
  confidence: number | null
  channel: Channel | null
  page: number | null
  quality_issues?: QualityIssue[]
  reasonableness_issues?: ReasonablenessIssue[]
  /** 模型改写后的完整条款文本（修订对照报告数据源） */
  revised_text?: string | null
  /** 处置状态与备注（处置闭环） */
  disposition?: Disposition | null
  disposition_note?: string | null
}

/** 商业合理性问题（甲方利益视角：失衡/不对等/不可执行） */
export interface ReasonablenessIssue extends QualityIssue {
  direction?: '对甲方不利' | '对乙方不利' | '双方失衡'
}

/** 合同级合理性评估（0-100 分 + 结论 + 问题分布） */
export interface ReasonablenessSummary {
  score: number
  conclusion: string
  counts: { 高: number; 中: number; 低: number }
  total: number
}

export interface TaskResultsResult {
  task_id: string
  status: TaskStatus
  filename: string | null
  contract_name: string | null
  party_a: string | null
  party_b: string | null
  total_amount: string | null
  total_clauses: number
  error: string | null
  summary: Summary
  /** 识别出的合同类型（缺失条款检查按类型取必备清单） */
  contract_type?: string | null
  /** 任务级完整性问题：缺失条款/数值冲突/主体不一致/期限矛盾 */
  contract_issues?: QualityIssue[]
  /** 合同级合理性评估（甲方利益视角） */
  reasonableness?: ReasonablenessSummary | null
  /** 合同全文（结果页原文对照左栏） */
  full_text?: string | null
  /** 本次审查采用的方案名称与配置快照 */
  playbook_name?: string | null
  playbook?: Partial<Playbook> | null
  /** 送审前脱敏的敏感信息处数 */
  masked_count?: number
  /** 灰度变体：baseline / canary */
  llm_variant?: string
  /** LLM token 用量与估算成本（成本核算：报告页展示"本次消耗 X tokens ≈ ¥Y"） */
  token_usage?: {
    prompt_tokens: number
    completion_tokens: number
    total_tokens: number
    estimated_cost_cny: number
  } | null
  /** 处置状态分布（未处理/已采纳/已修改/接受风险/已忽略） */
  disposition_summary?: Record<string, number> | null
  items: ReviewItem[]
}

// ---------------- 缺失条款草案 / 版本对比 ----------------
export interface DraftClauseResult {
  name: string
  draft: string
  source: 'llm' | 'template'
  suggestion: string
}

export type VersionChangeStatus = 'fixed' | 'remaining' | 'new' | 'added' | 'removed'

export interface VersionCompareChange {
  clause_id: string | null
  title: string | null
  status: VersionChangeStatus
  base_risk: VerdictLevel | null
  target_risk: VerdictLevel | null
  base_issue: string
  target_issue: string
  base_suggestion: string
  target_suggestion: string
}

export interface VersionCompareSummary {
  fixed: number
  remaining: number
  new: number
  added: number
  removed: number
  unchanged: number
}

export interface VersionBrief {
  task_id: string
  contract_name: string | null
  created_at: string | null
  playbook_name: string | null
  summary: Summary | null
  status: TaskStatus
}

export interface VersionCompareResult {
  base: VersionBrief
  target: VersionBrief
  summary: VersionCompareSummary
  changes: VersionCompareChange[]
  total_changes: number
}

// ---------------- 历史 ----------------
export interface HistoryTask {
  task_id: string
  filename: string | null
  contract_name: string | null
  party_a: string | null
  party_b: string | null
  total_amount: string | null
  total_clauses: number
  status: TaskStatus
  current: number
  summary: Summary | null
  /** 审查方案名称（历史记录直接展示） */
  playbook_name?: string | null
  created_at: string
  finished_at: string | null
}

export interface HistoryResult {
  total: number
  items: HistoryTask[]
}

// ---------------- 风控规则 ----------------
export interface RiskRule {
  id: number
  name: string
  category: string | null
  matcher_kind: string
  params_json: string | null
  risk_level: RiskLevel
  description: string | null
  suggestion: string | null
  enabled: number
  updated_at: string | null
}

export interface RulesResult {
  items: RiskRule[]
}

/** 规则可编辑字段（PUT /rules/{id}） */
export interface RulePatch {
  name?: string
  category?: string
  risk_level?: RiskLevel
  description?: string
  suggestion?: string
  enabled?: boolean
}

export type MatcherKind = 'percent_ceiling' | 'ip_rights' | 'keyword_any'

export interface RuleCreatePayload {
  name: string
  category?: string
  matcher_kind: MatcherKind
  params?: Record<string, unknown>
  risk_level?: RiskLevel
  description?: string
  suggestion?: string
  enabled?: boolean
}

// ---------------- 结构化日志 / trace（可观测性）----------------
/** 一条结构化日志事件（来自 logs/app.jsonl，字段随 event 类型变化） */
export interface TraceEvent {
  ts: string
  level: string
  logger: string
  event: string
  trace_id?: string
  clause_order?: number
  clause_id?: string
  channel?: string
  provider?: string
  risk_level?: string
  latency_ms?: number
  total_tokens?: number
  error?: string
  [key: string]: unknown
}
export interface TaskTraceResult {
  trace_id: string
  summary: {
    total_events: number
    fallbacks: number
    parse_failed: number
    provider_failures: number
    slowest_clause: {
      clause_id?: string
      clause_order?: number
      latency_ms: number
      channel?: string
      provider?: string
    } | null
  }
  events: TraceEvent[]
}

// ---------------- 学习闭环（越用越聪明）----------------/** 由历史"已采纳/已修改"归纳出的一条规则固化建议 */
export interface LearningSuggestion {
  topic: string
  kind: 'percent' | 'days'
  value: number
  support: number
  message: string
  suggested_rule: RuleCreatePayload
}
export interface LearningInsights {
  /** 各处置状态的累计条数，如 { 已采纳: 15, 已修改: 2 } */
  sample_pool: Record<string, number>
  total_samples: number
  suggestions: LearningSuggestion[]
}

// ---------------- 健康 / 知识库 ----------------
export interface HealthResult {
  status: string
  llm_provider: 'vllm' | 'siliconflow' | 'custom' | 'none'
  llm_model?: string | null
  custom_active?: boolean
  kb_ready: boolean
}

// ---------------- 灰度发布（canary）----------------
/** 灰度配置 + 运行时状态（GET/PUT /llm/canary） */
export interface CanaryConfig {
  enabled: boolean
  percent: number
  base_url: string
  api_key: string
  model: string
  status: { active: boolean; percent: number; circuit_open: boolean }
}

/** 灰度保存载荷 */
export interface CanaryConfigPayload {
  enabled: boolean
  percent: number
  base_url: string
  model: string
  api_key?: string
}

/** 单个变体的 A/B 指标 */
export interface CanaryVariantMetrics {
  variant: string
  tasks: number
  completed: number
  failed: number
  avg_seconds: number | null
  tokens: number
  risk: Record<string, number>
}

/** 灰度指标（GET /metrics/canary） */
export interface CanaryMetrics {
  variants: CanaryVariantMetrics[]
  status: { active: boolean; percent: number; circuit_open: boolean }
}

// ---------------- 审计留痕 + 版本管理 ----------------
/** 单条审计事件（append-only） */
export interface AuditLogItem {
  id: number
  created_at: string
  actor: string
  action: string
  target_type: string
  target_id: string
  task_id: string
  detail: Record<string, unknown>
}

export interface AuditLogResult {
  total: number
  items: AuditLogItem[]
  actions: Record<string, number>
}

/** 合同族中的一个版本 */
export interface TaskVersionItem {
  task_id: string
  contract_name: string | null
  version: number
  parent_task_id: string | null
  status: string
  total_clauses: number
  summary: Record<string, number>
  created_at: string
  finished_at: string | null
  is_current: boolean
}

export interface TaskVersionsResult {
  root_task_id: string
  versions: TaskVersionItem[]
}

// ---------------- 运行时自定义 LLM（模型配置页）----------------
/** 保存的运行时自定义配置（GET /llm/config 返回；api_key 为明文，供回显编辑） */
export interface LlmConfig {
  enabled: boolean
  base_url: string
  /** 明文 Key：后端回显，前端可编辑；空串表示未配置 */
  api_key: string
  api_key_set: boolean
  model: string
  updated_at: string | null
  /** 进程内是否已注入生效 */
  applied: boolean
}

/** 保存载荷（PUT /llm/config）——所见即所得：api_key 框内值即最终值，空串 = 清除 */
export interface LlmConfigPayload {
  enabled: boolean
  base_url: string
  model: string
  api_key?: string
}

/** 连通性测试载荷（POST /llm/test，不落库） */
export interface LlmProbePayload {
  base_url: string
  model: string
  api_key?: string
}

/** 连通性测试结果 */
export interface LlmProbeResult {
  ok: boolean
  latency_ms: number | null
  reply: string | null
  error: string | null
}

export interface KbStats {
  total: number
  sources: Record<string, number> | null
  error?: string
}

// ---------------- 知识库管理（知识库页）----------------
/** 知识库分类（后端与前端共享的枚举） */
export type KbCategory = 'law' | 'judicial' | 'case' | 'upload'

/** 文档列表项：按 (category, source) 聚合为一个"文件" */
export interface KbDocument {
  category: KbCategory | string
  source: string
  name: string
  file_type: string
  chunk_count: number
  snippet: string
}

export interface KbDocumentsResult {
  documents: KbDocument[]
}

/** 文档详情：单个文件的全部分块 */
export interface KbChunk {
  id: string
  content: string
  metadata: Record<string, unknown>
}

export interface KbDocumentDetail {
  source: string
  category: string
  chunks: KbChunk[]
}

/** 上传语料结果 */
export interface KbUploadResult {
  source: string
  category: string
  name: string
  added: number
  chunks: number
}

/** 语义检索命中 */
export interface KbSearchHit {
  content: string
  source: string
  category: KbCategory | string
  score: number
}

export interface KbSearchResult {
  query: string
  hits: KbSearchHit[]
}

/** 重建索引结果（POST /knowledge/index） */
export interface KbIndexResult {
  added: number
  total: number
  sources?: number
  message?: string
}

// ---------------- Prompt 预览 + 追问（Demo 亮点）----------------
/** Prompt 预览：重建指定条款在审查时发给 LLM 的完整 messages */
export interface PromptPreviewResult {
  clause_order: number
  clause_id: string | null
  clause_content: string | null
  /** 该条款实际走的通道（哪个通道有命中） */
  channel_hit: 'rule' | 'llm'
  stance: string
  law_context: string
  rules: Array<{ name: string; risk_level: string; description: string }>
  /** 规则命中时的 quality_check 通道 messages */
  rule_channel: Array<{ role: string; content: string }> | null
  /** LLM 主审查通道 messages */
  llm_channel: Array<{ role: string; content: string }> | null
}

/** 对话式追问结果 */
export interface ClauseAskResult {
  clause_order: number
  question: string
  answer: string
  provider: string
}

/** 合并采纳的改写 → 修订后合同 */
export interface ApplyRevisionsPayload {
  auto?: boolean
  clause_orders?: number[]
  revise_party_a?: string
  revise_party_b?: string
  revise_total_amount?: string
  start_new_review?: boolean
  append_drafts?: string[]
}
export interface ApplyRevisionsResult {
  task_id: string
  revised_text: string
  replacements: number
  applied_clause_orders: number[]
  appended_drafts: number
  /** 修订后全文一致性校验（编号重复/断层、交叉引用悬空） */
  consistency_issues?: QualityIssue[]
  meta: { party_a?: string; party_b?: string; total_amount?: string }
  new_task_id?: string
  new_total_clauses?: number
  note?: string
}
