<template>
  <div class="fashion-store-page">
    <div class="page-head">
      <div>
        <h2 class="page-title">时装商店</h2>
        <p class="page-subtitle mono">
          浏览站点全部时装 · 免费的直接「加入衣柜」，标价的用 🥭 芒果块购买后自动加入衣柜，穿戴后游戏内 mod 会同步生效
        </p>
      </div>
      <div class="head-actions">
        <!-- 🥭 芒果块余额（与侧边栏共享同一份状态，购买后实时刷新） -->
        <MangoWalletPanel :balance="mangoBalance" :loading="mangoLoading" />
        <el-button size="small" type="primary" plain @click="openRecharge">充值</el-button>
        <el-button size="small" text @click="openDrawer('orders')">购买记录</el-button>
        <el-button size="small" text @click="openDrawer('transactions')">收支明细</el-button>
        <span class="stat-chip mono">{{ filteredCatalog.length }} 件</span>
        <!-- 衣柜件数用「已在衣柜」的条目数：用户一眼就能确认刚才的操作有没有生效 -->
        <span
          class="stat-chip mono"
          :title="loadError ? '商店数据读取失败，点击「刷新」重试' : '已加入衣柜的时装数量'"
        >
          我的衣柜 {{ wardrobeChipText }}
        </span>
        <el-button
          size="small"
          text
          :loading="loading"
          :disabled="loading"
          @click="loadAll"
        >
          刷新
        </el-button>
        <el-button size="small" type="primary" plain @click="goWardrobe()">去我的衣柜</el-button>
      </div>
    </div>

    <el-card shadow="never" class="store-card">
      <template #header>
        <div class="store-toolbar">
          <el-input
            v-model="keyword"
            size="small"
            clearable
            placeholder="搜索名称 / ID"
            aria-label="搜索时装名称或 ID"
            class="store-search"
          />
          <el-radio-group v-model="activeCategory" size="small" class="store-filters" aria-label="按槽位筛选时装">
            <el-radio-button value="all">全部</el-radio-button>
            <el-radio-button v-for="slot in FASHION_SLOTS" :key="slot.value" :value="slot.value">
              {{ slot.label }}
            </el-radio-button>
          </el-radio-group>
        </div>
      </template>

      <div v-loading="loading" class="store-body" :aria-busy="loading">
        <el-alert
          v-if="loadError"
          class="panel-error"
          type="error"
          :closable="false"
          show-icon
          title="时装商店加载失败"
        >
          <template #default>
            <el-button size="small" text type="primary" @click="loadAll">重试</el-button>
          </template>
        </el-alert>

        <el-empty v-else-if="filteredCatalog.length === 0" :description="emptyText" :image-size="80" />

        <template v-else>
          <div class="store-grid" role="list" :aria-label="`时装商店，共 ${filteredCatalog.length} 件`">
            <article
              v-for="item in pagedCatalog"
              :key="item.cosmeticId"
              class="store-item"
              role="listitem"
            >
              <button
                type="button"
                class="item-thumb"
                :aria-label="`查看 ${displayName(item)} 详情`"
                @click="openDetail(item)"
              >
                <img
                  v-if="thumbUrl(item) && !brokenThumbUrls.has(thumbUrl(item))"
                  :src="thumbUrl(item)"
                  :alt="displayName(item)"
                  loading="lazy"
                  fetchpriority="low"
                  decoding="async"
                  @error="markThumbBroken"
                />
                <span v-else class="thumb-fallback">{{ (displayName(item) || '?').slice(0, 1) }}</span>
              </button>
              <div class="item-info">
                <div class="item-name" :title="displayName(item)">{{ displayName(item) }}</div>
                <div class="item-meta mono">
                  <span class="slot-tag">{{ CATEGORY_LABELS[itemSlot(item)] || item.category }}</span>
                  <span v-if="item.packId" class="pack-tag" :title="`时装包：${item.packId}`">
                    {{ item.packId }}
                  </span>
                  <span class="price-tag" :class="{ 'price-free': priceOf(item) === 0 }">
                    {{ priceOf(item) > 0 ? '🥭 ' + priceOf(item) : '免费' }}
                  </span>
                </div>
                <div class="item-actions">
                  <el-button size="small" text class="detail-btn" @click="openDetail(item)">详情</el-button>
                  <StoreItemActions
                    :item="item"
                    variant="card"
                    :adding="actionBusyIds.has(item.cosmeticId)"
                    :purchasing="actionBusyIds.has(item.cosmeticId)"
                    @add="addToWardrobe(item)"
                    @buy="openPurchase(item)"
                    @equip="goWardrobe(item)"
                    @restore="reAddToWardrobe(item)"
                  />
                </div>
              </div>
            </article>
          </div>

          <div v-if="filteredCatalog.length > pageSize" class="store-pager">
            <el-pagination
              layout="prev, pager, next"
              :total="filteredCatalog.length"
              :page-size="pageSize"
              :current-page="page"
              @current-change="page = $event"
            />
          </div>
        </template>
      </div>
    </el-card>

    <!-- 详情弹窗：先看大图 / 元信息，再决定购买 / 加入衣柜 / 去穿戴 -->
    <el-dialog
      v-model="detailOpen"
      :title="detailItem ? displayName(detailItem) : '时装详情'"
      width="min(520px, 92vw)"
      class="detail-dialog"
      destroy-on-close
      @open="onDetailOpen"
      @closed="onDetailClosed"
    >
      <div v-if="detailItem" class="detail-body">
        <!-- 只在真的有预览图时给容器 role=img / aria-label（P3） -->
        <div
          class="detail-preview"
          :role="detailUrl ? 'img' : undefined"
          :aria-label="detailUrl ? `${displayName(detailItem)} 预览图` : undefined"
        >
          <el-image
            v-if="detailUrl"
            :key="detailUrl"
            :src="detailUrl"
            :preview-src-list="[detailUrl]"
            preview-teleported
            hide-on-click-modal
            fit="contain"
            class="detail-image"
            :alt="displayName(detailItem)"
            @show="onViewerShow"
            @close="onViewerClose"
            @error="onDetailImageError"
            @load="onDetailImageLoad"
          >
            <template #placeholder>
              <div class="detail-placeholder">
                <span class="loading-spinner" aria-hidden="true"></span>
                <span>预览加载中…</span>
              </div>
            </template>
            <template #error>
              <div class="detail-image-error">
                <span>预览加载失败</span>
                <el-button size="small" type="primary" @click.stop="retryDetail">重试</el-button>
              </div>
            </template>
          </el-image>
          <span v-else class="detail-fallback">{{ (displayName(detailItem) || '?').slice(0, 1) }}</span>
        </div>
        <p v-if="detailUrl" class="zoom-hint mono">点击图片查看大图（可双指缩放 / 点空白处关闭）</p>
        <p v-if="detailHint" class="detail-hint mono">{{ detailHint }}</p>
        <el-descriptions :column="1" border size="small" class="detail-meta">
          <el-descriptions-item label="ID">
            <span class="mono">{{ detailItem.cosmeticId }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="槽位">
            {{ CATEGORY_LABELS[itemSlot(detailItem)] || detailItem.category || '其它' }}
          </el-descriptions-item>
          <el-descriptions-item label="时装包">
            <span class="mono">{{ detailItem.packId || '—' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="价格">
            <span :class="priceOf(detailItem) > 0 ? 'detail-price' : 'detail-price-free'">
              {{ priceOf(detailItem) > 0 ? '🥭 ' + priceOf(detailItem) : '免费' }}
            </span>
          </el-descriptions-item>
          <el-descriptions-item label="资源大小">
            <span class="mono">{{ formatBytes(detailItem.bytes) }}</span>
          </el-descriptions-item>
        </el-descriptions>
      </div>
      <template #footer>
        <el-button @click="detailOpen = false">关闭</el-button>
        <StoreItemActions
          v-if="detailItem"
          :item="detailItem"
          variant="dialog"
          :adding="actionBusyIds.has(detailItem.cosmeticId)"
          :purchasing="purchasing || actionBusyIds.has(detailItem.cosmeticId)"
          @add="addToWardrobe(detailItem)"
          @buy="openPurchase(detailItem)"
          @equip="goWardrobe(detailItem)"
          @restore="reAddToWardrobe(detailItem)"
        />
      </template>
    </el-dialog>

    <!-- 购买二次确认 -->
    <PurchaseDialog
      v-model="purchaseOpen"
      :item="purchaseItem"
      :balance="mangoBalance ?? 0"
      :submitting="purchasing"
      :error-text="purchaseError"
      @confirm="doPurchase"
      @recharge="onPurchaseRecharge"
    />

    <!-- 充值（本期占位，501 友好提示） -->
    <RechargeDialog v-model="rechargeOpen" />

    <!-- 购买记录 / 收支明细 -->
    <OrderDrawer v-model="drawerOpen" :type="drawerType" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  CATEGORY_LABELS,
  FASHION_SLOTS,
  fashionApi,
  fashionErrorMessage,
  fashionErrorIsInsufficientMango,
  notifyFashionError,
  type PurchaseResponse,
  type StoreItem,
} from '@/api/fashion'
import MangoWalletPanel from '@/components/fashion/MangoWalletPanel.vue'
import PurchaseDialog from '@/components/fashion/PurchaseDialog.vue'
import RechargeDialog from '@/components/fashion/RechargeDialog.vue'
import OrderDrawer from '@/components/fashion/OrderDrawer.vue'
import StoreItemActions from '@/components/fashion/StoreItemActions.vue'
import { useMangoWallet } from '@/components/fashion/useMangoWallet'
import { useResponsivePageSize } from '@/composables/useResponsivePageSize'
import { useScrollLock } from '@/composables/useScrollLock'
import { useFashionImageViewer } from '@/composables/useFashionImageViewer'

const router = useRouter()

// ── 共享钱包状态（与侧边栏同一份；购买后实时刷新）──────────────────────────
const { mangoBalance, mangoLoading, applyStoreWallet, setMangoBalance } = useMangoWallet()
const { lock, unlock } = useScrollLock()
const { onShow: onViewerShow, onClose: onViewerClose } = useFashionImageViewer()

// P1-2：移动端一页少渲染一些全尺寸大图（后端本期无缩略图端点），避免解码 OOM
const pageSize = useResponsivePageSize(48, 12)

// 商店数据源：GET /store/items 一次拿全目录 + 价格 + 拥有 / 衣柜状态 + 钱包
const catalog = ref<StoreItem[]>([])
const keyword = ref('')
const activeCategory = ref('all')
const page = ref(1)
const loading = ref(false)
const loadError = ref(false)
// P2-1：按**失败的 URL**记录破图，而不是按 cosmeticId；assetKey/sha256 变了 URL
// 也会变，旧标记的 URL 自然不影响新预览（旧请求的 error 不会误杀新图）
const brokenThumbUrls = ref<Set<string>>(new Set())

const detailOpen = ref(false)
const detailItem = ref<StoreItem | null>(null)
const detailRetrySeq = ref(0)
const detailImageError = ref(false)

// 正在异步操作（免费加入 / 补回衣柜）的 cosmeticId：给按钮 loading 防连点
const actionBusyIds = ref<Set<string>>(new Set())

// ── 购买 / 充值 / 记录 ────────────────────────────────────────────────────
const purchaseOpen = ref(false)
const purchaseItem = ref<StoreItem | null>(null)
const purchasing = ref(false)
const purchaseError = ref('')
const rechargeOpen = ref(false)
const drawerOpen = ref(false)
const drawerType = ref<'orders' | 'transactions'>('orders')

// 请求竞态保护：连点刷新时只采纳最后一次响应
let storeSeq = 0
let isUnmounted = false

/** 槽位归一化：后端历史脏数据（大小写 / 未知分类）统一落到 other */
function itemSlot(item: StoreItem): string {
  const category = String(item.category || 'other').trim().toLowerCase()
  return FASHION_SLOTS.some((slot) => slot.value === category) ? category : 'other'
}

/** 显示名：name 为空时回落 cosmeticId，避免渲染成空白卡片 */
function displayName(item: StoreItem): string {
  return (item.name || '').trim() || item.cosmeticId
}

function priceOf(item: StoreItem): number {
  const value = Number(item.priceMango)
  return Number.isFinite(value) && value > 0 ? Math.floor(value) : 0
}

function formatBytes(bytes: number): string {
  const value = Number(bytes)
  if (!Number.isFinite(value) || value <= 0) return '—'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

function newRequestId(): string {
  return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

/**
 * 按当前目录重新定位条目（P2-12）。
 * 详情弹窗 / 购买弹窗持有的是旧数组里的对象引用；目录刷新或下架后必须对
 * 「仍在当前数据里」的条目操作，否则会对过期 ID 发请求。
 */
function resolveItem(cosmeticId: string): StoreItem | null {
  return catalog.value.find((item) => item.cosmeticId === cosmeticId) || null
}

function warnStale(): void {
  ElMessage.warning('这件时装已不在当前目录里，请刷新后再试')
}

const filteredCatalog = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return catalog.value.filter((item) => {
    if (activeCategory.value !== 'all' && itemSlot(item) !== activeCategory.value) return false
    if (!kw) return true
    return (
      displayName(item).toLowerCase().includes(kw) ||
      item.cosmeticId.toLowerCase().includes(kw)
    )
  })
})

const pagedCatalog = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return filteredCatalog.value.slice(start, start + pageSize.value)
})

