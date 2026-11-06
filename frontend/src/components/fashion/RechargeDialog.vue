<template>
  <el-dialog
    :model-value="modelValue"
    title="充值 · 芒果块"
    width="min(520px, 92vw)"
    class="recharge-dialog"
    :close-on-click-modal="false"
    append-to-body
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div v-loading="loadingOptions" class="recharge-body">
      <!-- 本期充值通道只保留接口：明确「即将开放」，文案全部来自后端 options.notice -->
      <el-alert
        class="recharge-notice"
        type="info"
        :closable="false"
        show-icon
        title="充值通道即将开放"
      >
        <template #default>
          <p class="notice-line">
            {{ options?.notice || '充值通道即将开放，目前可联系管理员发放芒果块' }}
          </p>
        </template>
      </el-alert>

      <p class="recharge-desc mono">
        目前唯一的入金途径是管理端发放 / 调整。下方可作为接口占位体验：
        提交后后端会返回「暂未开放」，不会真的入账，也不会产生任何扣款。
      </p>

      <!-- 接入支付后后端把 available 置 true、channels 填充，前端无需发版 -->
      <template v-if="options?.available">
        <div v-if="options.channels.length" class="channel-list">
          <el-radio
            v-for="channel in options.channels"
            :key="channel.id"
            v-model="channelId"
            :value="channel.id"
            :disabled="!channel.enabled || submitting"
            class="channel-item"
          >
            {{ channel.name || channel.id }}
            <span v-if="channel.description" class="channel-desc mono"> · {{ channel.description }}</span>
          </el-radio>
        </div>
      </template>

      <el-form class="recharge-form" label-width="88px" @submit.prevent="submitRecharge">
        <el-form-item label="充值数量">
          <el-input-number
            v-model="amount"
            :min="1"
            :max="1000000"
            :step="10"
            :disabled="submitting"
            controls-position="right"
            class="amount-input"
            aria-label="充值芒果块数量"
          />
          <span class="unit-hint mono">芒果块</span>
        </el-form-item>
      </el-form>

      <!-- 501 占位调用的友好反馈：info 级、不是崩溃 -->
      <el-alert
        v-if="feedback"
        class="recharge-feedback"
        type="success"
        :closable="false"
        show-icon
        title="已收到请求"
      >
        <template #default>
          <p class="notice-line">{{ feedback }}</p>
        </template>
      </el-alert>
    </div>

    <template #footer>
      <el-button @click="close">关闭</el-button>
      <el-button
        type="primary"
        :loading="submitting"
        :disabled="submitting || !Number.isFinite(amount) || amount < 1"
        @click="submitRecharge"
      >
        提交（占位）
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import {
  fashionApi,
  fashionErrorIsRechargeUnavailable,
  fashionErrorMessage,
  notifyFashionError,
  type RechargeOptionsResponse,
} from '@/api/fashion'
import { useScrollLock } from '@/composables/useScrollLock'

/**
 * 充值弹窗（任务 A2）。本期充值通道**只保留接口**：
 * - 展示内容全部来自 `GET /wallet/recharge/options`（available=false + notice）；
 * - 「提交」走 `POST /wallet/recharge` 占位，固定 501 `recharge_not_available`；
 *   对 501 做**友好提示**（info 级 + 弹窗内说明），不当崩溃、不弹红色错误条；
 * - 后续接支付只需后端把 available 置 true、channels 填充，前端契约不变。
 */
const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

const { lock, unlock } = useScrollLock()

const options = ref<RechargeOptionsResponse | null>(null)
const loadingOptions = ref(false)
const submitting = ref(false)
const amount = ref<number>(100)
const channelId = ref('')
const feedback = ref('')

// 两条请求各自的竞态序号 + 卸载标记：切页 / 连点只采纳最后一次响应
let optionsSeq = 0
let submitSeq = 0
let isUnmounted = false

async function loadOptions(): Promise<void> {
  const mySeq = ++optionsSeq
  loadingOptions.value = true
  feedback.value = ''
  try {
    const { data } = await fashionApi.rechargeOptions()
    if (isUnmounted || mySeq !== optionsSeq) return
    options.value = data
    // 默认选中第一个可用渠道（本期 channels 为空）
    channelId.value = data.channels.find((c) => c.enabled)?.id || ''
  } catch (error) {
    if (isUnmounted || mySeq !== optionsSeq) return
    // 充值入口信息读取失败不阻塞弹窗展示：用内置兜底文案
    options.value = null
    notifyFashionError(error, '读取充值信息失败')
  } finally {
    if (!isUnmounted && mySeq === optionsSeq) loadingOptions.value = false
  }
}

async function submitRecharge(): Promise<void> {
  if (submitting.value || !Number.isFinite(amount.value) || amount.value < 1) return
  const mySeq = ++submitSeq
  submitting.value = true
  feedback.value = ''
  try {
    const clientRequestId = `web-recharge-${Date.now().toString(36)}-${Math.random()
      .toString(36)
      .slice(2, 10)}`
    await fashionApi.recharge({
      amount: Math.floor(amount.value),
      channel: channelId.value,
      clientRequestId,
    })
    if (isUnmounted || mySeq !== submitSeq) return
    // 后端未来接入支付后才会走这里：回显成功
    ElMessage.success('充值成功')
    feedback.value = '充值成功，余额已更新。'
    emit('update:modelValue', false)
  } catch (error) {
    if (isUnmounted || mySeq !== submitSeq) return
    // 本期固定 501：预期内的占位响应，友好提示，不当崩溃
    if (fashionErrorIsRechargeUnavailable(error)) {
      const notice = options.value?.notice || '充值通道即将开放，敬请期待'
      ElMessage.info(notice)
      feedback.value = notice
    } else {
      // 其它错误（鉴权 / 网络 / 参数）仍走统一中文提示
      notifyFashionError(error, '充值失败')
      feedback.value = fashionErrorMessage(error, '充值失败')
    }
  } finally {
    if (!isUnmounted && mySeq === submitSeq) submitting.value = false
  }
}

function close(): void {
  emit('update:modelValue', false)
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) {
      lock('recharge-dialog')
      void loadOptions()
    } else {
      unlock('recharge-dialog')
      // 作废仍在飞的两类请求
      ++optionsSeq
      ++submitSeq
      submitting.value = false
      loadingOptions.value = false
    }
  }
)

onUnmounted(() => {
  isUnmounted = true
  ++optionsSeq
  ++submitSeq
  unlock('recharge-dialog')
})
</script>

<style scoped>
.recharge-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.recharge-notice,
.recharge-feedback {
  margin: 0;
}

.notice-line {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.7;
}

.recharge-desc {
  margin: 0;
  font-size: 11.5px;
  color: var(--text-muted);
  line-height: 1.8;
}

.recharge-form {
  padding-top: 0;
}

.amount-input {
  width: 100%;
  max-width: 200px;
}

.unit-hint {
  margin-left: var(--space-2);
  color: var(--text-secondary);
  font-size: 12px;
}

.channel-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.channel-item {
  min-height: 44px;
  align-items: center;
}

.channel-desc {
  color: var(--text-muted);
  font-size: 11px;
}
</style>
