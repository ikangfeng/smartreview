<script setup lang="ts">
// =============================================================================
// 法律知识库管理页（参照 smartbrief KnowledgeBase.vue 页面结构）
// =============================================================================
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox, type UploadFile } from 'element-plus'
import {
  clearKb,
  deleteKbDocument,
  fetchKbDocumentDetail,
  fetchKbDocuments,
  rebuildKbIndex,
  searchKb,
  uploadKbDocument,
} from '../api'
import type { KbCategory, KbDocument, KbDocumentDetail, KbSearchHit } from '../api/types'

const activeTab = ref<'docs' | 'search'>('docs')

// ---------------- 分类元数据 ----------------
const categoryMeta: Record<KbCategory, { label: string; type: 'primary' | 'warning' | 'success' | 'info' }> = {
  law: { label: '法律法规', type: 'primary' },
  judicial: { label: '司法解释', type: 'warning' },
  case: { label: '指导案例', type: 'success' },
  upload: { label: '上传文档', type: 'info' },
}
const categoryOrder: KbCategory[] = ['law', 'judicial', 'case', 'upload']

function catOf(category: string) {
  return categoryMeta[category as KbCategory] ?? { label: category, type: 'info' as const }
}

/** 后端返回的 source 为绝对路径，取文件名为展示名 */
function fileName(source: string): string {
  return source.split(/[\\/]/).pop() || source
}

// ---------------- 文档列表 ----------------
const documents = ref<KbDocument[]>([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    documents.value = (await fetchKbDocuments()).documents
  } catch {
    ElMessage.error('获取知识库列表失败，请检查后端服务')
  } finally {
    loading.value = false
  }
}

onMounted(load)

const categoryChunkCounts = computed(() => {
  const counts: Record<string, number> = {}
  for (const doc of documents.value) {
    counts[doc.category] = (counts[doc.category] ?? 0) + doc.chunk_count
  }
  return counts
})
const totalChunks = computed(() =>
  documents.value.reduce((sum, doc) => sum + doc.chunk_count, 0)
)

// ---------------- 列表筛选 / 删除 / 详情 ----------------
const listFilter = ref<'all' | KbCategory>('all')
const filteredDocuments = computed(() =>
  listFilter.value === 'all'
    ? documents.value
    : documents.value.filter((d) => d.category === listFilter.value)
)

const detailVisible = ref(false)
const detailLoading = ref(false)
const detailDoc = ref<KbDocument | null>(null)
const detailData = ref<KbDocumentDetail | null>(null)

async function openDetail(row: KbDocument) {
  detailDoc.value = row
  detailData.value = null
  detailVisible.value = true
  detailLoading.value = true
  try {
    detailData.value = await fetchKbDocumentDetail(row.source, row.category)
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail || '读取文档详情失败')
    detailVisible.value = false
  } finally {
    detailLoading.value = false
  }
}

async function handleDelete(row: KbDocument) {
  try {
    const { deleted } = await deleteKbDocument(row.source, row.category)
    ElMessage.success(`已删除「${row.name}」（${deleted} 个分块）`)
    await load()
  } catch {
    // 错误已由拦截器提示
  }
}