const emptyText = computed(() => {
  const kw = keyword.value.trim()
  if (kw) return `没有匹配「${kw}」的时装`
  return activeCategory.value === 'all'
    ? '目录里还没有时装（站点管理员可在「时装管理」里导入）'
    : '该分类下暂无时装'
})

/** 页头「我的衣柜 N 件」：商店数据没读到时显示「—」，避免把失败渲染成「0 件」 */
const wardrobeChipText = computed(() =>
  loadError.value ? '—' : `${catalog.value.filter((item) => item.inWardrobe).length} 件`
)

// P2-3：预览 URL 带 sha256 版本戳，管理员替换资产后不会被 immutable 缓存卡一年
const previewUrls = computed(() => {
  const map = new Map<string, string>()
  for (const item of catalog.value) {
    if (!item.assetKey) continue
    if (!/\.(png|jpe?g|webp|gif)$/i.test(item.assetKey)) continue
    const url = fashionApi.assetUrl(item.assetKey, item.sha256)
    if (url) map.set(item.cosmeticId, url)
  }
  return map
})

function thumbUrl(item: StoreItem): string {
  return previewUrls.value.get(item.cosmeticId) || ''
}

/** 详情大图 URL；重试时追加时间戳强制重新请求（P2-2） */
const detailUrl = computed(() => {
  const item = detailItem.value
  if (!item) return ''
  const base = previewUrls.value.get(item.cosmeticId) || ''
  if (!base) return ''
  if (detailRetrySeq.value <= 0) return base
  return `${base}${base.includes('?') ? '&' : '?'}t=${detailRetrySeq.value}`
})

