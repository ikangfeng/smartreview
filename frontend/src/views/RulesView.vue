<script setup lang="ts">
// =============================================================================
// 规则配置页 - 确定性风控规则（双通道中的规则通道）
// =============================================================================
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { createRule, deleteRule, fetchRules, updateRule, fetchLearningInsights, applyLearningRule } from '../api'
import type { LearningInsights, MatcherKind, RiskLevel, RiskRule } from '../api/types'
import { RISK_TAG_TYPE } from '../utils/risk'

const rows = ref<RiskRule[]>([])
const loading = ref(false)

// ---------------- 学习闭环：由历史处置归纳规则建议 ----------------
const insights = ref<LearningInsights | null>(null)
const learningLoading = ref(false)

async function loadInsights() {
  learningLoading.value = true
  try {
    insights.value = await fetchLearningInsights(2)
  } catch {
    insights.value = null
  } finally {
    learningLoading.value = false
  }
}

async function adoptSuggestion(idx: number) {
  const s = insights.value?.suggestions?.[idx]
  if (!s) return
  try {
    await applyLearningRule(s.suggested_rule)
    ElMessage.success('已固化为企业规则，后续审查立即生效')
    await Promise.all([load(), loadInsights()])
  } catch (e: any) {
    ElMessage.error(`固化失败：${e?.message ?? e}`)
  }
}

const matcherLabel: Record<string, string> = {
  percent_ceiling: '比例上限',
  ip_rights: '知识产权归属',
  keyword_any: '关键词命中',
}

// ---------------- 弹窗（新增/编辑共用） ----------------
type DialogMode = 'create' | 'edit'
const dialogVisible = ref(false)
const dialogMode = ref<DialogMode>('edit')
const saving = ref(false)
const formRef = ref<FormInstance>()
const current = ref<RiskRule | null>(null)
const editForm = ref<{
  name: string
  category: string
  risk_level: RiskLevel
  description: string
  suggestion: string
}>({ name: '', category: '', risk_level: '低', description: '', suggestion: '' })

// 新增时专属的匹配配置
const matcherKind = ref<MatcherKind>('keyword_any')
const thresholdText = ref<number>(20) // percent_ceiling
const keywordsText = ref('') // keyword_any：逗号分隔
const matcherTip = computed(() => {
  if (matcherKind.value === 'percent_ceiling')
    return '条款中出现的百分比高于该阈值即命中（如 20）。阈值输入的百分比按「违约金/赔偿上限」语义理解。'
  if (matcherKind.value === 'ip_rights')
    return '命中：涉知识产权且约定非甲方独占（共有/归乙方等）。无需额外参数。'
  return '条款中出现任一关键词即命中；多个关键词用逗号分隔。'
})

const rules: FormRules = {
  name: [{ required: true, message: '请输入规则名称', trigger: 'blur' }],
  risk_level: [{ required: true, message: '请选择风险等级', trigger: 'change' }],
  matcher_kind: [{ required: true, message: '请选择匹配方式', trigger: 'change' }],
}

function openCreate() {
  dialogMode.value = 'create'
  current.value = null
  editForm.value = {
    name: '', category: '', risk_level: '高', description: '', suggestion: '',
  }
  matcherKind.value = 'keyword_any'
  thresholdText.value = 20
  keywordsText.value = ''
  dialogVisible.value = true
}

function openEdit(row: RiskRule) {
  dialogMode.value = 'edit'
  current.value = row
  editForm.value = {
    name: row.name ?? '',
    category: row.category ?? '',
    risk_level: row.risk_level,
    description: row.description ?? '',
    suggestion: row.suggestion ?? '',
  }
  dialogVisible.value = true
}

/** 依据当前匹配方式构造引擎可执行的 params */
function buildParams(): Record<string, unknown> {
  if (matcherKind.value === 'percent_ceiling') {
    return { threshold: thresholdText.value > 0 ? thresholdText.value : 20 }
  }
  if (matcherKind.value === 'keyword_any') {
    const kws = keywordsText.value.split(/[,，]/).map((s) => s.trim()).filter(Boolean)
    return { keywords: kws }
  }
  return {}
}

