/**
 * 时装中心浮层滚动锁（P2-6）。
 *
 * 背景：EP 的 dialog / drawer 只给 `body` 加 `el-popup-parent--hidden` 类
 *（`overflow:hidden`），但本项目真正的滚动容器是 `AppLayout` 里的 `.app-main`
 *（`overflow-y:auto`）。iOS Safari 上 `body{overflow:hidden}` 拦不住嵌套滚动容器，
 * 背景仍可滚 / 橡皮筋；且 EP 用 class、抽屉用 inline style，彼此互不知晓。
 *
 * 解决：模块级计数同时作用于 `body` 与 `.app-main`，多个浮层（详情弹窗 + 全屏大图
 * + 抽屉）叠加时只在最后一个关闭后恢复；`onUnmounted` 强制释放本实例持有的锁，
 * 避免路由切走后把 `.app-main` 永久锁死。
 *
 * 用 `key` 区分同一实例里的多个浮层：`lock('detail')` 与 `lock('viewer')` 各计一次，
 * 关闭大图不会误把仍打开的详情弹窗的锁也解掉。
 */
import { onUnmounted } from 'vue'

interface SavedStyle {
  body: string
  main: string
  mainOverscroll: string
  mainPaddingRight: string
}

// 模块级：所有 useScrollLock() 实例共享同一份计数与「解锁前」快照
let lockCount = 0
let saved: SavedStyle | null = null

function mainEl(): HTMLElement | null {
  if (typeof document === 'undefined') return null
  return document.querySelector<HTMLElement>('.app-main')
}

function applyLock(): void {
  if (typeof document === 'undefined') return
  if (lockCount === 0) {
    const main = mainEl()
    saved = {
      body: document.body.style.overflow,
      main: main?.style.overflow ?? '',
      mainOverscroll: main?.style.overscrollBehavior ?? '',
      mainPaddingRight: main?.style.paddingRight ?? '',
    }
    // EP 已用 class 处理 body；这里再补一层 inline（可安全还原，EP 不动 inline overflow）
    document.body.style.overflow = 'hidden'
    if (main) {
      main.style.overflow = 'hidden'
      main.style.overscrollBehavior = 'contain'
    }
  }
  lockCount += 1
}

function releaseLock(): void {
  if (typeof document === 'undefined') return
  lockCount = Math.max(0, lockCount - 1)
  if (lockCount === 0 && saved) {
    document.body.style.overflow = saved.body
    const main = mainEl()
    if (main) {
      main.style.overflow = saved.main
      main.style.overscrollBehavior = saved.mainOverscroll
      main.style.paddingRight = saved.mainPaddingRight
    }
    saved = null
  }
}

/** 在组件 setup 中调用；返回按 key 计数的 lock / unlock。 */
export function useScrollLock() {
  const held = new Set<string>()

  function lock(key = 'default'): void {
    if (held.has(key)) return
    held.add(key)
    applyLock()
  }

  function unlock(key = 'default'): void {
    if (!held.delete(key)) return
    releaseLock()
  }

  // 组件卸载（例如路由切走）时强制释放，避免把 .app-main 永久锁死
  onUnmounted(() => {
    for (const key of Array.from(held)) unlock(key)
  })

  return { lock, unlock }
}
