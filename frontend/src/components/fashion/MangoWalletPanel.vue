<template>
  <div
    class="mango-wallet-panel"
    :class="{ compact }"
    role="status"
    :aria-label="`芒果块余额：${displayBalance}`"
    :title="`芒果块余额：${displayBalance}`"
  >
    <span class="mango-icon" aria-hidden="true">🥭</span>
    <span v-if="!compact" class="mango-label">芒果块</span>
    <span class="mango-balance mono">{{ displayBalance }}</span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

/**
 * 侧边栏 / 页头共用的「芒果块余额」展示（任务 A4）。
 *
 * 纯展示组件：数据由 `useMangoWallet()` 提供，读不到（balance=null）时：
 * - loading → 「…」；
 * - 失败静默降级 → 「—」，不弹错误、不阻塞导航。
 * compact（侧边栏折叠 / 移动端抽屉）只显示 🥭 + 数字，保证折叠态也能看到余额。
 */
const props = withDefaults(
  defineProps<{
    balance: number | null
    loading?: boolean
    compact?: boolean
  }>(),
  { loading: false, compact: false }
)

const displayBalance = computed(() => {
  if (props.balance !== null && Number.isFinite(props.balance)) {
    return Number(props.balance).toLocaleString('zh-CN')
  }
  return props.loading ? '…' : '—'
})
</script>

<style scoped>
.mango-wallet-panel {
  display: flex;
  align-items: center;
  gap: 6px;
  min-height: 36px;
  padding: 6px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--bg-elevated);
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.2;
  /* 不横向溢出：折叠侧边栏很窄时数字优先、标签可收缩 */
  min-width: 0;
  overflow: hidden;
}

.mango-icon {
  font-size: 14px;
  line-height: 1;
  flex-shrink: 0;
}

.mango-label {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.mango-balance {
  color: var(--text-primary);
  font-size: 13px;
  font-weight: 600;
  white-space: nowrap;
  margin-left: auto;
}

.mango-wallet-panel.compact {
  justify-content: center;
  padding: 6px 4px;
}

.mango-wallet-panel.compact .mango-balance {
  margin-left: 0;
  font-size: 12px;
}

@media (max-width: 768px) {
  .mango-wallet-panel {
    min-height: 44px;
    padding: 8px 10px;
  }
}
</style>
