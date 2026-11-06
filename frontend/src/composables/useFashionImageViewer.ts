/**
 * 时装页 `el-image` 全屏大图（ElImageViewer）统一行为（P1-1 / P2-6 / P2-9）。
 *
 * 用法（每个含 `preview-src-list` 的 el-image 都挂同一组回调）：
 * ```
 * const { onShow, onClose } = useFashionImageViewer()
 * <el-image ... preview-teleported hide-on-click-modal @show="onShow" @close="onClose" />
 * ```
 *
 * 解决的问题：
 * 1. **Android 返回键**：viewer 打开时 `history.pushState` 压一条标记，返回键触发
 *    `popstate` → 关掉大图而不是让 vue-router 直接路由回退；正常关闭时把这条
 *    history 消费掉（`history.back()`，监听已移除，不会二次触发）。
 *    pushState 会**保留 vue-router 的 history.state**（含 position），前进/后退 delta
 *    恒为 0，不会触发 vue-router 重复导航或 `go(-1)`。
 * 2. **嵌套滚动容器**：viewer 打开时锁 `body` 与 `.app-main`（见 useScrollLock），
 *    关闭/卸载恢复。
 * 3. **横竖屏切换 transform 残留**：`resize`/`orientationchange` 时点两下 viewer 的
 *    「模式切换」按钮（内部 `toggleMode()` 会 `reset()` 变换），净效果回到原模式、
 *    但缩放/旋转/位移被清零。EP 2.14.2 未把 reset() 暴露到 el-image，这是不改动
 *    EP 源码、不改既有 viewer teleport 层级的前提下最稳妥的做法。
 */
import { onUnmounted } from 'vue'
import { useScrollLock } from './useScrollLock'

/** viewer 关闭按钮：teleport 到 body，用 querySelector 就能拿到 */
function closeViewerButton(): HTMLElement | null {
  return document.querySelector<HTMLElement>('.el-image-viewer__close')
}

/** viewer 工具条第 3 个图标 = 模式切换（toggleMode，内部调用 reset()） */
function viewerModeButton(): HTMLElement | null {
  const icons = document.querySelectorAll<HTMLElement>(
    '.el-image-viewer__actions__inner .el-icon'
  )
  return icons.length >= 3 ? icons[2] : null
}

function viewerIsOpen(): boolean {
  return !!document.querySelector('.el-image-viewer__wrapper')
}

export function useFashionImageViewer() {
  const { lock, unlock } = useScrollLock()

  // 是否有一条我们自己 push 的、仍留在 history 栈上的标记
  let pushed = false
  let alive = true
  // resize 会连续触发，双点 reset 需要节流，避免横竖屏切换时来回闪
  let viewportTimer: ReturnType<typeof setTimeout> | undefined

  function detach(): void {
    window.removeEventListener('popstate', handlePop)
    window.removeEventListener('resize', handleViewportChange)
    window.removeEventListener('orientationchange', handleViewportChange)
    if (viewportTimer) {
      clearTimeout(viewportTimer)
      viewportTimer = undefined
    }
  }

  function handlePop(): void {
    // 返回键已经把 push 的那条弹出去了：先复位，避免 onClose 再 back 一次
    pushed = false
    window.removeEventListener('popstate', handlePop)
    // 点关闭按钮 → 触发 el-image 的 @close → onClose（此时不再 back）
    closeViewerButton()?.click()
  }

  function resetViewerTransform(): void {
    if (!viewerIsOpen()) return
    const btn = viewerModeButton()
    if (!btn) return
    // 两次 toggleMode：净模式不变，但每次都调用 viewer 内部的 reset()
    btn.click()
    btn.click()
  }

  function handleViewportChange(): void {
    if (!alive) return
    if (viewportTimer) clearTimeout(viewportTimer)
    viewportTimer = setTimeout(() => {
      viewportTimer = undefined
      resetViewerTransform()
    }, 150)
  }

  function onShow(): void {
    if (!alive || pushed) return
    lock('fashion-image-viewer')
    try {
      // 保留 vue-router 的 history.state（position/back/current/...），只加一个标记
      const prev = (window.history.state as Record<string, unknown> | null) || {}
      window.history.pushState({ ...prev, fashionPreview: 1 }, '')
      pushed = true
      window.addEventListener('popstate', handlePop)
      window.addEventListener('resize', handleViewportChange)
      window.addEventListener('orientationchange', handleViewportChange)
    } catch {
      // 极少数 WebView / 隐私模式禁用 pushState：降级为「只靠遮罩 / ✕ / ESC 关闭」，
      // 不影响 viewer 本身（hide-on-click-modal 已在模板上开启）
      pushed = false
    }
  }

  function onClose(): void {
    detach()
    unlock('fashion-image-viewer')
    if (pushed) {
      pushed = false
      // 消费 push 的那条：监听已移除，popstate 不会再进 handlePop；
      // URL 未变且保留了 vue-router 的 position → 不会触发真实路由跳转
      try {
        if ((window.history.state as { fashionPreview?: number } | null)?.fashionPreview) {
          window.history.back()
        }
      } catch {
        /* 忽略：历史栈异常时不强求清理 */
      }
    }
  }

  onUnmounted(() => {
    alive = false
    detach()
    unlock('fashion-image-viewer')
    // 卸载（例如路由切走）时**不**调用 history.back()：可能与导致卸载的
    // router.push 打架。留下一条无害的标记条目，下次返回键只会消费它、不改 URL。
    pushed = false
  })

  return { onShow, onClose }
}
