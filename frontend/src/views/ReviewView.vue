<script setup lang="ts">
// =============================================================================
// 审查进度页 - 1s 轮询，增量 findings 实时流式展示
// =============================================================================
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { fetchTaskStatus } from '../api'
import type { Finding, TaskStatusResult } from '../api/types'
import { RISK_TAG_TYPE, progressPercent } from '../utils/risk'

const props = defineProps<{ taskId: string }>()
const router = useRouter()

const status = ref<TaskStatusResult | null>(null)
const loading = ref(true)
const error = ref('')

const percent = computed(() =>
  progressPercent(status.value?.current ?? 0, status.value?.total ?? 0)
)

const riskCount = computed(() => {
  const findings = status.value?.findings ?? []
  return {
    高: findings.filter((f) => f.risk_level === '高').length,
    中: findings.filter((f) => f.risk_level === '中').length,
    低: findings.filter((f) => f.risk_level === '低').length,
  }
})

// 通道计数（双通道展示 —— 面试 Demo 亮点）
const channelCount = computed(() => {
  const findings = status.value?.findings ?? []
  const c: Record<string, number> = { rule: 0, llm: 0, fallback: 0 }
  for (const f of findings) {
    if (f.channel) c[f.channel] = (c[f.channel] ?? 0) + 1
  }
  return c
})

// 全量 findings（按到达顺序流式展示）
const allFindings = computed<Finding[]>(() => status.value?.findings ?? [])

// 上一次的 findings 数量 —— 用来高亮新增的
const prevCount = ref(0)
watch(allFindings, (val) => {
  prevCount.value = val.length
})

const CHANNEL_LABEL: Record<string, string> = {
  rule: '规则通道',
  llm: 'LLM 通道',
  fallback: '降级兜底',
}
const CHANNEL_TYPE: Record<string, string> = {
  rule: 'danger',
  llm: 'primary',
  fallback: 'info',
}

async function poll() {
  try {
    const data = await fetchTaskStatus(props.taskId)
    status.value = data
    loading.value = false
    if (data.status === 'completed') {
      router.replace({ name: 'result', params: { taskId: props.taskId } })
    } else if (data.status === 'failed') {
      error.value = data.error ?? '审查任务执行失败，请稍后重试'
      stopPoll()
    }
  } catch {
    loading.value = false
    error.value = error.value || '无法连接后端，请确认服务已启动'
    stopPoll()
  }
}

let timer: number | undefined
function startPoll() {
  poll()
  timer = window.setInterval(poll, 1000) // 1s 轮询（更快的流式体验）
}
function stopPoll() {
  if (timer !== undefined) {
    window.clearInterval(timer)
    timer = undefined
  }
}

function retry() {
  error.value = ''
  loading.value = true
  startPoll()
}

onMounted(startPoll)
onUnmounted(stopPoll)
</script>

