<template>
  <el-drawer
    :model-value="modelValue"
    direction="rtl"
    size="min(520px, 92vw)"
    class="order-drawer"
    :with-header="false"
    append-to-body
    @open="onOpen"
    @closed="onClosed"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="drawer-inner">
      <header class="drawer-head">
        <h3 class="drawer-title">{{ title }}</h3>
        <button type="button" class="drawer-close" aria-label="关闭" @click="close">✕</button>
      </header>

      <div v-loading="loading" class="drawer-scroll" :aria-busy="loading">
        <el-alert
          v-if="loadError"
          class="drawer-error"
          type="error"
          :closable="false"
          show-icon
          :title="title + '加载失败'"
        >
          <template #default>
            <el-button size="small" text type="primary" @click="reload">重试</el-button>
          </template>
        </el-alert>

        <el-empty v-else-if="items.length === 0" :description="emptyText" :image-size="72" />

        <!-- 购买记录 -->
        <template v-else-if="type === 'orders'">
          <ul class="record-list" :aria-label="title">
            <li v-for="row in orderRows" :key="row.orderNo + row.cosmeticId + row.acquiredAt" class="record-row">
              <div class="record-main">
                <span class="record-name">{{ row.name || row.cosmeticId }}</span>
                <span v-if="row.missing" class="record-tag tag-danger">已下架</span>
              </div>
              <div class="record-id mono">{{ row.cosmeticId }}</div>
              <div class="record-foot">
                <span class="record-price" :class="{ free: row.pricePaid === 0 }">
                  {{ row.pricePaid > 0 ? '🥭 ' + row.pricePaid : '免费' }}
                </span>
                <span class="record-source">{{ sourceLabel(row.source) }}</span>
                <span class="record-time mono">{{ fmtApiTime(row.acquiredAt) }}</span>
              </div>
            </li>
          </ul>
        </template>

        <!-- 收支明细 -->
        <template v-else>
          <ul class="record-list" :aria-label="title">
            <li v-for="row in txRows" :key="row.id" class="record-row">
              <div class="record-main">
                <span class="record-name">{{ txTypeLabel(row.type) }}</span>
                <span class="record-delta mono" :class="row.delta >= 0 ? 'is-plus' : 'is-minus'">
                  {{ row.delta >= 0 ? '+' : '' }}{{ row.delta }}
                </span>
              </div>
              <div v-if="row.cosmeticName || row.note" class="record-id mono">
                {{ row.cosmeticName || row.cosmeticId }}{{ row.note ? ' · ' + row.note : '' }}
              </div>
              <div class="record-foot">
                <span class="record-source">余额 🥭 {{ row.balanceAfter }}</span>
                <span class="record-time mono">{{ fmtApiTime(row.createdAt) }}</span>
              </div>
            </li>
          </ul>
        </template>

        <div v-if="items.length > 0 && items.length < total" class="load-more">
          <el-button :loading="loadingMore" :disabled="loadingMore" @click="loadMore">
            加载更多（还有 {{ total - items.length }} 条）
          </el-button>
        </div>
      </div>
    </div>
  </el-drawer>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import {
  fashionApi,
  notifyFashionError,
  type PurchaseOrderItem,
  type WalletTransactionItem,
} from '@/api/fashion'
import { fmtApiTime } from '@/utils/time'
import { useScrollLock } from '@/composables/useScrollLock'

/**
 * 「购买记录」/「收支明细」抽屉（任务 A2）。
 * 用可滚动的**列表**而不是 el-table：移动端不横向溢出、行高与触控区友好。
 * `type` 切换数据源（GET /store/orders 或 GET /wallet/transactions），分页加载更多。
 */
const props = defineProps<{
  modelValue: boolean
  type: 'orders' | 'transactions'
}>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

const { lock, unlock } = useScrollLock()

const LIMIT = 20
const orderRows = ref<PurchaseOrderItem[]>([])
const txRows = ref<WalletTransactionItem[]>([])
const total = ref(0)
const loading = ref(false)
const loadingMore = ref(false)
const loadError = ref(false)

const title = computed(() => (props.type === 'orders' ? '购买记录' : '收支明细'))
const emptyText = computed(() =>
  props.type === 'orders' ? '还没有购买 / 获得过时装' : '还没有芒果块收支记录'
)
const items = computed<unknown[]>(() =>
  props.type === 'orders' ? orderRows.value : txRows.value
)

// 竞态 + 卸载保护：关闭 / 切页后旧响应不得写状态或弹提示
let seq = 0
let isUnmounted = false

