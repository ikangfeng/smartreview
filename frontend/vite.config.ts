// =============================================================================
// Vite 配置 - smartreview 前端
// =============================================================================
// 后端路由不带 /api 前缀，因此仅对后端实际暴露的路径前缀做代理转发，
// 其余路径（含 "/" 与 vite 内部资源）仍由 Vite 开发服务器处理。
// 注意：前端 SPA 路由首段刻意避开这些前缀（见 src/App.vue 注释），
// 因此这里的代理只命中 API 请求，不会吞掉前端页面路由。
// 生产环境由 nginx 使用相同路径前缀反代到 backend:8002（见 deploy/）。
// =============================================================================
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端 FastAPI 暴露的路径前缀（见 backend/main.py）
const backendPaths = [
  '/upload', '/review', '/tasks', '/history', '/playbooks', '/rules',
  '/knowledge', '/llm', '/health', '/audit', '/learning', '/metrics',
]

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5174,
    proxy: Object.fromEntries(
      backendPaths.map((p) => [
        p,
        {
          target: 'http://127.0.0.1:8002',
          changeOrigin: true,
          // 只代理 XHR/fetch（axios 等 API 请求）；浏览器整页导航（Accept: text/html，
          // 如直接刷新 /llm-config 等前端路由）不代理，交给 Vite SPA 处理，避免误转后端
          // 但 Word 报告下载 /tasks/*/report 必须代理——window.open 会带 text/html
          bypass: (req: any) => {
            const isDownload = /\/tasks\/[^/]+\/report(\?|$)/.test(req.url ?? '')
            if (isDownload) return undefined // 不 bypass，强制走代理
            return String(req.headers.accept ?? '').includes('text/html') ? req.url : undefined
          },
        },
      ])
    ),
  },
})