<template>
  <div class="page">
    <div class="sr-card stream-card">
      <div class="sr-card-title">
        审查进行中
        <span class="stream-badge" v-if="status?.status === 'running'">
          <span class="pulse-dot"></span>
          并行执行中
        </span>
      </div>

      <!-- 失败态 -->
      <el-result
        v-if="error"
        icon="error"
        title="审查任务异常"
        :sub-title="error"
      >
        <template #extra>
          <el-button type="primary" @click="retry">重新连接</el-button>
          <el-button @click="router.push({ name: 'home' })">返回首页</el-button>
        </template>
      </el-result>

      <!-- 轮询态 -->
      <template v-else>
        <div class="task-line text-muted mono">任务号：{{ taskId }}</div>
        <el-progress
          :percentage="percent"
          :status="percent >= 100 ? 'success' : undefined"
          :stroke-width="16"
          striped
          striped-flow
          class="progress"
        />
        <div class="progress-text">
          <span v-if="status?.status === 'running' || status?.status === 'pending'">
            正在审查第 <b class="current-num">{{ status?.current ?? 0 }}</b> / <b>{{ status?.total ?? 0 }}</b> 条条款
          </span>
          <span v-else-if="loading">正在建立审查任务…</span>
          <span v-else-if="status?.status === 'completed'">全部完成，跳转结果页…</span>
        </div>

        <!-- 双通道统计（面试 Demo 亮点） -->
        <div class="channel-stats" v-if="allFindings.length > 0">
          <div class="channel-stat">
            <div class="channel-num">{{ channelCount.rule }}</div>
            <div class="channel-label">规则通道（确定性）</div>
          </div>
          <div class="channel-stat">
            <div class="channel-num llm">{{ channelCount.llm }}</div>
            <div class="channel-label">LLM 通道（语义判断）</div>
          </div>
          <div class="channel-stat-tip" v-if="channelCount.rule > 0 && channelCount.llm > 0">
            ThreadPoolExecutor 并发执行，每条审查完成即落库展示
          </div>
        </div>

        <!-- 实时发现流（全量，逐条淡入） -->
        <div class="found" v-if="allFindings.length > 0">
          <div class="found-title">已发现风险（{{ allFindings.length }}）</div>
          <div class="found-counts">
            <el-tag v-for="lv in (['高', '中', '低'] as const)" :key="lv" :type="RISK_TAG_TYPE[lv]" size="small">
              {{ lv }}风险 {{ riskCount[lv] }}
            </el-tag>
          </div>
          <TransitionGroup name="stream" tag="ul" class="found-list">
            <li v-for="(f, i) in allFindings" :key="`${f.clause_id}-${i}`">
              <el-tag :type="RISK_TAG_TYPE[(f.risk_level ?? '无') as '高' | '中' | '低' | '无']" size="small" effect="dark">
                {{ f.risk_level ?? '无' }}
              </el-tag>
              <el-tag :type="(CHANNEL_TYPE[f.channel ?? 'llm'] || 'info') as any" size="small" effect="plain" class="channel-tag">
                {{ CHANNEL_LABEL[f.channel ?? ''] || f.channel || '—' }}
              </el-tag>
              <span class="found-clause mono">{{ f.clause_id ?? '—' }}</span>
              <span class="found-text">{{ f.title || f.description || '未知条款' }}</span>
            </li>
          </TransitionGroup>
        </div>
        <div v-else-if="status?.status === 'running'" class="waiting">
          <el-icon class="is-loading"><Loading /></el-icon>
          首批条款正在审查中…
        </div>

        <el-alert
          class="tip"
          type="info"
          :closable="false"
          title="后台并发执行，每条完成即展示；可离开本页面稍后从「历史记录」查看结果"
        />
      </template>
    </div>
  </div>
</template>

<script lang="ts">
import { Loading } from '@element-plus/icons-vue'
export default { components: { Loading } }
</script>

<style scoped>
.stream-card { position: relative; }

.stream-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: normal;
  color: #409eff;
  margin-left: 10px;
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

.task-line { font-size: 13px; margin-bottom: 18px; }

.progress { margin-bottom: 10px; }

.progress-text {
  text-align: center;
  font-size: 14px;
  color: #3a3f45;
}
.current-num { color: #409eff; font-size: 18px; }

.channel-stats {
  display: flex;
  gap: 24px;
  margin: 20px 0 0;
  padding: 14px 18px;
  background: linear-gradient(135deg, #f0f7ff 0%, #fafbfc 100%);
  border-radius: 10px;
  border: 1px solid #e4e7ed;
}
.channel-stat { text-align: center; }
.channel-num {
  font-size: 28px; font-weight: 700; color: #f56c6c;
  line-height: 1;
}
.channel-num.llm { color: #409eff; }
.channel-label { font-size: 12px; color: #909399; margin-top: 4px; }
.channel-stat-tip {
  flex: 1;
  align-self: center;
  text-align: right;
  font-size: 12px;
  color: #606266;
  line-height: 1.5;
}

.found { margin-top: 24px; }
.found-title { font-size: 15px; font-weight: 600; margin-bottom: 12px; }
.found-counts { display: flex; gap: 10px; margin-bottom: 12px; }

.found-list {
  list-style: none;
  margin: 0; padding: 0;
  display: flex; flex-direction: column; gap: 8px;
  max-height: 380px; overflow-y: auto;
}
.found-list li {
  display: flex; align-items: center; gap: 8px;
  font-size: 13px; padding: 8px 10px;
  background: #fafbfc; border-radius: 6px;
  border: 1px solid #ebeef5;
}
.channel-tag { margin-left: auto; }
.found-clause {
  color: #909399; font-size: 12px; min-width: 42px;
}
.found-text {
  color: #4c5157;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  flex: 1;
}

/* 淡入动画 */
.stream-enter-active { transition: all 0.4s ease-out; }
.stream-enter-from { opacity: 0; transform: translateY(-8px); }
.stream-move { transition: transform 0.4s ease-out; }

.waiting {
  text-align: center;
  color: #909399;
  padding: 30px 0;
  display: flex; align-items: center; justify-content: center;
  gap: 8px; font-size: 13px;
}

.tip { margin-top: 24px; }
</style>