/** 区分「没有图片资产」「不是图片格式」「图片加载失败」，不再把 404 说成不是图片 */
const detailHint = computed(() => {
  const item = detailItem.value
  if (!item) return ''
  if (!item.assetKey) return '这件时装还没有上传资产（assetKey 为空）。'
  if (!/\.(png|jpe?g|webp|gif)$/i.test(item.assetKey)) {
    return '该资产不是图片格式（glb / json / zip 等），网页无法预览。'
  }
  if (detailImageError.value) {
    return '预览图加载失败，可点上方「重试」重新加载；若持续失败请联系管理员检查资产文件。'
  }
  return ''
})

/** P2-1：记录真正失败的那个 src，旧请求的 error 不会误杀换了 URL 的新预览 */
function markThumbBroken(event: Event): void {
  const el = event.target as HTMLImageElement | null
  const url = el?.getAttribute('src') || el?.currentSrc || ''
  if (!url || brokenThumbUrls.value.has(url)) return
  brokenThumbUrls.value = new Set(brokenThumbUrls.value).add(url)
}

// 搜索 / 切分类后回到第一页，否则可能停在一个空页上
watch([keyword, activeCategory], () => {
  page.value = 1
})
// 视口变化导致 pageSize 改变时把页码夹回有效范围（P1-2）
watch(pageSize, () => {
  const maxPage = Math.max(1, Math.ceil(filteredCatalog.value.length / pageSize.value))
  if (page.value > maxPage) page.value = maxPage
})

