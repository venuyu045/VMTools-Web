import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from '@/router'

const client = axios.create({
  baseURL: '/api',
  timeout: 15000,
})

/**
 * 「本次会话已处理过登录过期」闩锁。
 *
 * 一个页面往往并发多个请求，token 失效时会**同时**收到多个 401；
 * 无闩锁时每个 401 各弹一次「登录已过期」并各 `router.push('/login')` 一次
 * → 提示风暴 + 重复导航。请求重新带上 token（= 已重新登录）时复位。
 */
let sessionExpiredHandled = false

client.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) {
    sessionExpiredHandled = false // 已重新登录：下次过期仍需提示
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

client.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      // 2026-10-15 审查修复：仅在【确实持有 token】（曾登录、token 失效）时才执行
      // 「清 token + 提示过期 + 跳登录」。原逻辑对所有 401 一律处理，导致未登录访客
      // 访问 guestReadable 只读页（任务中心 / 仪表盘等）时，本应「静默留白」的 401
      // 被误报为「登录已过期」并强制跳转登录页 → 访客只读浏览功能失效。
      const hadToken = !!localStorage.getItem('token')
      if (hadToken) {
        // 2026-11-03 审查修复（轻微·重复提示）：
        // 标记「这个 401 已由拦截器统一处理」，调用方（如时装页的 notifyFashionError）
        // 据此不再弹第二条同样的「登录已过期」。
        // 只靠「此时 localStorage 里还有没有 token」判断是不行的 —— 下面
        // removeItem 在 reject 之前执行，页面 catch 拿到的永远是「没有 token」。
        ;(error as { authErrorHandled?: boolean }).authErrorHandled = true
      }
      if (hadToken && !sessionExpiredHandled) {
        sessionExpiredHandled = true
        localStorage.removeItem('token')
        // ⚠️ 2026-10-30 审查修复（中等·状态不一致）：
        //   authStore.token 是应用启动时从 localStorage 读入的**内存值**，
        //   只删 localStorage 时内存里仍是「已登录」→ 顶栏继续显示用户名、
        //   侧边栏继续显示管理分组，而此后所有请求都 401（用户无从察觉原因）。
        //   此处同步重置 Pinia；动态 import 规避
        //   client → stores/auth → api/auth → client 的循环依赖。
        void import('@/stores/auth')
          .then(({ useAuthStore }) => { useAuthStore().$reset() })
          .catch(() => { /* 极端情况下忽略：仅影响 UI 同步，不影响跳转登录 */ })
        ElMessage.error('登录已过期，请重新登录')
        router.push('/login')
      }
      // 无 token（访客）：不提示、不跳转，交由调用方静默处理
    }
    return Promise.reject(error)
  }
)

export default client