async function submit() {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return
  saving.value = true
  try {
    if (dialogMode.value === 'create') {
      if (matcherKind.value === 'keyword_any' && !((buildParams().keywords as string[]) || []).length) {
        ElMessage.warning('关键词匹配至少需要填写一个关键词')
        return
      }
      const created = await createRule({
        name: editForm.value.name,
        category: editForm.value.category || '自定义',
        matcher_kind: matcherKind.value,
        params: buildParams(),
        risk_level: editForm.value.risk_level,
        description: editForm.value.description,
        suggestion: editForm.value.suggestion,
        enabled: true,
      })
      rows.value.unshift(created)
      ElMessage.success('规则已创建，默认启用')
    } else if (current.value) {
      const updated = await updateRule(current.value.id, { ...editForm.value })
      const idx = rows.value.findIndex((r) => r.id === updated.id)
      if (idx >= 0) rows.value[idx] = updated
      ElMessage.success('规则已更新')
    }
    dialogVisible.value = false
  } catch {
    // 错误已由拦截器提示
  } finally {
    saving.value = false
  }
}

// ---------------- 启用开关 ----------------
async function toggleEnabled(row: RiskRule, val: string | number | boolean) {
  const updated = await updateRule(row.id, { enabled: !!val })
  const idx = rows.value.findIndex((r) => r.id === updated.id)
  if (idx >= 0) rows.value[idx] = updated
  ElMessage.success(updated.enabled ? '规则已启用' : '规则已停用')
}

