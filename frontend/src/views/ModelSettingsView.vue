<script setup lang="ts">
// =============================================================================
// 模型配置页 - 运行时自定义 LLM（OpenAI 兼容端点，典型 = 本地 vLLM 微调模型）
// 与 .env 静态配置互补：保存到 llm_profile 单行表，进程内即时生效；
// 自定义端点不可用时自动降级到 .env 默认链（vllm → siliconflow）。
// =============================================================================
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { fetchCanaryConfig, fetchCanaryMetrics, fetchHealth, fetchLlmConfig, saveCanaryConfig, saveLlmConfig, testLlmConfig } from '../api'
import type { CanaryMetrics, HealthResult, LlmProbeResult } from '../api/types'

const formRef = ref<FormInstance>()
const saving = ref(false)
const probing = ref(false)
const appliedHint = ref('')
const probeResult = ref<LlmProbeResult | null>(null)
const health = ref<HealthResult | null>(null)

const form = reactive({
  enabled: false,
  base_url: '',
  model: '',
  api_key: '',
  api_key_set: false,
})

const providerLabel: Record<string, string> = {
  custom: '自定义端点',
  vllm: '本地 vLLM',
  siliconflow: 'SiliconFlow',
  none: '未连接（降级）',
}

const rules: FormRules = {
  base_url: [
    { required: true, message: '请输入 API 根地址', trigger: 'blur' },
    {
      validator: (_r, value: string, cb) => {
        const v = value?.trim() ?? ''
        if (!/^https?:\/\//.test(v)) cb(new Error('需以 http:// 或 https:// 开头'))
        else if (/\/chat\/completions$|\/completions$/.test(v.replace(/\/+$/, '')))
          cb(new Error('填写根地址即可，如 http://127.0.0.1:8001/v1'))
        else cb()
      },
      trigger: 'blur',
    },
  ],
  model: [{ required: true, message: '请输入模型名称', trigger: 'blur' }],
}

async function load() {
  try {
    const cfg = await fetchLlmConfig()
    form.enabled = cfg.enabled
    form.base_url = cfg.base_url
    form.model = cfg.model
    // 明文回显已保存的 Key，实现"所见即所得"：可直接修改，清空后保存即删除
    form.api_key = cfg.api_key ?? ''
    form.api_key_set = cfg.api_key_set
    appliedHint.value = cfg.applied
      ? `已生效（自定义端点优先） · 最后保存：${cfg.updated_at ?? '—'}`
      : cfg.enabled
        ? '已启用但未生效（base_url/model 不合法）'
        : '未启用（当前按 .env 配置链 vllm → siliconflow）'
  } catch {
    // 拦截器已提示
  }
  try {
    health.value = await fetchHealth()
  } catch {
    health.value = null
  }
}

onMounted(load)

// ---------------- 灰度发布 ----------------
const canary = reactive({ enabled: false, percent: 10, base_url: '', model: '', api_key: '' })
const canarySaving = ref(false)
const canaryStatus = ref<{ active: boolean; percent: number; circuit_open: boolean } | null>(null)
const canaryMetrics = ref<CanaryMetrics | null>(null)

async function loadCanary() {
  try {
    const cfg = await fetchCanaryConfig()
    canary.enabled = cfg.enabled
    canary.percent = cfg.percent
    canary.base_url = cfg.base_url
    canary.model = cfg.model
    canary.api_key = cfg.api_key ?? ''
    canaryStatus.value = cfg.status
  } catch {
    // 拦截器已提示
  }
  await refreshCanaryMetrics()
}

async function refreshCanaryMetrics() {
  try {
    canaryMetrics.value = await fetchCanaryMetrics()
  } catch {
    canaryMetrics.value = null
  }
}

async function onSaveCanary() {
  canarySaving.value = true
  try {
    const saved = await saveCanaryConfig({ ...canary })
    canary.api_key = saved.api_key ?? ''
    canaryStatus.value = saved.status
    ElMessage.success(
      saved.enabled && saved.percent > 0
        ? `灰度已开启：约 ${saved.percent}% 的新任务走 canary`
        : '灰度已关闭，全部任务走基线链',
    )
    await refreshCanaryMetrics()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '灰度配置保存失败')
  } finally {
    canarySaving.value = false
  }
}

function variantLabel(v: string) {
  if (v === 'canary') return '灰度 canary'
  if (v === 'mixed') return '混合（部分回落）'
  return '基线 baseline'
}

function avgText(v: number | null) {
  return v == null ? '—' : `${v.toFixed(1)}s`
}

onMounted(loadCanary)

async function refreshHealth() {
  try {
    health.value = await fetchHealth()
  } catch {
    health.value = null
  }
}