function openDetail(item: StoreItem): void {
  const current = resolveItem(item.cosmeticId)
  if (!current) {
    warnStale()
    return
  }
  detailRetrySeq.value = 0
  detailImageError.value = false
  detailItem.value = current
  detailOpen.value = true
}

function onDetailOpen(): void {
  lock('store-detail')
}

function onDetailClosed(): void {
  unlock('store-detail')
  // P2-12 / P3：关闭后清掉引用，避免对着过期条目操作
  detailItem.value = null
  detailRetrySeq.value = 0
  detailImageError.value = false
}

function onDetailImageError(): void {
  detailImageError.value = true
}

function onDetailImageLoad(): void {
  detailImageError.value = false
}

function retryDetail(): void {
  detailImageError.value = false
  detailRetrySeq.value = Date.now()
}

/** 目录刷新后让详情弹窗跟着换成新数组里的同一个条目（或关掉） */
function syncDetailWithCatalog(): void {
  const current = detailItem.value
  if (!current) return
  const fresh = resolveItem(current.cosmeticId)
  if (fresh) {
    detailItem.value = fresh
    return
  }
  detailItem.value = null
  detailOpen.value = false
}

/**
 * 去衣柜穿戴。
 * 带上 cosmeticId 让衣柜能定位到这一件（只是提示，不强制穿戴）。
 */
