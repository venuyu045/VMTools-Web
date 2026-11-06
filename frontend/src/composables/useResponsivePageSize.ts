/**
 * 按视口宽度自适应的分页大小（P1-2）。
 *
 * 背景：单资产上限 64MB、允许 4096² PNG，后端本期没有缩略图端点（无图像库）。
 * 一页铺 48 张全尺寸大图在移动端极易解码 OOM。桌面保持原分页，≤768px 时
 * 用调用方给的较小值，靠 `loading="lazy"` + `fetchpriority="low"` 进一步压并发解码。
 *
 * 用 matchMedia（而不是 resize 事件）避免抖动；监听 change 后响应式更新，
 * 调用方 watch 返回值把当前页码夹回有效范围。`onUnmounted` 时移除监听。
 */
import { onUnmounted, ref, type Ref } from 'vue'

const MOBILE_QUERY = '(max-width: 768px)'

export function useResponsivePageSize(desktop: number, mobile: number): Ref<number> {
  const size = ref(desktop)
  let mql: MediaQueryList | null = null

  const update = (): void => {
    size.value = mql && mql.matches ? mobile : desktop
  }

  if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
    mql = window.matchMedia(MOBILE_QUERY)
    update()
    type LegacyMQL = MediaQueryList & {
      addListener?: (cb: () => void) => void
      removeListener?: (cb: () => void) => void
    }
    const legacy = mql as LegacyMQL
    if (typeof mql.addEventListener === 'function') {
      mql.addEventListener('change', update)
    } else if (typeof legacy.addListener === 'function') {
      legacy.addListener(update)
    }
  }

  onUnmounted(() => {
    if (!mql) return
    type LegacyMQL = MediaQueryList & {
      addListener?: (cb: () => void) => void
      removeListener?: (cb: () => void) => void
    }
    const legacy = mql as LegacyMQL
    if (typeof mql.removeEventListener === 'function') {
      mql.removeEventListener('change', update)
    } else if (typeof legacy.removeListener === 'function') {
      legacy.removeListener(update)
    }
    mql = null
  })

  return size
}
