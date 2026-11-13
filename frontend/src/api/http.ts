// =============================================================================
// Axios 实例封装
// =============================================================================
// 后端路由不带 /api 前缀：开发环境由 Vite 代理，生产由 nginx 反代，
// 因此实例 baseURL 保持 "/"（同源）。响应拦截统一解包 data。
// =============================================================================
import axios, { type AxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'

/** 扩展配置：业务层可静默错误（轮询 404 等由页面自行处理） */
export interface RequestOptions extends AxiosRequestConfig {
  silent?: boolean
}

const http = axios.create({
  baseURL: '/',
  timeout: 60000,
})

// 审计留痕：演示环境不接登录，用本地记住的姓名作为操作人（X-Operator），
// 让"谁改了什么"在审计日志里可读；企业版替换为登录态即可。
// 注意：HTTP 头只允许 latin-1，中文姓名必须 URI 编码后再传（后端 unquote 还原）。
http.interceptors.request.use((config) => {
  const operator = localStorage.getItem('sr_operator') || '演示用户'
  config.headers.set('X-Operator', encodeURIComponent(operator))
  return config
})

// 响应拦截：直接返回 data，让调用方拿到类型化结果
http.interceptors.response.use(
  (res) => res.data,
  (err) => {
    const config = (err?.config ?? {}) as RequestOptions
    const detail = err?.response?.data?.detail
    const message =
      typeof detail === 'string'
        ? detail
        : err?.message ?? '网络异常，请确认后端服务已启动'
    if (!config.silent) {
      ElMessage.error(`请求失败：${message}`)
    }
    return Promise.reject(err)
  }
)

/** 类型安全 GET：返回已解包的 data */
export function httpGet<T>(url: string, config?: RequestOptions): Promise<T> {
  return http.get(url, config) as Promise<T>
}

/** 类型安全 POST */
export function httpPost<T>(url: string, data?: unknown, config?: RequestOptions): Promise<T> {
  return http.post(url, data, config) as Promise<T>
}

/** 类型安全 PUT */
export function httpPut<T>(url: string, data?: unknown, config?: RequestOptions): Promise<T> {
  return http.put(url, data, config) as Promise<T>
}

/** 类型安全 DELETE */
export function httpDelete<T>(url: string, config?: RequestOptions): Promise<T> {
  return http.delete(url, config) as Promise<T>
}

export default http
