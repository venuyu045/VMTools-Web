<template>
  <el-dialog
    :model-value="modelValue"
    :title="'确认购买 · ' + (item ? itemName : '时装')"
    width="min(520px, 92vw)"
    class="purchase-dialog"
    :close-on-click-modal="false"
    :close-on-press-escape="!submitting"
    :before-close="beforeClose"
    append-to-body
    @open="lock('purchase-dialog')"
    @closed="unlock('purchase-dialog')"
    @update:model-value="onUpdateVisible"
  >
    <div v-if="item" class="purchase-body">
      <div class="purchase-item">
        <div class="purchase-item-name">{{ itemName }}</div>
        <div class="purchase-item-id mono">{{ item.cosmeticId }}</div>
      </div>

      <!-- 二次确认：价格 / 购买前余额 / 购买后余额，移动端单列可滚动 -->
      <el-descriptions :column="1" border size="small" class="purchase-meta">
        <el-descriptions-item label="价格">
          <span class="price-value">🥭 {{ price }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="当前余额">
          <span class="mono">🥭 {{ balanceText }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="购买后余额">
          <span class="mono" :class="{ 'is-negative': insufficient }">
            🥭 {{ balanceAfterText }}
          </span>
        </el-descriptions-item>
      </el-descriptions>

      <!-- 余额不足引导：本期充值未开放，明确告诉用户去哪拿芒果块 -->
      <el-alert
        v-if="insufficient"
        class="purchase-alert"
        type="warning"
        :closable="false"
        show-icon
        title="芒果块余额不足"
      >
        <template #default>
          <p class="alert-line">
            还差 <strong>🥭 {{ shortBy }}</strong> 块。充值通道暂未开放，可联系管理员发放后再购买。
          </p>
          <el-button
            size="small"
            text
            type="primary"
            class="recharge-link"
            @click="emit('recharge')"
          >
            去充值 / 查看说明 →
          </el-button>
        </template>
      </el-alert>

      <!-- 上一次购买失败的后端中文 message（402 / 409 / 429 / 冲突等） -->
      <el-alert
        v-if="errorText"
        class="purchase-alert"
        type="error"
        :closable="false"
        show-icon
        title="上次购买未成功"
      >
        <template #default>
          <p class="alert-line">{{ errorText }}</p>
        </template>
      </el-alert>

      <p class="purchase-hint mono">
        购买成功后会自动加入「我的衣柜」，可前往衣柜穿戴；重复购买不会重复扣款。
      </p>
    </div>

    <template #footer>
      <el-button :disabled="submitting" @click="close">取消</el-button>
      <el-button
        type="primary"
        :loading="submitting"
        :disabled="submitting || !item"
        @click="confirmPurchase"
      >
        🥭 {{ price }} 确认购买
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { StoreItem } from '@/api/fashion'
import { useScrollLock } from '@/composables/useScrollLock'

/**
 * 购买二次确认弹窗（任务 A2）。
 *
 * 只做展示与确认：API 调用、成功/失败后的状态更新都由父组件（商店页）负责，
 * 通过 `confirm(clientRequestId)` 把幂等键交给父组件，避免组件内重复扣款逻辑。
 * 失败文案（`errorText`）由父组件用 `fashionErrorMessage()` 取后端中文 message 传入。
 */
const props = withDefaults(
  defineProps<{
    modelValue: boolean
    item: StoreItem | null
    balance: number
    submitting?: boolean
    errorText?: string
  }>(),
  { submitting: false, errorText: '' }
)

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  confirm: [clientRequestId: string]
  recharge: []
}>()

const { lock, unlock } = useScrollLock()

const itemName = computed(() => {
  const item = props.item
  if (!item) return ''
  return (item.name || '').trim() || item.cosmeticId
})
const price = computed(() => props.item?.priceMango ?? 0)
const balance = computed(() =>
  Number.isFinite(props.balance) ? Number(props.balance) : 0
)
const balanceAfter = computed(() => balance.value - price.value)
const insufficient = computed(() => balanceAfter.value < 0)
const shortBy = computed(() => Math.max(0, price.value - balance.value))
const balanceText = computed(() => balance.value.toLocaleString('zh-CN'))
const balanceAfterText = computed(() => balanceAfter.value.toLocaleString('zh-CN'))

function onUpdateVisible(value: boolean): void {
  emit('update:modelValue', value)
}

function close(): void {
  emit('update:modelValue', false)
}

/** 保存中不允许关闭（遮罩 / ESC / ✕），避免旧响应关掉刚打开的新弹窗 */
function beforeClose(done: () => void): void {
  if (props.submitting) return
  done()
}

/** 生成 <=64 字符的幂等键：网络重试 / 双击不会重复扣款 */
function newClientRequestId(): string {
  return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

function confirmPurchase(): void {
  if (props.submitting || !props.item) return
  emit('confirm', newClientRequestId())
}
</script>

<style scoped>
.purchase-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.purchase-item {
  min-width: 0;
}

.purchase-item-name {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
  overflow-wrap: anywhere;
}

.purchase-item-id {
  margin-top: 2px;
  font-size: 11px;
  color: var(--text-muted);
  overflow-wrap: anywhere;
}

.purchase-meta :deep(.el-descriptions__label) {
  width: 96px;
}

.price-value {
  color: var(--accent);
  font-weight: 600;
  font-size: 15px;
}

.is-negative {
  color: var(--danger);
}

.purchase-alert {
  margin: 0;
}

.alert-line {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.7;
}

.recharge-link {
  margin-top: 4px;
  padding-left: 0;
}

.purchase-hint {
  margin: 0;
  font-size: 11.5px;
  color: var(--text-muted);
  line-height: 1.7;
}

@media (max-width: 480px) {
  /* 弹窗内容可滚动、描述列表单列、按钮触控区由 global.css 统一撑到 44px */
  .purchase-meta :deep(.el-descriptions__label) {
    width: 84px;
  }
}
</style>
