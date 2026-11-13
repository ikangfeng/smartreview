<script setup lang="ts">
// =============================================================================
// 首页 - 上传合同并开始审查
// =============================================================================
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type UploadFile, type UploadInstance, type UploadRawFile } from 'element-plus'
import { fetchHealth, fetchPlaybooks, startReview, uploadContract } from '../api'
import type { HealthResult, Playbook, UploadResult } from '../api/types'

const router = useRouter()
const uploadRef = ref<UploadInstance>()
const health = ref<HealthResult | null>(null)
const healthOk = ref<boolean | null>(null)
const uploadInfo = ref<UploadResult | null>(null)
const uploading = ref(false)
const starting = ref(false)

// 审查方案：0 = 自动匹配（后端按识别出的合同类型选默认方案）
const playbooks = ref<Playbook[]>([])
const playbookId = ref<number>(0)
const currentPlaybook = computed(() =>
  playbooks.value.find((p) => p.id === playbookId.value) || null
)

// 审查前人工确认：合同类型与立场（避免类型猜错/立场写死导致系统性误判）
const contractType = ref('通用合同')
const contractTypes = ref<string[]>([
  '技术服务/开发合同', '采购/买卖合同', '租赁合同', '保密协议', '劳动/劳务合同', '通用合同',
])
const stance = ref('甲方')

// 选择方案时同步其立场（用户仍可手动改）；切换类型时回到自动匹配
watch(playbookId, () => {
  if (currentPlaybook.value) stance.value = currentPlaybook.value.stance
})

function onTypeChange() {
  playbookId.value = 0
}

const MAX_MB = 20
const ACCEPT = '.pdf,.docx,.txt'

const providerLabel: Record<string, string> = {
  vllm: '本地 vLLM',
  siliconflow: '云端 SiliconFlow',
  none: '未配置（降级为规则通道）',
}

const kbLabel: Record<string, string> = {
  law: '法律法规',
  judicial: '司法解释',
  case: '指导案例',
  upload: '上传文档',
}

async function loadHealth() {
  try {
    health.value = await fetchHealth()
    healthOk.value = true
  } catch {
    healthOk.value = false
  }
}

async function loadPlaybooks() {
  try {
    const data = await fetchPlaybooks(true)
    playbooks.value = data.items
  } catch {
    playbooks.value = []
  }
}

onMounted(() => {
  loadHealth()
  loadPlaybooks()
})

// 校验文件类型与大小
function beforeUpload(raw: UploadRawFile): boolean {
  const ext = (raw.name.split('.').pop() ?? '').toLowerCase()
  if (!['pdf', 'docx', 'txt'].includes(ext)) {
    ElMessage.warning('仅支持 PDF / DOCX / TXT 格式')
    return false
  }
  if (raw.size > MAX_MB * 1024 * 1024) {
    ElMessage.warning(`文件不能超过 ${MAX_MB}MB`)
    return false
  }
  return true
}

// 选择文件后立即上传解析，拿到页数/预估条款数
async function handleChange(file: UploadFile) {
  const raw = file.raw
  if (!raw) return
  uploading.value = true
  try {
    uploadInfo.value = await uploadContract(raw)
    // 解析即带出自动识别的合同类型，供用户确认/修正（可覆盖）
    contractType.value = uploadInfo.value.detected_contract_type || '通用合同'
    if (uploadInfo.value.contract_types?.length) {
      contractTypes.value = uploadInfo.value.contract_types
    }
    playbookId.value = 0
    ElMessage.success(`解析成功：约 ${uploadInfo.value.estimate_clauses} 条条款`)
  } catch {
    uploadInfo.value = null
  } finally {
    uploading.value = false
    // 清空组件内部文件列表，避免再次选中同文件不触发 on-change
    uploadRef.value?.clearFiles()
  }
}

function resetFile() {
  uploadInfo.value = null
  contractType.value = '通用合同'
  playbookId.value = 0
}

async function handleStart() {
  if (!uploadInfo.value) return
  starting.value = true
  try {
    const { task_id } = await startReview(uploadInfo.value.file_id, {
      playbookId: playbookId.value || undefined,
      contractType: contractType.value,
      stance: stance.value,
    })
    router.push({ name: 'review', params: { taskId: task_id } })
  } catch {
    // 错误已由拦截器提示
  } finally {
    starting.value = false
  }
}
</script>