function goWardrobe(item?: StoreItem): void {
  if (item && !resolveItem(item.cosmeticId)) {
    warnStale()
    return
  }
  const query = item ? { cosmeticId: item.cosmeticId } : {}
  void router.push({ path: '/fashion/wardrobe', query })
}

/**
 * 免费时装「加入衣柜」（沿用原逻辑，调用 POST /wardrobe）。
 * 不做乐观切换：失败只弹错误、卡片保持原样，后端幂等重复点也不会写坏数据。
 */
async function addToWardrobe(item: StoreItem): Promise<void> {
  const current = resolveItem(item.cosmeticId)
  if (!current) {
    warnStale()
    return
  }
  const cosmeticId = current.cosmeticId
  if (actionBusyIds.value.has(cosmeticId)) return
  actionBusyIds.value = new Set(actionBusyIds.value).add(cosmeticId)
  try {
    const { data } = await fashionApi.addToWardrobe(cosmeticId)
    if (isUnmounted) return
    // 幂等：added=false 表示本来就在（例如另一个标签页刚加过）
    const idx = catalog.value.findIndex((row) => row.cosmeticId === cosmeticId)
    if (idx >= 0) catalog.value[idx] = { ...catalog.value[idx], inWardrobe: true }
    if (data.added) ElMessage.success(`已把「${displayName(current)}」加入衣柜`)
    else ElMessage.info(`「${displayName(current)}」已经在你的衣柜里了`)
  } catch (error) {
    // 付费未拥有时后端会返回 403 purchase_required，统一走中文提示（402/403/501 同理）
    notifyFashionError(error, '加入衣柜失败')
  } finally {
    const rest = new Set(actionBusyIds.value)
    rest.delete(cosmeticId)
    actionBusyIds.value = rest
  }
}

function openPurchase(item: StoreItem): void {
  const current = resolveItem(item.cosmeticId)
  if (!current) {
    warnStale()
    return
  }
  purchaseItem.value = current
  purchaseError.value = ''
  purchaseOpen.value = true
}

function onPurchaseRecharge(): void {
  purchaseOpen.value = false
  rechargeOpen.value = true
}

/** 把购买结果同步回目录条目与共享余额（任务 A2：成功后更新余额 / owned / 衣柜集合） */
function applyPurchaseResult(data: PurchaseResponse, item: StoreItem): void {
  const fresh: StoreItem = data.item || {
    ...item,
    owned: true,
    inWardrobe: true,
    priceMango: data.priceMango || item.priceMango,
  }
  const idx = catalog.value.findIndex((row) => row.cosmeticId === item.cosmeticId)
  if (idx >= 0) catalog.value.splice(idx, 1, fresh)
  setMangoBalance(data.balance)
  syncDetailWithCatalog()
}

