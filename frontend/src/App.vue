<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()

// 导航高亮：/check、/result 属于"上传审查"流程中的延续，归入首页分组。
// 注：SPA 路由首段刻意避开后端 API 前缀（/upload /review /tasks /history
// /rules /knowledge /llm /health），保证刷新子页面不会被反代吞掉。
const activeMenu = computed(() => {
  const path = route.path
  if (path.startsWith('/records')) return '/records'
  if (path.startsWith('/audit')) return '/audit'
  if (path.startsWith('/playbooks')) return '/playbooks'
  if (path.startsWith('/risk-rules')) return '/risk-rules'
  if (path.startsWith('/kb')) return '/kb'
  if (path.startsWith('/model')) return '/model'
  return '/'
})
</script>

<template>
  <el-container class="layout">
    <el-header class="header">
      <div class="brand">
        <div class="logo">法</div>
        <div class="brand-text">
          <span class="title">SmartReview</span>
          <span class="subtitle">法律合同智能审查系统</span>
        </div>
      </div>
      <el-menu
        class="nav"
        mode="horizontal"
        :default-active="activeMenu"
        :ellipsis="false"
        router
      >
        <el-menu-item index="/">上传审查</el-menu-item>
        <el-menu-item index="/records">历史记录</el-menu-item>
        <el-menu-item index="/audit">审计日志</el-menu-item>
        <el-menu-item index="/playbooks">审查方案</el-menu-item>
        <el-menu-item index="/kb">知识库</el-menu-item>
        <el-menu-item index="/risk-rules">规则配置</el-menu-item>
        <el-menu-item index="/model">模型配置</el-menu-item>
      </el-menu>
    </el-header>
    <el-main class="main">
      <router-view />
    </el-main>
  </el-container>
</template>

<style scoped>
.layout {
  min-height: 100%;
}

.header {
  display: flex;
  align-items: center;
  gap: 32px;
  height: 60px;
  padding: 0 24px;
  background: #fff;
  border-bottom: 1px solid #eef0f4;
  box-shadow: 0 1px 4px rgba(31, 35, 41, 0.04);
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
}

.logo {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  border-radius: 9px;
  background: linear-gradient(135deg, var(--sr-brand), var(--sr-brand-light));
  color: #fff;
  font-weight: 700;
  font-size: 18px;
}

.brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.2;
}

.brand-text .title {
  font-size: 17px;
  font-weight: 700;
  color: #1f2329;
}

.brand-text .subtitle {
  font-size: 12px;
  color: #8a919f;
}

.nav {
  flex: 1;
  border-bottom: none;
}

.main {
  padding: 0;
}
</style>