<template>
  <div class="page">
    <!-- 系统状态横幅 -->
    <div class="sr-card status-card">
      <div class="sr-card-title">
        系统状态
        <el-button link type="primary" @click="loadHealth">
          <el-icon style="margin-right: 4px"><Refresh /></el-icon>刷新
        </el-button>
      </div>
      <div v-if="healthOk === false" class="status-item">
        <el-tag type="danger" effect="plain">后端不可用</el-tag>
        <span class="text-muted">请确认 backend 服务已启动（scripts/start.bat）</span>
      </div>
      <div v-else-if="health" class="status-row">
        <el-tag type="success" effect="plain">后端服务正常</el-tag>
        <el-tag :type="health.llm_provider === 'none' ? 'warning' : 'primary'" effect="plain">
          LLM：{{ providerLabel[health.llm_provider] ?? health.llm_provider }}
        </el-tag>
        <el-tag :type="health.kb_ready ? 'success' : 'warning'" effect="plain">
          {{ health.kb_ready ? '法律知识库就绪' : '法律知识库为空' }}
        </el-tag>
      </div>
      <div v-else class="status-item text-muted">正在检测后端状态…</div>
    </div>

    <!-- 审查方案：按合同类型自动匹配默认方案，也可手动指定 -->
    <div class="sr-card">
      <div class="sr-card-title">
        审查方案
        <span class="text-muted small">决定必备条款清单、参与规则、知识库范围与审查立场</span>
      </div>
      <el-select v-model="playbookId" class="pb-select" placeholder="自动匹配">
        <el-option :value="0" label="自动匹配（按识别出的合同类型）" />
        <el-option
          v-for="p in playbooks"
          :key="p.id"
          :value="p.id"
          :label="`${p.name}（${p.contract_type || '通用'} · ${p.stance}）`"
        />
      </el-select>
      <div v-if="currentPlaybook" class="pb-detail">
        <p class="pb-desc">{{ currentPlaybook.description || '暂无方案说明' }}</p>
        <div class="pb-tags">
          <el-tag size="small" type="primary" effect="plain">{{ currentPlaybook.stance }}立场</el-tag>
          <el-tag size="small" effect="plain">
            必备条款 {{ currentPlaybook.required_clauses.length }} 项
          </el-tag>
          <el-tag size="small" effect="plain">
            {{ currentPlaybook.rule_ids.length ? `规则 ${currentPlaybook.rule_ids.length} 条` : '全部启用规则' }}
          </el-tag>
          <el-tag v-if="currentPlaybook.kb_categories.length" size="small" effect="plain">
            知识库：{{ currentPlaybook.kb_categories.map((c) => kbLabel[c] || c).join('/') }}
          </el-tag>
          <el-tag v-else size="small" effect="plain">知识库：不限</el-tag>
        </div>
        <p v-if="currentPlaybook.extra_prompt" class="pb-extra">
          附加要求：{{ currentPlaybook.extra_prompt }}
        </p>
      </div>
      <div v-else class="text-muted small pb-auto-tip">
        未手动指定时，系统将按合同中识别出的类型自动套用对应默认方案；无匹配方案则使用通用方案。
      </div>
    </div>

    <!-- 上传区 -->
    <div class="sr-card">
      <div class="sr-card-title">
        上传合同
        <span class="text-muted small">支持 PDF / DOCX / TXT，≤ {{ MAX_MB }}MB</span>
      </div>

      <el-upload
        ref="uploadRef"
        drag
        :accept="ACCEPT"
        :auto-upload="false"
        :show-file-list="false"
        :before-upload="beforeUpload"
        :on-change="handleChange"
      >
        <div class="upload-inner">
          <div class="upload-icon">⇪</div>
          <div class="upload-text">
            将合同文件拖到此处，或 <em>点击上传</em>
          </div>
          <div class="upload-tip">文件解析在浏览器内发起，仅暂存于后端，审查完成后自动清理</div>
        </div>
      </el-upload>

      <el-alert
        v-if="uploadInfo"
        type="success"
        :closable="false"
        class="parse-result"
        title="解析完成，请确认后开始审查"
      >
        <div class="parse-grid">
          <div class="parse-item"><span class="label">文件</span>{{ uploadInfo.filename }}</div>
          <div class="parse-item"><span class="label">页数</span>{{ uploadInfo.page_count }} 页</div>
          <div class="parse-item">
            <span class="label">预估条款</span>{{ uploadInfo.estimate_clauses }} 条
          </div>
        </div>
      </el-alert>

      <!-- 审查前确认：类型/立场错误会导致必备条款清单与规则/Prompt 系统性偏向 -->
      <div v-if="uploadInfo" class="confirm-block">
        <div class="confirm-title">
          审查前确认
          <span class="text-muted small">类型决定必备条款口径，立场决定规则与建议的偏向</span>
        </div>
        <div class="confirm-row">
          <span class="confirm-label">合同类型</span>
          <el-select v-model="contractType" class="type-select" @change="onTypeChange">
            <el-option v-for="t in contractTypes" :key="t" :value="t" :label="t" />
          </el-select>
          <el-tag v-if="uploadInfo.detected_contract_type" size="small" effect="plain">
            自动识别：{{ uploadInfo.detected_contract_type }}
          </el-tag>
        </div>
        <div class="confirm-row">
          <span class="confirm-label">我方立场</span>
          <el-radio-group v-model="stance">
            <el-radio-button value="甲方">甲方</el-radio-button>
            <el-radio-button value="乙方">乙方</el-radio-button>
            <el-radio-button value="中立">中立</el-radio-button>
          </el-radio-group>
          <span class="text-muted small">
            规则库当前按甲方口径配置；乙方/中立立场已同步调整合理性判断与提示词
          </span>
        </div>
        <el-alert
          v-if="uploadInfo.ocr_pages"
          type="success"
          :closable="false"
          class="ocr-alert"
          :title="`扫描件已启用 OCR（${uploadInfo.ocr_pages} 页）`"
          description="这些页面没有可提取的文字层，已通过 OCR 识别后参与条款拆解与审查。"
        />
      </div>
    </div>

    <div class="actions">
      <el-button size="large" @click="resetFile" :disabled="!uploadInfo">重新选择</el-button>
      <el-button
        type="primary"
        size="large"
        :loading="uploading || starting"
        :disabled="!uploadInfo"
        @click="handleStart"
      >
        {{ uploading ? '解析中…' : '开始审查' }}
      </el-button>
    </div>

    <el-alert
      v-if="health?.llm_provider === 'none'"
      type="info"
      :closable="false"
      class="degrade-tip"
      title="未配置 LLM 密钥"
      description="当前仅执行确定性风控规则判断；配置 backend/.env 中的 vLLM / SiliconFlow 后可启用逐条智能分析。"
    />
    <el-alert
      v-else-if="health?.kb_ready === false"
      type="info"
      :closable="false"
      class="degrade-tip"
      title="法律知识库为空"
      description="法条引用通道将降级（审查结论不附带法律依据）。可在「知识库」页上传法律语料，或将文件放入 backend/data/kb 后重建索引。"
    />
  </div>