const SOURCE_LABELS: Record<string, string> = {
  purchase: '购买',
  free_claim: '免费领取',
  legacy_wardrobe: '历史衣柜',
  admin_grant: '管理员发放',
  migration: '迁移',
}
const TX_TYPE_LABELS: Record<string, string> = {
  purchase: '购买时装',
  refund: '退款',
  admin_adjust: '管理员调整',
  recharge: '充值',
  grant: '发放',
}

function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] || source || '—'
}
function txTypeLabel(type: string): string {
  return TX_TYPE_LABELS[type] || type || '—'
}

async function fetchPage(offset: number, append: boolean): Promise<void> {
  const mySeq = ++seq
  if (append) loadingMore.value = true
  else loading.value = true
  loadError.value = false
  try {
    if (props.type === 'orders') {
      const { data } = await fashionApi.orders({ limit: LIMIT, offset })
      if (isUnmounted || mySeq !== seq) return
      orderRows.value = append ? [...orderRows.value, ...(data.items || [])] : data.items || []
      txRows.value = []
      total.value = data.total || 0
    } else {
      const { data } = await fashionApi.walletTransactions({ limit: LIMIT, offset })
      if (isUnmounted || mySeq !== seq) return
      txRows.value = append ? [...txRows.value, ...(data.items || [])] : data.items || []
      orderRows.value = []
      total.value = data.total || 0
    }
  } catch (error) {
    if (isUnmounted || mySeq !== seq) return
    loadError.value = true
    notifyFashionError(error, title.value + '加载失败')
  } finally {
    if (!isUnmounted && mySeq === seq) {
      loading.value = false
      loadingMore.value = false
    }
  }
}

function reload(): void {
  void fetchPage(0, false)
}

function loadMore(): void {
  if (loading.value || loadingMore.value) return
  void fetchPage(items.value.length, true)
}

function onOpen(): void {
  lock('order-drawer')
  reload()
}

function onClosed(): void {
  unlock('order-drawer')
}

function close(): void {
  emit('update:modelValue', false)
}

// 同一个抽屉复用在「购买记录 / 收支明细」之间切换时，切类型即重新拉第一页
watch(
  () => props.type,
  () => {
    if (props.modelValue) {
      lock('order-drawer')
      reload()
    }
  }
)

onUnmounted(() => {
  isUnmounted = true
  ++seq
  unlock('order-drawer')
})
</script>

<style scoped>
.drawer-inner {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.drawer-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  padding-top: calc(var(--space-3) + env(safe-area-inset-top, 0px));
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}

.drawer-title {
  margin: 0;
  font-family: var(--font-display);
  font-size: 18px;
  color: var(--text-primary);
}

.drawer-close {
  width: 44px;
  height: 44px;
  min-width: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: none;
  color: var(--text-secondary);
  font-size: 16px;
  cursor: pointer;
  border-radius: var(--radius-md);
  touch-action: manipulation;
}

.drawer-close:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.drawer-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  overscroll-behavior: contain;
  padding: var(--space-3) var(--space-4);
  padding-bottom: calc(var(--space-4) + env(safe-area-inset-bottom, 0px));
}

.drawer-error {
  margin-bottom: var(--space-3);
}

.record-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.record-row {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--bg-elevated);
  padding: var(--space-3);
  min-width: 0;
}

.record-main {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-2);
}

.record-name {
  font-size: 14px;
  font-weight: 500;
  color: var(--text-primary);
  overflow-wrap: anywhere;
  min-width: 0;
}

.record-id {
  margin-top: 2px;
  font-size: 11px;
  color: var(--text-muted);
  overflow-wrap: anywhere;
}

.record-foot {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-top: var(--space-2);
  font-size: 12px;
  color: var(--text-secondary);
}

.record-price {
  color: var(--accent);
  font-weight: 600;
}

.record-price.free {
  color: var(--success);
  font-weight: 500;
}

.record-delta {
  font-size: 15px;
  font-weight: 600;
  white-space: nowrap;
}

.record-delta.is-plus {
  color: var(--success);
}

.record-delta.is-minus {
  color: var(--danger);
}

.record-tag {
  font-size: 11px;
  padding: 0 6px;
  border-radius: 999px;
  white-space: nowrap;
}

.tag-danger {
  color: var(--danger);
  background: var(--danger-bg);
}

.record-source {
  color: var(--text-muted);
}

.record-time {
  margin-left: auto;
  color: var(--text-muted);
  white-space: nowrap;
}

.load-more {
  display: flex;
  justify-content: center;
  padding-top: var(--space-3);
}

@media (max-width: 480px) {
  .drawer-head {
    padding-left: var(--space-3);
    padding-right: var(--space-3);
  }
  .drawer-scroll {
    padding-left: var(--space-3);
    padding-right: var(--space-3);
  }
}
</style>
