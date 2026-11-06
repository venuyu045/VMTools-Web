<template>
  <div class="store-item-actions" :class="`variant-${variant}`">
    <!-- ① 已拥有：不扣款；若被移出衣柜，提供「补回衣柜」（幂等 purchase） -->
    <template v-if="state === 'owned'">
      <span class="ward-tag">已拥有 ✓</span>
      <el-button
        v-if="!inWardrobe"
        :size="buttonSize"
        :text="isCard"
        type="primary"
        class="restore-btn"
        :loading="purchasing"
        :disabled="purchasing"
        @click="emit('restore')"
      >
        补回衣柜
      </el-button>
      <el-button
        :size="buttonSize"
        :text="isCard"
        type="primary"
        class="equip-btn"
        @click="emit('equip')"
      >
        去穿戴 →
      </el-button>
    </template>

    <!-- ② 不可售卖：禁用态 -->
    <el-button v-else-if="state === 'not-purchasable'" :size="buttonSize" :text="isCard" disabled>
      暂不可购买
    </el-button>

    <!-- ③ 免费：沿用原「加入衣柜」逻辑 -->
    <template v-else-if="state === 'free'">
      <template v-if="inWardrobe">
        <span class="ward-tag">已在衣柜 ✓</span>
        <el-button :size="buttonSize" :text="isCard" type="primary" class="equip-btn" @click="emit('equip')">
          去穿戴 →
        </el-button>
      </template>
      <el-button
        v-else
        :size="buttonSize"
        :text="isCard"
        type="primary"
        class="equip-btn"
        :loading="adding"
        :disabled="adding"
        @click="emit('add')"
      >
        加入衣柜
      </el-button>
    </template>

    <!-- ④ 付费未拥有：🥭 N 购买 → 二次确认 -->
    <el-button
      v-else
      :size="buttonSize"
      type="primary"
      class="buy-btn"
      :loading="purchasing"
      :disabled="purchasing"
      @click="emit('buy')"
    >
      🥭 {{ price }} 购买
    </el-button>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { StoreItem } from '@/api/fashion'

/**
 * 商店卡片 / 详情弹窗共用的「四态动作」（任务 A2）。
 *
 * 状态优先级：已拥有 → 不可售卖 → 免费 → 付费未拥有。
 * owned / inWardrobe 直接取 `StoreItem`（后端 GET /store/items 已按规范 ID
 * 算好拥有与衣柜状态，并处理了历史简单 ID 的 localId 兜底）；购买成功后父组件
 * 就地翻转这两个字段即可，无需另发 /wardrobe。
 */
const props = withDefaults(
  defineProps<{
    item: StoreItem
    variant?: 'card' | 'dialog'
    adding?: boolean
    purchasing?: boolean
  }>(),
  { variant: 'card', adding: false, purchasing: false }
)

const emit = defineEmits<{
  add: []
  buy: []
  equip: []
  restore: []
}>()

const isCard = computed(() => props.variant === 'card')
const buttonSize = computed<'small' | 'default'>(() => (isCard.value ? 'small' : 'default'))
const price = computed(() => Math.max(0, props.item.priceMango || 0))
const inWardrobe = computed(() => !!props.item.inWardrobe)

const state = computed<'owned' | 'not-purchasable' | 'free' | 'paid'>(() => {
  if (props.item.owned) return 'owned'
  if (!props.item.purchasable) return 'not-purchasable'
  if (price.value <= 0) return 'free'
  return 'paid'
})
</script>

<style scoped>
.store-item-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  flex-wrap: wrap;
  margin-left: auto;
}

.store-item-actions.variant-dialog {
  gap: var(--space-2);
  margin-left: 0;
}

.ward-tag {
  font-size: 11px;
  line-height: 18px;
  white-space: nowrap;
  color: var(--success);
  background: var(--success-bg);
  border-radius: 999px;
  padding: 0 8px;
}

/* 卡片内文字按钮：12 张卡各一个实心主按钮会把整页压成一片橙。
   只对 card 变体覆盖文字色；dialog 变体是实心 primary（白字），
   若也把 color 改成强调橙，就会变成橙底橙字看不清。 */
.variant-card .equip-btn,
.variant-card .restore-btn {
  color: var(--accent) !important;
}

.buy-btn {
  white-space: nowrap;
}

@media (max-width: 768px) {
  /* 触控热区由 global.css 统一把 .el-button 撑到 ≥44px；这里只防换行挤压 */
  .store-item-actions {
    row-gap: 4px;
  }
}
</style>
