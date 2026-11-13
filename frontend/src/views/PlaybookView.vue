<script setup lang="ts">
// =============================================================================
// 审查方案管理页 - Playbook（合同类型 + 立场 + 企业审查口径）
// =============================================================================
// 方案决定一次审查的：必备条款清单、参与规则子集、知识库检索范围、附加提示词。
// 上传页按类型自动匹配默认方案，也可在此维护多套口径（如甲方版/乙方版）。
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createPlaybook,
  deletePlaybook,
  fetchPlaybooks,
  fetchRules,
  updatePlaybook,
} from '../api'
import type { Playbook, PlaybookPayload, RiskRule } from '../api/types'

const rows = ref<Playbook[]>([])
const rules = ref<RiskRule[]>([])
const loading = ref(false)

const stances = ['甲方', '乙方', '中立']
const kbCategories = [
  { value: 'law', label: '法律法规' },
  { value: 'judicial', label: '司法解释' },
  { value: 'case', label: '指导案例' },
  { value: 'upload', label: '上传文档' },
]
const contractTypes = [
  '技术服务/开发合同',
  '采购/买卖合同',
  '租赁合同',
  '保密协议',
  '劳动/劳务合同',
  '通用合同',
]

/** 编辑态必备条款行：keywords 在表单里用逗号分隔文本编辑，保存时拆分 */
interface ClauseRow {
  name: string
  keywordsText: string
  severity: '高' | '中' | '低'
  suggestion: string
}

const dialogVisible = ref(false)
const saving = ref(false)
const editingId = ref<number | null>(null)
const form = ref({
  name: '',
  contract_type: '',
  stance: '甲方',
  description: '',
  extra_prompt: '',
  is_default: false,
  enabled: true,
  kb_categories: [] as string[],
  rule_ids: [] as number[],
  clauses: [] as ClauseRow[],
})

const dialogTitle = computed(() => (editingId.value ? '编辑审查方案' : '新建审查方案'))

async function loadAll() {
  loading.value = true
  try {
    const [pb, rs] = await Promise.all([fetchPlaybooks(false), fetchRules()])
    rows.value = pb.items
    rules.value = rs.items
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false
  }
}

onMounted(loadAll)

function resetForm() {
  form.value = {
    name: '',
    contract_type: '',
    stance: '甲方',
    description: '',
    extra_prompt: '',
    is_default: false,
    enabled: true,
    kb_categories: [],
    rule_ids: [],
    clauses: [],
  }
}

function openCreate() {
  editingId.value = null
  resetForm()
  form.value.is_default = false
  dialogVisible.value = true
}

function openEdit(row: Playbook) {
  editingId.value = row.id
  form.value = {
    name: row.name,
    contract_type: row.contract_type || '',
    stance: row.stance || '甲方',
    description: row.description || '',
    extra_prompt: row.extra_prompt || '',
    is_default: !!row.is_default,
    enabled: !!row.enabled,
    kb_categories: [...(row.kb_categories || [])],
    rule_ids: [...(row.rule_ids || [])],
    clauses: (row.required_clauses || []).map((c) => ({
      name: c.name,
      keywordsText: (c.keywords || []).join('，'),
      severity: c.severity,
      suggestion: c.suggestion || '',
    })),
  }
  dialogVisible.value = true
}

function addClause() {
  form.value.clauses.push({ name: '', keywordsText: '', severity: '中', suggestion: '' })
}

function removeClause(index: number) {
  form.value.clauses.splice(index, 1)
}

function buildPayload(): PlaybookPayload {
  return {
    name: form.value.name.trim(),
    contract_type: form.value.contract_type || null,
    stance: form.value.stance,
    description: form.value.description,
    required_clauses: form.value.clauses
      .filter((c) => c.name.trim())
      .map((c) => ({
        name: c.name.trim(),
        keywords: c.keywordsText
          .split(/[,，、]/)
          .map((s) => s.trim())
          .filter(Boolean),
        severity: c.severity,
        suggestion: c.suggestion.trim(),
      })),
    rule_ids: form.value.rule_ids,
    kb_categories: form.value.kb_categories,
    extra_prompt: form.value.extra_prompt,
    is_default: form.value.is_default,
    enabled: form.value.enabled,
  }
}

async function submit() {
  if (!form.value.name.trim()) {
    ElMessage.warning('请填写方案名称')
    return
  }
  saving.value = true
  try {
    const payload = buildPayload()
    if (editingId.value) {
      await updatePlaybook(editingId.value, payload)
      ElMessage.success('方案已更新')
    } else {
      await createPlaybook(payload)
      ElMessage.success('方案已创建')
    }
    dialogVisible.value = false
    await loadAll()
  } catch {
    // 拦截器已提示
  } finally {
    saving.value = false
  }
}

async function remove(row: Playbook) {
  try {
    await ElMessageBox.confirm(
      `确认删除方案「${row.name}」？历史审查任务的方案快照不受影响。`,
      '删除确认',
      { type: 'warning' }
    )
    await deletePlaybook(row.id)
    ElMessage.success('方案已删除')
    await loadAll()
  } catch {
    // 取消或请求失败（拦截器已提示）
  }
}

function ruleScope(row: Playbook): string {
  return row.rule_ids?.length ? `${row.rule_ids.length} 条规则` : '全部启用规则'
}

function ruleName(id: number): string {
  return rules.value.find((r) => r.id === id)?.name || `#${id}`
}
</script>