function buildPayload() {
  return {
    enabled: form.enabled,
    base_url: form.base_url.trim(),
    model: form.model.trim(),
    // 所见即所得：框内值即最终值（空 = 清除已保存的 Key）
    api_key: form.api_key,
  }
}

async function onProbe() {
  probeResult.value = null
  try {
    await formRef.value?.validate()
  } catch {
    ElMessage.warning('请按提示修正表单后再测试')
    return
  }
  probing.value = true
  try {
    probeResult.value = await testLlmConfig({
      base_url: form.base_url.trim(),
      model: form.model.trim(),
      api_key: form.api_key,
    })
  } catch (e: any) {
    probeResult.value = {
      ok: false,
      latency_ms: null,
      reply: null,
      error: e?.response?.data?.detail || e?.message || '请求失败',
    }
  } finally {
    probing.value = false
  }
}

async function onSave() {
  try {
    await formRef.value?.validate()
  } catch {
    ElMessage.warning('请按提示修正表单后再保存')
    return
  }
  saving.value = true
  try {
    const saved = await saveLlmConfig(buildPayload())
    // 后端返回明文，回填保持输入框与数据库同步（含"清空后保存"的情形）
    form.api_key = saved.api_key ?? ''
    form.api_key_set = saved.api_key_set
    appliedHint.value = saved.applied
      ? `已生效（自定义端点优先） · 最后保存：${saved.updated_at ?? '—'}`
      : '已保存；当前未启用自定义端点，按 .env 配置链生效'
    ElMessage.success(saved.api_key ? '配置已保存并生效' : '已保存（API Key 已清除）')
    await refreshHealth()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

function activeModelText(): string {
  const h = health.value
  if (!h) return '后端未连接'
  const model = h.llm_model ? ` · ${h.llm_model}` : ''
  return `${providerLabel[h.llm_provider] ?? h.llm_provider}${model}`
}

/** 重置：清空本页表单（不写数据库），并清掉测试结果与生效提示 */
function resetForm() {
  form.enabled = false
  form.base_url = ''
  form.model = ''
  form.api_key = ''
  form.api_key_set = false
  probeResult.value = null
  appliedHint.value = ''
  formRef.value?.clearValidate()
}
</script>

<template>
  <div class="page">
    <div class="sr-card">
      <div class="sr-card-title">模型配置</div>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="运行时自定义大模型（OpenAI 兼容）"
        description="把本系统接入为当前项目微调并部署的模型（例如 vLLM 启动的服务）。填写 API 根地址、API Key（可留空）与模型名称后保存即生效；自定义端点不可用时，审查会自动降级到 .env 配置的 vllm → siliconflow 链。"
      />

      <div class="status-line">
        <span class="text-muted">当前实际使用的模型：</span>
        <span class="mono">{{ activeModelText() }}</span>
        <el-tag
          v-if="health?.llm_provider === 'custom'"
          size="small"
          type="success"
          style="margin-left: 8px"
        >
          自定义生效
        </el-tag>
        <span v-if="appliedHint" class="applied-hint">{{ appliedHint }}</span>
      </div>

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-width="110px"
        class="config-form"
      >
        <el-form-item label="启用">
          <el-switch
            v-model="form.enabled"
            active-text="启用自定义端点（关闭则按 .env 默认链）"
          />
        </el-form-item>

        <el-form-item label="API 地址" prop="base_url">
          <el-input
            v-model="form.base_url"
            placeholder="如 http://127.0.0.1:8001/v1 （vLLM 兼容端点）或 https://api.siliconflow.cn/v1"
            clearable
          />
          <div class="form-tip">
            填 OpenAI 兼容根地址，不要带 /chat/completions；本地 vLLM 通常形如
            http://127.0.0.1:8001/v1
          </div>
        </el-form-item>

        <el-form-item label="API Key">
          <el-input
            v-model="form.api_key"
            type="password"
            show-password
            placeholder="粘贴 API Key；本地 vLLM 可留空"
            clearable
          />
          <div class="form-tip">
            Key 明文显示在此，可直接修改；清空后点"保存并生效"即删除已保存的 Key
            （点击右侧眼睛可查看）。本地 vLLM 通常不校验鉴权，留空即可。
          </div>
        </el-form-item>

        <el-form-item label="模型名称" prop="model">
          <el-input
            v-model="form.model"
            placeholder="如 Qwen2.5-14B-Instruct 或微调模型名"
            clearable
          />
          <div class="form-tip">
            须与 vLLM 启动参数 --served-model-name 保持一致（微调模型请填实际部署名）
          </div>
        </el-form-item>

        <el-form-item>
          <el-button
            :loading="probing"
            type="primary"
            plain
            @click="onProbe"
          >
            测试连接
          </el-button>
          <el-button type="primary" :loading="saving" @click="onSave">保存并生效</el-button>
          <el-button @click="resetForm">重置</el-button>
        </el-form-item>
      </el-form>

      <div v-if="probeResult" class="probe-result">
        <el-alert
          :type="probeResult.ok ? 'success' : 'error'"
          :closable="false"
          show-icon
          :title="probeResult.ok ? `连接成功 · 耗时 ${probeResult.latency_ms ?? '—'}ms` : '连接失败'"
          :description="probeResult.ok ? (probeResult.reply || '模型已响应') : (probeResult.error || '')"
        />
      </div>
    </div>

    <!-- 灰度发布：按任务把一部分真实流量切给新模型，失败自动熔断回落 -->
    <div class="sr-card canary-card">
      <div class="card-head">
        <div>
          <div class="card-title">灰度发布（Canary）</div>
          <div class="card-sub">
            按任务哈希稳定分流：同一份合同的所有条款固定走同一变体，便于 A/B 归因；
            canary 连续失败 3 次自动熔断 10 分钟并回落到基线链。
          </div>
        </div>
        <el-tag v-if="canaryStatus?.circuit_open" type="danger" effect="dark">已熔断</el-tag>
        <el-tag v-else-if="canaryStatus?.active" type="success" effect="dark">
          运行中 · {{ canaryStatus.percent }}%
        </el-tag>
        <el-tag v-else type="info">未启用</el-tag>
      </div>

      <el-form label-width="120px" class="config-form">
        <el-form-item label="启用灰度">
          <el-switch v-model="canary.enabled" active-text="开启分流" />
        </el-form-item>
        <el-form-item label="分流比例">
          <el-slider
            v-model="canary.percent"
            :min="0"
            :max="100"
            :step="5"
            show-input
            style="max-width: 460px"
          />
        </el-form-item>
        <el-form-item label="Canary 地址">
          <el-input v-model="canary.base_url" placeholder="如 http://127.0.0.1:8003/v1" clearable />
        </el-form-item>
        <el-form-item label="Canary Key">
          <el-input v-model="canary.api_key" type="password" show-password clearable placeholder="留空表示无需鉴权" />
        </el-form-item>
        <el-form-item label="Canary 模型">
          <el-input v-model="canary.model" placeholder="如 Qwen2.5-32B-Instruct" clearable />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="canarySaving" @click="onSaveCanary">保存灰度配置</el-button>
        </el-form-item>
      </el-form>

      <div v-if="canaryMetrics?.variants?.length" class="canary-metrics">
        <div class="metrics-title">A/B 观测（历史任务）</div>
        <el-table :data="canaryMetrics.variants" size="small" border>
          <el-table-column label="变体" width="130">
            <template #default="{ row }">
              <el-tag
                size="small"
                :type="row.variant === 'canary' ? 'warning' : row.variant === 'mixed' ? 'danger' : 'info'"
              >
                {{ variantLabel(row.variant) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="tasks" label="任务数" width="85" />
          <el-table-column label="完成/失败" width="100">
            <template #default="{ row }">{{ row.completed }} / {{ row.failed }}</template>
          </el-table-column>
          <el-table-column label="平均耗时" width="100">
            <template #default="{ row }">{{ avgText(row.avg_seconds) }}</template>
          </el-table-column>
          <el-table-column prop="tokens" label="Tokens" width="100" />
          <el-table-column label="高危条款" width="95">
            <template #default="{ row }">{{ row.risk?.['高'] ?? 0 }}</template>
          </el-table-column>
        </el-table>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sr-card-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 16px;
  font-weight: 600;
}

.status-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
  padding: 12px 16px;
  margin-top: 16px;
  background: var(--el-fill-color-light, #f5f7fa);
  border-radius: 8px;
  font-size: 13px;
}

.applied-hint {
  margin-left: auto;
  color: var(--el-color-success, #67c23a);
  font-size: 12px;
}

.config-form {
  max-width: 720px;
  margin-top: 8px;
  padding-top: 16px;
}

.canary-card {
  margin-top: 16px;
}

.card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.card-title {
  font-size: 16px;
  font-weight: 600;
}

.card-sub {
  margin-top: 6px;
  color: var(--el-text-color-secondary, #909399);
  font-size: 12px;
  line-height: 1.6;
  max-width: 720px;
}

.canary-metrics {
  margin-top: 4px;
}

.metrics-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-regular, #606266);
  margin-bottom: 8px;
}

.form-tip {
  width: 100%;
  color: var(--el-text-color-secondary, #909399);
  font-size: 12px;
  line-height: 1.5;
}

.probe-result {
  margin-top: 8px;
  max-width: 720px;
}
</style>