</template>

<style scoped>
.status-card {
  padding: 14px 24px;
}

/* 审查方案选择 */
.pb-select {
  width: 420px;
  max-width: 100%;
}

.pb-detail {
  margin-top: 14px;
}

.pb-desc {
  margin: 0 0 10px;
  font-size: 13px;
  color: #4c5157;
}

.pb-tags {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.pb-extra {
  margin: 10px 0 0;
  font-size: 12px;
  color: #8a919f;
}

.pb-auto-tip {
  margin-top: 12px;
}

/* 审查前确认（合同类型 + 立场） */
.confirm-block {
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px dashed #e6e8ee;
}

.confirm-title {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 12px;
}

.confirm-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.confirm-label {
  min-width: 64px;
  color: #8a919f;
  font-size: 13px;
}

.type-select {
  width: 220px;
}

.ocr-alert {
  margin-top: 6px;
}

.status-row {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: center;
}

.status-item {
  display: flex;
  gap: 12px;
  align-items: center;
}

.upload-inner {
  padding: 18px 0 6px;
}

.upload-icon {
  font-size: 40px;
  line-height: 1;
  color: var(--sr-brand-light);
}

.upload-text {
  margin-top: 12px;
  font-size: 15px;
  color: #3a3f45;
}

.upload-text em {
  color: var(--sr-brand);
  font-style: normal;
  font-weight: 600;
}

.upload-tip {
  margin-top: 6px;
  font-size: 12px;
  color: #8a919f;
}

.parse-result {
  margin-top: 16px;
}

.parse-grid {
  display: flex;
  gap: 40px;
  flex-wrap: wrap;
  margin-top: 8px;
}

.parse-item .label {
  display: inline-block;
  min-width: 76px;
  color: #8a919f;
}

.actions {
  display: flex;
  justify-content: center;
  gap: 16px;
  margin-top: 20px;
}

.degrade-tip {
  margin-top: 16px;
}

.small {
  font-size: 12px;
}
</style>