<template>
  <div class="page">
    <div class="sr-card">
      <div class="sr-card-title">
        审查方案（Playbook）
        <el-button type="primary" @click="openCreate">新建方案</el-button>
      </div>
      <p class="intro text-muted">
        方案 = 合同类型 + 审查立场 + 企业审查口径：决定必备条款清单、参与审查的规则子集、
        知识库检索范围与附加提示词。上传合同后可按类型自动匹配默认方案，也可手动指定。
      </p>

      <el-table :data="rows" v-loading="loading" stripe style="width: 100%">
        <el-table-column label="方案名称" min-width="230">
          <template #default="{ row }">
            <div class="pb-name">
              <span>{{ row.name }}</span>
              <el-tag v-if="row.is_default" size="small" type="success" effect="plain">默认</el-tag>
              <el-tag v-if="!row.enabled" size="small" type="info" effect="plain">已停用</el-tag>
            </div>
            <div v-if="row.description" class="pb-sub">{{ row.description }}</div>
          </template>
        </el-table-column>
        <el-table-column label="合同类型" width="150">
          <template #default="{ row }">{{ row.contract_type || '通用' }}</template>
        </el-table-column>
        <el-table-column label="立场" width="80">
          <template #default="{ row }">{{ row.stance }}</template>
        </el-table-column>
        <el-table-column label="审查口径" min-width="260">
          <template #default="{ row }">
            <div class="scope-line">
              <el-tag size="small" effect="plain">必备条款 {{ row.required_clauses.length }} 项</el-tag>
              <el-tag size="small" effect="plain">{{ ruleScope(row) }}</el-tag>
              <el-tag size="small" effect="plain">
                知识库：{{ row.kb_categories.length ? row.kb_categories.length + ' 类' : '不限' }}
              </el-tag>
            </div>
            <div v-if="row.rule_ids.length" class="pb-sub">
              规则：{{ row.rule_ids.slice(0, 4).map(ruleName).join('、') }}
              <span v-if="row.rule_ids.length > 4"> 等 {{ row.rule_ids.length }} 条</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="160">
          <template #default="{ row }">{{ row.updated_at || '—' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 新建 / 编辑弹窗 -->
    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="820px" top="6vh">
      <el-form label-width="96px" label-position="right">
        <el-form-item label="方案名称" required>
          <el-input v-model="form.name" placeholder="如：技术服务/开发合同 · 甲方审查方案" />
        </el-form-item>
        <el-form-item label="合同类型">
          <el-select
            v-model="form.contract_type"
            filterable
            allow-create
            clearable
            placeholder="选择或输入合同类型（留空 = 通用）"
            style="width: 100%"
          >
            <el-option v-for="t in contractTypes" :key="t" :value="t" :label="t" />
          </el-select>
        </el-form-item>
        <el-form-item label="审查立场">
          <el-radio-group v-model="form.stance">
            <el-radio-button v-for="s in stances" :key="s" :value="s">{{ s }}</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="方案说明">
          <el-input v-model="form.description" type="textarea" :rows="2" placeholder="适用场景与审查口径说明" />
        </el-form-item>

        <el-form-item label="必备条款">
          <div class="clause-editor">
            <div v-for="(c, i) in form.clauses" :key="i" class="clause-row">
              <el-input v-model="c.name" placeholder="条款名称" class="c-name" />
              <el-input v-model="c.keywordsText" placeholder="判定关键词（逗号分隔）" class="c-kw" />
              <el-select v-model="c.severity" class="c-sev">
                <el-option value="高" label="高" />
                <el-option value="中" label="中" />
                <el-option value="低" label="低" />
              </el-select>
              <el-input v-model="c.suggestion" placeholder="缺失时的补充建议" class="c-sug" />
              <el-button link type="danger" @click="removeClause(i)">移除</el-button>
            </div>
            <el-button link type="primary" @click="addClause">+ 添加必备条款</el-button>
          </div>
        </el-form-item>

        <el-form-item label="参与规则">
          <el-select
            v-model="form.rule_ids"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            placeholder="不选 = 全部启用规则"
            style="width: 100%"
          >
            <el-option
              v-for="r in rules"
              :key="r.id"
              :value="r.id"
              :label="`${r.name}（${r.category || '未分类'}）`"
            >
              <span>{{ r.name }}</span>
              <span class="rule-opt-meta">{{ r.category }} · {{ r.risk_level }}风险</span>
            </el-option>
          </el-select>
        </el-form-item>

        <el-form-item label="知识库范围">
          <el-checkbox-group v-model="form.kb_categories">
            <el-checkbox v-for="c in kbCategories" :key="c.value" :value="c.value">
              {{ c.label }}
            </el-checkbox>
          </el-checkbox-group>
          <div class="text-muted small">不勾选 = 检索全部知识库；勾选后仅在所选分类中检索法条。</div>
        </el-form-item>

        <el-form-item label="附加提示词">
          <el-input
            v-model="form.extra_prompt"
            type="textarea"
            :rows="3"
            placeholder="叠加到审查系统提示词的附加要求，如：重点关注验收标准是否量化、知识产权是否归甲方"
          />
        </el-form-item>

        <el-form-item label="方案状态">
          <el-switch v-model="form.is_default" active-text="设为该合同类型的默认方案" />
          <el-switch v-model="form.enabled" active-text="启用" style="margin-left: 24px" />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.intro {
  margin: -4px 0 16px;
  font-size: 13px;
}

.pb-name {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
}

.pb-sub {
  margin-top: 4px;
  font-size: 12px;
  color: #8a919f;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.scope-line {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.clause-editor {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.clause-row {
  display: flex;
  gap: 8px;
  align-items: center;
}

.c-name {
  width: 150px;
  flex: none;
}

.c-kw {
  flex: 1;
}

.c-sev {
  width: 84px;
  flex: none;
}

.c-sug {
  flex: 1;
}

.rule-opt-meta {
  float: right;
  color: #8a919f;
  font-size: 12px;
  margin-left: 16px;
}

.small {
  font-size: 12px;
}
</style>