// ---------------- 删除 ----------------
async function removeRule(row: RiskRule) {
  try {
    await ElMessageBox.confirm(
      `将删除规则「${row.name}」。删除后该规则不再参与审查，且服务重启不会自动恢复。此操作不可恢复，确认删除？`,
      '删除规则',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return // 用户取消
  }
  try {
    await deleteRule(row.id)
    rows.value = rows.value.filter((r) => r.id !== row.id)
    ElMessage.success('规则已删除')
  } catch {
    // 错误已由拦截器提示
  }
}

async function load() {
  loading.value = true
  try {
    const data = await fetchRules()
    rows.value = data.items
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  load()
  loadInsights()
})
</script>

<template>
  <div class="page">
    <!-- 学习闭环：把法务历史上的"已采纳/已修改"沉淀成规则建议 -->
    <div class="sr-card">
      <div class="sr-card-title" style="display: flex; align-items: center; justify-content: space-between">
        <span>🧠 规则自学习 · 越用越聪明</span>
        <el-button link type="primary" :loading="learningLoading" @click="loadInsights">重新分析</el-button>
      </div>
      <el-alert
        type="success"
        :closable="false"
        class="hint"
        title="数据来源是法务真实的处置动作，不是模拟数据：同一类条款若被反复改成同一个数值口径，系统会建议固化为确定性规则——固化后零 token、零幻觉、对新合同立即生效。是否固化由人决定。"
      />
      <div class="learn-pool">
        <span>企业样本池：</span>
        <el-tag
          v-for="(n, k) in insights?.sample_pool ?? {}"
          :key="k"
          size="small"
          :type="k === '已采纳' || k === '已修改' ? 'success' : 'info'"
          effect="plain"
          style="margin-right: 6px"
        >
          {{ k }} {{ n }}
        </el-tag>
        <span class="text-muted" style="margin-left: 8px">
          可学习样本 {{ insights?.total_samples ?? 0 }} 条
        </span>
      </div>
      <div v-if="insights?.suggestions?.length" class="learn-list">
        <div v-for="(s, i) in insights.suggestions" :key="i" class="learn-item">
          <div class="learn-msg">
            <el-tag size="small" type="warning" effect="dark">支持 {{ s.support }} 条</el-tag>
            <span>{{ s.message }}</span>
          </div>
          <el-button size="small" type="success" plain @click="adoptSuggestion(i)">
            ✨ 固化为规则
          </el-button>
        </div>
      </div>
      <div v-else class="text-muted" style="font-size: 12px">
        暂无足够的重复模式可归纳（同一主题需 ≥2 条历史改写收敛到同一口径）。继续处置条款即可积累样本。
      </div>
    </div>

    <div class="sr-card">
      <div class="sr-card-title" style="display: flex; align-items: center; justify-content: space-between">
        <span>确定性风控规则</span>
        <span>
          <el-button type="primary" plain @click="openCreate">
            <el-icon style="margin-right: 4px"><Plus /></el-icon>新增规则
          </el-button>
          <el-button link type="primary" @click="load">
            <el-icon style="margin-right: 4px"><Refresh /></el-icon>刷新
          </el-button>
        </span>
      </div>

      <el-alert
        type="info"
        :closable="false"
        class="hint"
        title="双通道判断：条款命中下方规则时，直接以规则结论为准（无需 LLM）；未命中时再由 LLM 分析。规则支持手动新增/编辑/启停/删除，删除后重启不会自动恢复。"
      />

      <el-table v-loading="loading" :data="rows" stripe>
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column prop="name" label="规则名称" min-width="160" show-overflow-tooltip />
        <el-table-column prop="category" label="类别" width="110" show-overflow-tooltip />
        <el-table-column label="匹配方式" width="120">
          <template #default="{ row }">
            {{ matcherLabel[row.matcher_kind] ?? row.matcher_kind }}
          </template>
        </el-table-column>
        <el-table-column label="风险等级" width="90">
          <template #default="{ row }">
            <el-tag :type="RISK_TAG_TYPE[row.risk_level as RiskLevel]" size="small">
              {{ row.risk_level }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="规则说明" min-width="240" show-overflow-tooltip />
        <el-table-column label="启用" width="80" align="center">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled === 1"
              @change="(v: string | number | boolean) => toggleEnabled(row, v)"
            />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" @click="removeRule(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 新增 / 编辑弹窗 -->
    <el-dialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? '新增规则' : '编辑规则'"
      width="560px"
      destroy-on-close
    >
      <el-form
        ref="formRef"
        :model="editForm"
        :rules="rules"
        label-width="90px"
        label-position="top"
      >
        <el-form-item label="规则名称" prop="name">
          <el-input v-model="editForm.name" placeholder="如：违约金/赔偿上限超标" />
        </el-form-item>
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="类别" prop="category">
              <el-input v-model="editForm.category" placeholder="如：违约责任" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="风险等级" prop="risk_level">
              <el-select v-model="editForm.risk_level" style="width: 100%">
                <el-option v-for="lv in (['高', '中', '低'] as const)" :key="lv" :label="`${lv}风险`" :value="lv" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <!-- 新增时配置匹配方式与参数；编辑时匹配逻辑不可改（改动会破坏判定一致性） -->
        <template v-if="dialogMode === 'create'">
          <el-form-item label="匹配方式" prop="matcher_kind">
            <el-select v-model="matcherKind" style="width: 100%">
              <el-option
                v-for="(label, kind) in matcherLabel"
                :key="kind"
                :label="label"
                :value="kind"
              />
            </el-select>
          </el-form-item>
          <el-form-item
            v-if="matcherKind === 'percent_ceiling'"
            label="百分比阈值"
            prop="threshold"
          >
            <el-input-number v-model="thresholdText" :min="0.1" :max="100" :precision="1" />
            <span style="margin-left: 8px">%（出现高于该值的百分比即命中）</span>
          </el-form-item>
          <el-form-item
            v-if="matcherKind === 'keyword_any'"
            label="触发关键词"
            prop="keywords"
          >
            <el-input v-model="keywordsText" placeholder="如：不可抗力、单方解除（逗号分隔）" />
          </el-form-item>
          <el-alert v-if="matcherKind === 'ip_rights'" type="info" :closable="false" :title="matcherTip" />
        </template>

        <el-form-item label="规则说明（命中后展示为风险描述）" prop="description">
          <el-input v-model="editForm.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="修改建议" prop="suggestion">
          <el-input v-model="editForm.suggestion" type="textarea" :rows="3" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">
          {{ dialogMode === 'create' ? '创建' : '保存' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.hint {
  margin-bottom: 16px;
}
.learn-pool {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
  font-size: 13px;
  margin-bottom: 12px;
}
.learn-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.learn-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  background: #f0fdf4;
  border: 1px solid #bbf7d0;
  border-radius: 8px;
}
.learn-msg {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #166534;
  line-height: 1.6;
}
</style>
