// =============================================================================
// 展示映射工具 - 风险等级 / 判断渠道的文案与 Element 组件类型映射
// =============================================================================
import type { Channel, RiskLevel, TaskStatus, VerdictLevel } from '../api/types'

/** 风险等级 → el-tag / el-progress 类型 */
export const RISK_TAG_TYPE: Record<VerdictLevel, 'danger' | 'warning' | 'success' | 'info'> = {
  高: 'danger',
  中: 'warning',
  低: 'info',
  无: 'success',
}

/** 风险等级排序权重（用于列表按严重程度排序） */
export const RISK_WEIGHT: Record<VerdictLevel, number> = { 高: 3, 中: 2, 低: 1, 无: 0 }

/** 判定渠道文案 */
export const CHANNEL_LABEL: Record<Channel, string> = {
  rule: '规则命中',
  llm: '模型判定',
  fallback: '降级提示',
}

/** 判定渠道 → tag 类型 */
export const CHANNEL_TAG_TYPE: Record<Channel, 'primary' | 'warning' | 'info'> = {
  rule: 'warning',
  llm: 'primary',
  fallback: 'info',
}

/** 任务状态文案与 tag 类型 */
export const STATUS_META: Record<
  TaskStatus,
  { label: string; type: 'primary' | 'success' | 'danger' | 'info' | 'warning' }
> = {
  pending: { label: '排队中', type: 'info' },
  running: { label: '审查中', type: 'primary' },
  completed: { label: '已完成', type: 'success' },
  failed: { label: '失败', type: 'danger' },
}

/** 按严重程度降序排序（未知等级视为"无"） */
export function sortByRisk<T extends { risk_level: VerdictLevel | null }>(items: T[]): T[] {
  return [...items].sort(
    (a, b) => RISK_WEIGHT[(b.risk_level ?? '无') as VerdictLevel] - RISK_WEIGHT[(a.risk_level ?? '无') as VerdictLevel]
  )
}

/** 进度百分比（0 条款时按 100 处理） */
export function progressPercent(current: number, total: number): number {
  if (!total) return 100
  return Math.min(100, Math.round((current / total) * 100))
}

export type { RiskLevel }
