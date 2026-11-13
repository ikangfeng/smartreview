<script setup lang="ts">
// =============================================================================
// 历史记录页 - 审查任务列表（分页）
// =============================================================================
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { deleteHistoryTask, fetchHistory } from '../api'
import type { HistoryTask } from '../api/types'
import { RISK_TAG_TYPE, STATUS_META } from '../utils/risk'

const router = useRouter()
const rows = ref<HistoryTask[]>([])
const total = ref(0)
const loading = ref(false)
const page = ref(1)
const pageSize = 10

async function load() {
  loading.value = true
  try {
    const data = await fetchHistory(pageSize, (page.value - 1) * pageSize)
    rows.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

onMounted(load)

function openTask(row: HistoryTask) {
  const target = row.status === 'running' || row.status === 'pending' ? 'review' : 'result'
  router.push({ name: target, params: { taskId: row.task_id } })
}

function progressText(row: HistoryTask): string {
  if (row.status === 'completed') return `${row.total_clauses} 条`
  if (row.status === 'running') return `${row.current}/${row.total_clauses}`
  return `${row.total_clauses} 条`
}

/** 是否允许删除（执行中任务由后端 409 保护，前端直接隐藏入口） */
function canDelete(row: HistoryTask): boolean {
  return row.status !== 'pending' && row.status !== 'running'
}

async function removeTask(row: HistoryTask) {
  const name = row.contract_name || row.filename || '未命名合同'
  try {
    await ElMessageBox.confirm(
      `将删除「${name}」的审查记录与逐条结果，同时清理其报告文件。此操作不可恢复，确认删除？`,
      '删除历史记录',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return // 用户取消
  }
  try {
    const res = await deleteHistoryTask(row.task_id)
    ElMessage.success(
      res.removed_files > 0 ? `已删除记录，并清理 ${res.removed_files} 个文件` : '已删除记录',
    )
    // 删除当前页最后一条时回退一页
    if (rows.value.length === 1 && page.value > 1) page.value -= 1
    await load()
  } catch {
    // 错误已由拦截器提示
  }
}
</script>

<template>
  <div class="page">
    <div class="sr-card">
      <div class="sr-card-title">
        历史审查记录
        <el-button link type="primary" @click="load">
          <el-icon style="margin-right: 4px"><Refresh /></el-icon>刷新
        </el-button>
      </div>

      <el-table v-loading="loading" :data="rows" stripe>
        <el-table-column label="合同" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="contract-name">{{ row.contract_name || row.filename || '未命名合同' }}</div>
            <div class="file-name text-muted">{{ row.filename }}</div>
          </template>
        </el-table-column>

        <el-table-column label="相对方" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">
            <template v-if="row.party_a || row.party_b">
              {{ row.party_a || '—' }} ⇄ {{ row.party_b || '—' }}
            </template>
            <span v-else class="text-muted">—</span>
          </template>
        </el-table-column>

        <el-table-column label="金额" min-width="100" show-overflow-tooltip>
          <template #default="{ row }">{{ row.total_amount || '—' }}</template>
        </el-table-column>

        <el-table-column label="审查方案" min-width="170" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.playbook_name">{{ row.playbook_name }}</span>
            <span v-else class="text-muted">—</span>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="STATUS_META[row.status as keyof typeof STATUS_META].type" size="small">
              {{ STATUS_META[row.status as keyof typeof STATUS_META].label }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="风险概况" min-width="140">
          <template #default="{ row }">
            <div v-if="row.summary" class="risk-line">
              <template v-for="lv in (['高', '中', '低'] as const)" :key="lv">
                <el-tag v-if="row.summary[lv] > 0" size="small" :type="RISK_TAG_TYPE[lv]">
                  {{ lv }}×{{ row.summary[lv] }}
                </el-tag>
              </template>
              <span
                v-if="row.summary['高'] === 0 && row.summary['中'] === 0 && row.summary['低'] === 0"
                class="text-muted"
              >
                无风险
              </span>
            </div>
            <span v-else class="text-muted">—</span>
          </template>
        </el-table-column>

        <el-table-column label="进度" width="110">
          <template #default="{ row }">{{ progressText(row) }}</template>
        </el-table-column>

        <el-table-column label="创建时间" width="160">
          <template #default="{ row }">
            <span class="mono small-text">{{ row.created_at }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openTask(row)">查看</el-button>
            <el-button
              v-if="canDelete(row)"
              link
              type="danger"
              @click="removeTask(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="暂无审查记录，去上传一份合同吧" />
        </template>
      </el-table>

      <div class="pager">
        <el-pagination
          layout="total, prev, pager, next"
          :total="total"
          :page-size="pageSize"
          :current-page="page"
          @current-change="(p: number) => { page = p; load() }"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.contract-name {
  font-weight: 600;
}

.file-name {
  font-size: 12px;
}

.risk-line {
  display: flex;
  gap: 6px;
}

.small-text {
  font-size: 12px;
}

.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
</style>
