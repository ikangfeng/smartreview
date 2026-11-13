<script setup lang="ts">
// =============================================================================
// 审计日志页 - 全局操作留痕（谁 / 何时 / 对什么 / 改了什么）
// =============================================================================
import { onMounted, ref } from 'vue'
import { fetchAuditLogs } from '../api'
import type { AuditLogItem } from '../api/types'

const items = ref<AuditLogItem[]>([])
const total = ref(0)
const actions = ref<Record<string, number>>({})
const loading = ref(false)
const filterAction = ref('')
const filterType = ref('')
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
const TYPE_LABEL: Record<string, string> = {
  task: '审查任务',
  rule: '风控规则',
  playbook: '审查方案',
  llm: '模型配置',
}

function actionLabel(a: string) {
  return ACTION_LABEL[a] || a
}

function detailText(detail: Record<string, unknown>) {
  const d = detail || {}
  const parts: string[] = []
  if (d.clause_order != null) parts.push(`第 ${d.clause_order} 条`)
  if (d.from != null || d.to != null) parts.push(`${d.from ?? '—'} → ${d.to ?? '—'}`)
  if (d.name) parts.push(String(d.name))
  if (Array.isArray(d.clause_orders) && d.clause_orders.length) parts.push(`合并 ${d.clause_orders.length} 条改写`)
  if (d.contract_type) parts.push(String(d.contract_type))
  if (d.report) parts.push(`报告类型：${d.report}`)
  if (d.percent != null) parts.push(`分流 ${d.percent}%`)
  return parts.join(' · ') || '—'
}

async function load() {
  loading.value = true
  try {
    const res = await fetchAuditLogs({
      limit: 200,
      action: filterAction.value,
      target_type: filterType.value,
    })
    items.value = res.items ?? []
    total.value = res.total ?? 0
    actions.value = res.actions ?? {}
  } finally {
    loading.value = false
  }
}

function saveOperator() {
  const name = (operator.value || '').trim() || '演示用户'
  operator.value = name
  localStorage.setItem('sr_operator', name)
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <div class="page-title">审计日志</div>
        <div class="page-sub">
          所有改变合同结论、处置口径、规则与模型配置的动作都会留痕；日志只追加、不覆盖。
        </div>
      </div>
      <div class="operator-box">
        <span class="operator-cap">当前操作人</span>
        <el-input v-model="operator" size="small" style="width: 150px" @blur="saveOperator" />
        <el-tag size="small" type="info">共 {{ total }} 条</el-tag>
      </div>
    </div>

    <div class="sr-card">
      <div class="filters">
        <el-select v-model="filterAction" placeholder="全部动作" clearable style="width: 190px" @change="load">
          <el-option v-for="(n, k) in actions" :key="k" :label="`${actionLabel(k)} (${n})`" :value="k" />
        </el-select>
        <el-select v-model="filterType" placeholder="全部对象" clearable style="width: 150px" @change="load">
          <el-option v-for="(label, k) in TYPE_LABEL" :key="k" :label="label" :value="k" />
        </el-select>
        <el-button @click="load">刷新</el-button>
      </div>

      <el-table v-loading="loading" :data="items" size="small" border>
        <el-table-column prop="created_at" label="时间" width="175">
          <template #default="{ row }">{{ row.created_at.replace('T', ' ') }}</template>
        </el-table-column>
        <el-table-column prop="actor" label="操作人" width="110" />
        <el-table-column label="动作" width="150">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ actionLabel(row.action) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="对象" width="130">
          <template #default="{ row }">
            {{ TYPE_LABEL[row.target_type] || row.target_type }}
            <span v-if="row.target_id" class="target-id">{{ String(row.target_id).slice(-8) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="明细">
          <template #default="{ row }">{{ detailText(row.detail) }}</template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<style scoped>
.page {
  max-width: 1380px;
  margin: 0 auto;
}
.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.page-title {
  font-size: 22px;
  font-weight: 700;
}
.page-sub {
  margin-top: 6px;
  font-size: 13px;
  color: #909399;
}
.operator-box {
  display: flex;
  align-items: center;
  gap: 8px;
}
.operator-cap {
  font-size: 13px;
  color: #606266;
}
.filters {
  display: flex;
  gap: 10px;
  margin-bottom: 14px;
}
.target-id {
  margin-left: 4px;
  color: #c0c4cc;
  font-size: 12px;
}
</style>
