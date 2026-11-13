// =============================================================================
// 路由表 - smartreview 页面
// =============================================================================
import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'home',
      component: () => import('../views/HomeView.vue'),
      meta: { title: '上传审查' },
    },
    {
      path: '/check/:taskId',
      name: 'review',
      component: () => import('../views/ReviewView.vue'),
      props: true,
      meta: { title: '审查中' },
    },
    {
      path: '/result/:taskId',
      name: 'result',
      component: () => import('../views/ResultView.vue'),
      props: true,
      meta: { title: '审查结果' },
    },
    {
      path: '/records',
      name: 'history',
      component: () => import('../views/HistoryView.vue'),
      meta: { title: '历史记录' },
    },
    {
      path: '/audit',
      name: 'audit',
      component: () => import('../views/AuditView.vue'),
      meta: { title: '审计日志' },
    },
    {
      path: '/playbooks',
      name: 'playbooks',
      component: () => import('../views/PlaybookView.vue'),
      meta: { title: '审查方案' },
    },
    {
      path: '/risk-rules',
      name: 'rules',
      component: () => import('../views/RulesView.vue'),
      meta: { title: '规则配置' },
    },
    {
      path: '/kb',
      name: 'knowledge',
      component: () => import('../views/KnowledgeView.vue'),
      meta: { title: '知识库管理' },
    },
    {
      path: '/model',
      name: 'llm-config',
      component: () => import('../views/ModelSettingsView.vue'),
      meta: { title: '模型配置' },
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/',
    },
  ],
})

router.afterEach((to) => {
  const title = to.meta?.title
  document.title = title ? `${title} · SmartReview` : 'SmartReview · 法律合同智能审查'
})

export default router