async function doPurchase(clientRequestId: string): Promise<void> {
  const item = purchaseItem.value
  if (!item || purchasing.value) return
  const current = resolveItem(item.cosmeticId)
  if (!current) {
    purchaseError.value = '这件时装已不在当前目录里，请关闭后刷新重试'
    return
  }
  purchasing.value = true
  purchaseError.value = ''
  try {
    const { data } = await fashionApi.purchase({
      cosmeticId: current.cosmeticId,
      clientRequestId,
    })
    if (isUnmounted) return
    applyPurchaseResult(data, current)
    purchaseOpen.value = false
    purchaseItem.value = null
    if (data.alreadyOwned) {
      ElMessage.info(`「${displayName(current)}」你已经拥有了，已确保在衣柜里（不扣款）`)
    } else if (data.free) {
      ElMessage.success(`已免费领取「${displayName(current)}」并加入衣柜`)
    } else {
      ElMessage.success(
        `购买成功！已扣除 🥭 ${data.priceMango}，剩余 🥭 ${data.balance}；已自动加入衣柜`
      )
    }
  } catch (error) {
    if (isUnmounted) return
    // 402 余额不足 / 409 / 429 / 冲突：展示后端中文 message，弹窗内也保留一份
    const message = fashionErrorMessage(error, '购买失败')
    purchaseError.value = fashionErrorIsInsufficientMango(error)
      ? `${message}。充值通道暂未开放，可点「去充值」查看说明，或联系管理员发放后再买。`
      : message
    notifyFashionError(error, '购买失败')
  } finally {
    purchasing.value = false
  }
}

/**
 * 已拥有但被移出衣柜：点「补回衣柜」走幂等 purchase（alreadyOwned=true 不扣款），
 * 后端会确保该时装重新出现在衣柜里（契约 §4.2）。
 */
async function reAddToWardrobe(item: StoreItem): Promise<void> {
  const current = resolveItem(item.cosmeticId)
  if (!current) {
    warnStale()
    return
  }
  const cosmeticId = current.cosmeticId
  if (actionBusyIds.value.has(cosmeticId)) return
  actionBusyIds.value = new Set(actionBusyIds.value).add(cosmeticId)
  try {
    const { data } = await fashionApi.purchase({ cosmeticId, clientRequestId: newRequestId() })
    if (isUnmounted) return
    applyPurchaseResult(data, current)
    ElMessage.success(`已把「${displayName(current)}」补回衣柜（已拥有，不扣款）`)
  } catch (error) {
    notifyFashionError(error, '补回衣柜失败')
  } finally {
    const rest = new Set(actionBusyIds.value)
    rest.delete(cosmeticId)
    actionBusyIds.value = rest
  }
}

function openRecharge(): void {
  rechargeOpen.value = true
}

function openDrawer(type: 'orders' | 'transactions'): void {
  drawerType.value = type
  drawerOpen.value = true
}

/** 商店页唯一的一次请求：目录 + 价格 + 拥有 / 衣柜状态 + 钱包 + 充值开关 */
async function loadStore(): Promise<void> {
  const seq = ++storeSeq
  loading.value = true
  loadError.value = false
  try {
    const { data } = await fashionApi.storeItems()
    if (isUnmounted || seq !== storeSeq) return
    catalog.value = data.items || []
    applyStoreWallet(data.wallet, data.rechargeAvailable)
    // 目录重拉成功 = 资产可能已补齐，全部缩略图重试一次（P2-1）
    brokenThumbUrls.value = new Set()
    const maxPage = Math.max(1, Math.ceil(filteredCatalog.value.length / pageSize.value))
    if (page.value > maxPage) page.value = maxPage
    syncDetailWithCatalog()
  } catch (error) {
    if (isUnmounted || seq !== storeSeq) return
    loadError.value = true
    notifyFashionError(error, '读取时装商店失败')
  } finally {
    if (!isUnmounted && seq === storeSeq) loading.value = false
  }
}

/** 挂载与「刷新」都走这里（原先并行读 catalog + wardrobe，现在一次拿全） */
async function loadAll(): Promise<void> {
  await loadStore()
}

onMounted(loadAll)

onUnmounted(() => {
  // P3：路由切走后不再写状态、不再弹 toast
  isUnmounted = true
  ++storeSeq
})
</script>

<style scoped>
/* 与主站 / 衣柜页共用同一套 Token 与排版，保证「时装中心风格和主站一致」 */
.fashion-store-page {
  padding: var(--space-4);
  max-width: 1320px;
  margin: 0 auto;
}

.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--space-4);
  margin-bottom: var(--space-4);
  flex-wrap: wrap;
}

.page-title {
  font-family: var(--font-display);
  font-size: 26px;
  color: var(--text-primary);
  margin: 0 0 var(--space-2);
}

.page-subtitle {
  color: var(--text-secondary);
  font-size: 13px;
  margin: 0;
  max-width: 720px;
  line-height: 1.7;
}