function confirmDelete(row: KbDocument) {
  ElMessageBox.confirm(
    `确定要删除「${row.name}」吗？（${catOf(row.category).label}，${row.chunk_count} 个分块）删除后不可恢复。`,
    '删除确认',
    { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' }
  )
    .then(() => handleDelete(row))
    .catch(() => {})
}

// ---------------- 上传语料 ----------------
const uploadCategory = ref<KbCategory>('upload')
const uploading = ref(false)

async function onPickFile(uploadFile: UploadFile) {
  if (!uploadFile.raw) {
    ElMessage.error('未获取到文件内容，请重新选择')
    return false
  }
  uploading.value = true
  try {
    const result = await uploadKbDocument(uploadFile.raw, uploadCategory.value)
    ElMessage.success(
      `「${result.name}」入库成功：新增 ${result.added} 块 / 共 ${result.chunks} 块`
    )
    await load()
  } catch {
    // 拦截器已提示（未配置嵌入 Key / 解析失败等）
  } finally {
    uploading.value = false
  }
  return false // 阻止 el-upload 自动上传
}

// ---------------- 重建索引 / 清空 ----------------
const reindexing = ref(false)

async function doReindex() {
  reindexing.value = true
  try {
    const result = await rebuildKbIndex()
    const parts = [`新增 ${result.added} 块`, `当前共 ${result.total} 块`]
    if (result.message) parts.push(result.message)
    ElMessage.success(parts.join('；'))
    await load()
  } catch {
    // 拦截器已提示
  } finally {
    reindexing.value = false
  }
}

function confirmClear() {
  ElMessageBox.confirm(
    '将清空整个法律知识库（全部向量数据）。此操作不可恢复，确定继续吗？',
    '清空知识库',
    { confirmButtonText: '清空', cancelButtonText: '取消', type: 'error' }
  )
    .then(async () => {
      try {
        const result = await clearKb()
        ElMessage.success(result.message)
        documents.value = []
      } catch {
        // 拦截器已提示
      }
    })
    .catch(() => {})
}

// ---------------- 检索测试 ----------------
const ragQuery = ref('')
const ragCategory = ref<'' | KbCategory>('')
const searching = ref(false)
const hits = ref<KbSearchHit[]>([])
const searched = ref(false)

async function doSearch() {
  const q = ragQuery.value.trim()
  if (!q) {
    ElMessage.warning('请输入检索文本')
    return
  }
  searching.value = true
  try {
    const result = await searchKb(q, ragCategory.value || undefined)
    hits.value = result.hits
    searched.value = true
  } catch {
    // 拦截器已提示
  } finally {
    searching.value = false
  }
}

/** 相似度展示：Chroma cosine 裁剪到 0~100% */
function scorePercent(score: number) {
  return `${(Math.max(0, Math.min(1, score)) * 100).toFixed(0)}%`
}
function scoreType(score: number) {
  if (score >= 0.7) return 'success' as const
  if (score >= 0.45) return 'warning' as const
  return 'info' as const
}
</script>

<template>
  <div class="page">
    <!-- 头部 -->
    <div class="kb-header">
      <div>
        <h2>法律知识库（RAG）</h2>
        <p class="hint">
          法律法规 / 司法解释 / 指导案例语料入库后，审查每个条款时自动语义检索相关法条注入 LLM。
          上传文件即时入库；放入 backend/data/kb 的语料需点「重建索引」。分类仅用于管理/筛选，
          审查时对全部类别统一语义检索，不区分效力层级。
        </p>
      </div>
      <div class="header-actions">
        <el-button :loading="reindexing" @click="doReindex">重建索引</el-button>
        <el-button type="danger" plain @click="confirmClear">清空</el-button>
        <el-button :loading="loading" @click="load">
          <el-icon style="margin-right: 4px"><Refresh /></el-icon>刷新
        </el-button>
      </div>
    </div>

    <!-- 统计 -->
    <div class="kb-panel">
      <div class="stat-row">
        <div v-for="key in categoryOrder" :key="key" class="stat-card">
          <el-tag :type="categoryMeta[key].type" size="small" effect="plain">
            {{ categoryMeta[key].label }}
          </el-tag>
          <span class="stat-num">{{ categoryChunkCounts[key] ?? 0 }}</span>
          <span class="stat-unit">块</span>
        </div>
        <div class="stat-card total-stat">
          <el-tag type="primary" effect="plain">全部</el-tag>
          <span class="stat-num">{{ totalChunks }}</span>
          <span class="stat-unit">块 / {{ documents.length }} 文件</span>
        </div>
      </div>

      <el-tabs v-model="activeTab">
        <!-- ============ 文档库 ============ -->
        <el-tab-pane label="文档库" name="docs">
          <div class="toolbar">
            <div class="filter-group">
              <span class="toolbar-label">分类：</span>
              <el-radio-group v-model="listFilter" size="small">
                <el-radio-button value="all">全部</el-radio-button>
                <el-radio-button v-for="key in categoryOrder" :key="key" :value="key">
                  {{ categoryMeta[key].label }}
                </el-radio-button>
              </el-radio-group>
            </div>
            <div class="upload-group">
              <el-radio-group v-model="uploadCategory" size="small">
                <el-radio-button v-for="key in categoryOrder" :key="key" :value="key">
                  {{ categoryMeta[key].label }}
                </el-radio-button>
              </el-radio-group>
              <el-upload
                :show-file-list="false"
                :auto-upload="false"
                accept=".txt,.md,.markdown,.docx,.pdf"
                :on-change="onPickFile"
                :disabled="uploading"
              >
                <el-button type="primary" :loading="uploading">
                  {{ uploading ? '入库中…' : '上传语料' }}
                </el-button>
              </el-upload>
            </div>
          </div>
          <p class="hint upload-hint">支持 txt / md / docx / pdf，上传后自动解析 → 分块 → 向量化入库（需配置嵌入 Key）</p>

          <el-table
            v-loading="loading"
            :data="filteredDocuments"
            class="kb-table"
            highlight-current-row
            @row-click="openDetail"
          >
            <el-table-column label="分类" width="100">
              <template #default="{ row }">
                <el-tag :type="catOf(row.category).type" size="small">
                  {{ catOf(row.category).label }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="文档" min-width="220" show-overflow-tooltip>
              <template #default="{ row }">
                <span class="doc-name">{{ row.name }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="file_type" label="类型" width="90" />
            <el-table-column prop="chunk_count" label="分块" width="80" />
            <el-table-column prop="snippet" label="内容摘要（首块）" min-width="240" show-overflow-tooltip />
            <el-table-column label="操作" width="120">
              <template #default="{ row }">
                <el-button link type="primary" @click.stop="openDetail(row)">查看</el-button>
                <el-button link type="danger" @click.stop="confirmDelete(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-empty
            v-if="!loading && filteredDocuments.length === 0"
            :description="documents.length === 0 ? '知识库为空：上传上方语料，或把文件放入 backend/data/kb 后点「重建索引」' : '该分类下暂无文档'"
          />

          <!-- 文档详情弹窗 -->
          <el-dialog
            v-model="detailVisible"
            :title="detailDoc ? `${detailDoc.name} — 文档详情` : '文档详情'"
            width="760px"
            top="3vh"
            destroy-on-close
          >
            <div v-loading="detailLoading" class="detail-body">
              <div v-if="detailData" class="detail-meta">
                <el-tag :type="catOf(detailData.category).type" size="small">
                  {{ catOf(detailData.category).label }}
                </el-tag>
                <code class="detail-source">{{ detailData.source }}</code>
                <span class="hint">{{ detailData.chunks.length }} 个分块</span>
              </div>
              <div v-if="detailData" class="detail-chunks">
                <div v-for="(chunk, idx) in detailData.chunks" :key="chunk.id" class="detail-chunk">
                  <div class="chunk-head">
                    <span class="chunk-index">#{{ idx + 1 }}</span>
                    <span v-if="chunk.metadata.page_number" class="chunk-page">
                      第 {{ chunk.metadata.page_number }} 页
                    </span>
                    <code class="chunk-id">{{ chunk.id }}</code>
                  </div>
                  <pre class="chunk-content">{{ chunk.content }}</pre>
                </div>
              </div>
              <div v-else-if="detailLoading" class="hint detail-loading">加载中…</div>
            </div>
          </el-dialog>
        </el-tab-pane>

        <!-- ============ 检索测试 ============ -->
        <el-tab-pane label="检索测试" name="search">
          <div class="search-block">
            <h3 class="search-title">语义检索（审查条款时 LLM 法条依据的数据源）</h3>
            <div class="search-bar">
              <el-input
                v-model="ragQuery"
                placeholder="输入检索文本，例如：违约金超过损失百分之三十"
                clearable
                @keyup.enter="doSearch"
              />
              <el-select v-model="ragCategory" placeholder="全部分类" clearable style="width: 150px">
                <el-option v-for="key in categoryOrder" :key="key" :label="categoryMeta[key].label" :value="key" />
              </el-select>
              <el-button type="primary" :loading="searching" @click="doSearch">检索</el-button>
            </div>
            <div v-if="searched" class="result-area">
              <div class="result-count hint">
                命中 {{ hits.length }} 条{{ hits.length === 0 ? '（空集合或相似度未超阈值）' : '' }}
              </div>
              <div v-for="(hit, i) in hits" :key="i" class="result-item">
                <div class="result-head">
                  <el-tag :type="catOf(hit.category).type" size="small">
                    {{ catOf(hit.category).label }}
                  </el-tag>
                  <el-tag :type="scoreType(hit.score)" size="small" effect="plain">
                    相似度 {{ scorePercent(hit.score) }}
                  </el-tag>
                  <span class="result-source">{{ fileName(hit.source) }}</span>
                </div>
                <pre class="result-content">{{ hit.content }}</pre>
              </div>
            </div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </div>
  </div>
</template>

<style scoped>
.kb-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.kb-header h2 {
  margin: 0;
  font-size: 20px;
  color: #1f2329;
}
.hint {
  color: #909399;
  font-size: 13px;
  line-height: 1.6;
  margin: 6px 0 0;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.kb-panel {
  background: #fff;
  border-radius: var(--sr-radius);
  box-shadow: 0 2px 10px rgba(31, 35, 41, 0.06);
  padding: 16px 20px 20px;
}
.stat-row {
  display: flex;
  gap: 12px;
  margin-bottom: 4px;
  flex-wrap: wrap;
}
.stat-card {
  display: flex;
  align-items: baseline;
  gap: 6px;
  background: #fafbfd;
  border-radius: 8px;
  padding: 8px 14px;
  border: 1px solid #eef0f4;
}
.total-stat {
  border-style: dashed;
}
.stat-num {
  font-size: 18px;
  font-weight: 600;
  color: #1f2329;
}
.stat-unit {
  font-size: 12px;
  color: #909399;
}
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 8px;
}
.filter-group,
.upload-group {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.toolbar-label {
  font-size: 13px;
  color: #606266;
}
.upload-hint {
  margin: 8px 0 12px;
}
.kb-table {
  width: 100%;
}
.doc-name {
  font-weight: 500;
  color: #303133;
}
.search-block {
  background: #fafbfd;
  border: 1px solid #eef0f4;
  border-radius: 8px;
  padding: 16px;
  margin-top: 8px;
}
.search-title {
  margin: 0 0 12px;
  font-size: 15px;
  font-weight: 600;
  color: #1f2329;
}
.search-bar {
  display: flex;
  gap: 8px;
}
.result-area {
  margin-top: 12px;
}
.result-count {
  margin-bottom: 8px;
}
.result-item {
  border: 1px solid #eef0f4;
  border-radius: 6px;
  padding: 8px 12px;
  margin-bottom: 8px;
  background: #fff;
}
.result-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}
.result-source {
  color: #606266;
  font-size: 12px;
  font-family: monospace;
  word-break: break-all;
}
.result-content {
  margin: 0;
  padding: 8px;
  background: #f5f7fa;
  border-radius: 4px;
  font-size: 12px;
  color: #303133;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 120px;
  overflow: auto;
}
.detail-body {
  padding: 8px 4px;
}
.detail-loading {
  padding: 24px 0;
  text-align: center;
}
.detail-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 1px solid #eef0f4;
  flex-wrap: wrap;
}
.detail-source {
  font-size: 12px;
  color: #909399;
  word-break: break-all;
}
.detail-chunks {
  display: flex;
  flex-direction: column;
  gap: 14px;
  max-height: 62vh;
  overflow: auto;
  padding-right: 4px;
}
.detail-chunk {
  border: 1px solid #eef0f4;
  border-radius: 8px;
  padding: 10px 12px;
  background: #fafbfd;
}
.chunk-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.chunk-index {
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}
.chunk-page {
  font-size: 12px;
  color: #909399;
}
.chunk-id {
  font-size: 11px;
  color: #c0c4cc;
  word-break: break-all;
}
.chunk-content {
  margin: 0;
  padding: 10px;
  background: #fff;
  border-radius: 4px;
  font-size: 13px;
  color: #303133;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 260px;
  overflow: auto;
}
</style>
