/**
 * 时装中心共享的「芒果块钱包」状态（任务 A4 / 侧边栏余额）。
 *
 * 为什么是模块级单例：
 * - `FashionSidebar` 挂在 AppLayout 上，所有时装页共用**同一个实例**；它只在自己
 *   onMounted 时读一次 `wallet()`，切换时装路由不会重新发请求。商店页改用
 *   `storeItems()`（一次拿全，余额内联在响应里），不再单独调 `wallet()`，避免 N+1。
 * - 商店页把 `storeItems()` 里的钱包写回这里（`applyStoreWallet`）、购买成功后写回
 *   新余额（`setMangoBalance`），侧边栏数字会**实时跟着变**，无需重新挂载。
 * - `ensureWallet()` 有「已读就不重复请求 + 在飞请求复用 + seq 竞态」三重保护：
 *   多个组件同时要余额时只发一个请求，旧响应不会覆盖新值。
 * - 会话切换（登录 / 换账号）时按 token 变化自动作废旧余额，避免把上一个账号的
 *   数字显示给新账号。
 */
import { ref } from 'vue'
import { fashionApi, type WalletOut } from '@/api/fashion'

// ── 模块级共享状态（所有 useMangoWallet() 调用方看到同一份）────────────────
const mangoBalance = ref<number | null>(null)
const mangoWallet = ref<WalletOut | null>(null)
const mangoLoading = ref(false)
const mangoRechargeAvailable = ref(false)

/** 竞态序号：只采纳最后一次请求的结果 */
let seq = 0
/** 在飞的 wallet() 请求（去重） */
let inFlight: Promise<void> | null = null
/** 这份余额属于哪个 token；变了就说明会话切换，要重读 */
let boundToken: string | null = null

function currentToken(): string {
  try {
    return localStorage.getItem('token') || ''
  } catch {
    return ''
  }
}

function setWallet(wallet: WalletOut, rechargeAvailable?: boolean): void {
  mangoWallet.value = wallet
  mangoBalance.value = Number.isFinite(wallet.balance) ? Number(wallet.balance) : 0
  boundToken = currentToken()
  if (typeof rechargeAvailable === 'boolean') {
    mangoRechargeAvailable.value = rechargeAvailable
  }
}

/** 会话变化时丢弃旧余额（下次 ensureWallet 会重新读） */
function ensureSession(token: string): void {
  if (boundToken === token) return
  boundToken = token
  seq += 1
  inFlight = null
  mangoBalance.value = null
  mangoWallet.value = null
  mangoLoading.value = false
  mangoRechargeAvailable.value = false
}

export function useMangoWallet() {
  /**
   * 读我的钱包余额（静默降级）。
   *
   * 失败**不弹任何提示**、不阻塞导航：侧边栏只是个余额展示，读不到就显示「—」，
   * 商店页自己会用 `storeItems()` 再拿一次并在错误时给可重试的提示。
   */
  function ensureWallet(): Promise<void> {
    const token = currentToken()
    ensureSession(token)
    // 已经有值（storeItems 已写入 / 之前读过）→ 切路由不再重复并发请求
    if (mangoBalance.value !== null) return Promise.resolve()
    // 已有在飞请求 → 复用它，不另发一个
    if (inFlight) return inFlight

    const mySeq = ++seq
    mangoLoading.value = true
    inFlight = (async () => {
      try {
        const { data } = await fashionApi.wallet()
        if (mySeq !== seq) return // 已被更新的请求 / 会话取代
        setWallet(data)
      } catch {
        // 静默降级：余额保持 null（UI 显示「—」），下次进入会重试；
        // 绝不因为一个余额请求把整个导航/页面打成错误态
      } finally {
        if (mySeq === seq) {
          mangoLoading.value = false
          inFlight = null
        }
      }
    })()
    return inFlight
  }

  /**
   * 商店页 `storeItems()` 成功后回填：钱包 + 充值开关。
   * 会作废旧在飞的 `wallet()` 请求（seq++），避免旧响应覆盖新值。
   */
  function applyStoreWallet(wallet: WalletOut, rechargeAvailable: boolean): void {
    const token = currentToken()
    ensureSession(token)
    seq += 1
    inFlight = null
    mangoLoading.value = false
    setWallet(wallet, rechargeAvailable)
  }

  /** 购买 / 充值成功后的新余额：同步更新共享状态，侧边栏立即刷新 */
  function setMangoBalance(balance: number): void {
    const value = Number.isFinite(balance) ? Number(balance) : 0
    seq += 1
    inFlight = null
    mangoLoading.value = false
    boundToken = currentToken()
    mangoBalance.value = value
    if (mangoWallet.value) {
      mangoWallet.value = { ...mangoWallet.value, balance: value }
    }
  }

  return {
    mangoBalance,
    mangoWallet,
    mangoLoading,
    mangoRechargeAvailable,
    ensureWallet,
    applyStoreWallet,
    setMangoBalance,
  }
}