.head-actions {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.stat-chip {
  font-size: 12px;
  color: var(--text-secondary);
  padding: 4px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  background: var(--bg-elevated);
}

.store-card {
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
}

.store-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.store-search {
  width: 220px;
  max-width: 100%;
}

.store-filters {
  display: flex;
  flex-wrap: wrap;
  row-gap: var(--space-2);
}

.store-toolbar :deep(.el-radio-group) {
  flex-wrap: wrap;
  row-gap: var(--space-2);
}

.store-body {
  min-height: 240px;
}

.panel-error {
  margin-bottom: var(--space-3);
}

.store-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(168px, 1fr));
  gap: var(--space-3);
}

.store-item {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--bg-elevated);
  padding: var(--space-2);
  transition: border-color 150ms var(--ease-out), transform 150ms var(--ease-out);
  min-width: 0;
}

@media (hover: hover) {
  .store-item:hover {
    border-color: var(--accent-active);
    transform: translateY(-2px);
  }
}

.item-thumb {
  height: 104px;
  border: none;
  border-radius: var(--radius-md);
  background: var(--bg-hover);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  margin-bottom: var(--space-2);
  cursor: pointer;
  padding: 0;
  touch-action: manipulation;
}

.item-thumb:focus-visible {
  outline: 2px solid var(--accent-active);
  outline-offset: 2px;
}

.item-thumb img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}

.thumb-fallback {
  font-family: var(--font-display);
  font-size: 30px;
  color: var(--text-secondary);
}

.item-info {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1;
  min-width: 0;
}

.item-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.item-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--text-secondary);
  min-height: 18px;
  flex-wrap: wrap;
}

.slot-tag {
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--accent-soft);
  color: var(--accent);
}

.pack-tag {
  max-width: 96px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-muted);
}

.price-tag {
  margin-left: auto;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
  white-space: nowrap;
}

.price-tag.price-free {
  background: var(--success-bg);
  color: var(--success);
  font-weight: 500;
}

.item-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 4px;
  margin-top: auto;
  padding-top: 2px;
  flex-wrap: wrap;
  min-width: 0;
}

.detail-btn {
  color: var(--text-secondary);
}

@media (hover: hover) {
  .detail-btn:hover {
    color: var(--text-primary);
  }
}

.store-pager {
  display: flex;
  justify-content: center;
  margin-top: var(--space-4);
}

/* ---- 详情弹窗 ---- */
.detail-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.detail-preview {
  height: 220px;
  border-radius: var(--radius-md);
  background: var(--bg-hover);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}

.detail-image {
  max-width: 100%;
  max-height: 100%;
  cursor: zoom-in;
}

.detail-fallback {
  font-family: var(--font-display);
  font-size: 56px;
  color: var(--text-secondary);
}

.detail-placeholder,
.detail-image-error {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  width: 100%;
  height: 100%;
  min-height: 120px;
  color: var(--text-secondary);
  font-size: 13px;
}

.loading-spinner {
  width: 22px;
  height: 22px;
  border: 2px solid var(--border-subtle);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: fashion-spin 0.8s linear infinite;
}

@keyframes fashion-spin {
  to { transform: rotate(360deg); }
}

.zoom-hint {
  margin: 0;
  font-size: 11.5px;
  color: var(--text-muted);
  text-align: center;
}

.detail-hint {
  font-size: 12px;
  color: var(--text-secondary);
  margin: 0;
  line-height: 1.7;
}

.detail-meta {
  margin-top: var(--space-2);
}

.detail-price {
  color: var(--accent);
  font-weight: 600;
}

.detail-price-free {
  color: var(--success);
}

@media (max-width: 768px) {
  .fashion-store-page {
    padding: var(--space-2);
  }

  .page-title {
    font-size: 22px;
  }

  .store-search {
    width: 100%;
  }

  .store-grid {
    grid-template-columns: repeat(auto-fill, minmax(132px, 1fr));
  }

  .item-thumb {
    height: 88px;
  }

  /* 弹窗内大图在窄屏 / 横屏收缩，别把整个弹窗占满 */
  .detail-preview {
    height: min(200px, 32dvh);
  }
}

@media (max-height: 480px) {
  .detail-preview {
    height: 120px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .store-item { transition: none; }
  .loading-spinner { animation: none; }
}
</style>
