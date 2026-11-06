<template>
  <div class="fashion-bind-page">
    <h2 class="page-title">时装绑定</h2>
    <p class="page-subtitle mono">
      把 Minecraft 账号绑定到本站账号 · 绑定后可在网页衣柜里换装，游戏内 mod 与网页共用同一份穿戴配置
    </p>

    <div class="bind-grid">
      <!-- 已绑定身份 -->
      <el-card shadow="never" class="bind-card">
        <template #header>
          <div class="card-header">
            <span class="card-title">我的 Minecraft 账号</span>
            <el-button
              size="small"
              text
              :loading="loadingIdentities"
              :disabled="loadingIdentities"
              @click="loadIdentities"
            >
              刷新
            </el-button>
          </div>
        </template>

        <div v-loading="loadingIdentities" class="card-body">
          <el-alert
            v-if="identitiesError"
            class="panel-error"
            type="error"
            :closable="false"
            show-icon
            title="读取绑定信息失败"
          >
            <template #default>
              <el-button size="small" text type="primary" @click="loadIdentities">重试</el-button>
            </template>
          </el-alert>
          <el-empty
            v-if="!loadingIdentities && !identitiesError && identities.length === 0"
            :image-size="72"
          >
            <template #description>
              <div class="empty-desc">
                <p class="mono">还没有绑定任何 Minecraft 账号</p>
                <p class="hint mono">
                  先在游戏内执行 <code>/vmfashion bind</code> 拿到 6 位绑定码，再到右侧输入。
                </p>
              </div>
            </template>
          </el-empty>
          <div v-else-if="identities.length > 0" class="identity-list">
            <div v-for="identity in identities" :key="identity.id" class="identity-item">
              <div class="identity-main">
                <span class="mc-name">{{ identity.mcName }}</span>
                <span class="pixel-badge" :class="identity.authType === 'mojang' ? 'green' : 'yellow'">
                  <span class="badge-dot"></span>
                  {{ identity.authType === 'mojang' ? '正版' : '皮肤站' }}
                </span>
              </div>
              <div class="identity-meta mono">{{ identity.mcUuid }}</div>
              <div class="identity-meta mono">
                认证于 {{ fmtApiTime(identity.verifiedAt) }}
                <template v-if="identity.lastSeenAt"> · 最近上线 {{ fmtApiTime(identity.lastSeenAt) }}</template>
              </div>
              <div class="identity-actions">
                <el-button size="small" :disabled="!!unbindingId" @click="goWardrobe(identity.id)">去衣柜</el-button>
                <el-button
                  size="small"
                  type="danger"
                  plain
                  :loading="unbindingId === identity.id"
                  :disabled="!!unbindingId"
                  @click="handleUnbind(identity)"
                >
                  解绑
                </el-button>
              </div>
            </div>
          </div>
        </div>
      </el-card>

      <!-- 输入绑定码 -->
      <el-card shadow="never" class="bind-card">
        <template #header>
          <div class="card-header">
            <span class="card-title">输入绑定码</span>
          </div>
        </template>
        <div class="card-body">
          <p class="hint mono">
            在游戏内执行 <code>/vmfashion bind</code>，mod 会在聊天栏显示 6 位绑定码（10 分钟内有效）。
            在下面输入该码即可完成绑定。
          </p>

          <div class="code-row" ref="codeRowRef">
            <el-input
              ref="codeInputRef"
              v-model="userCode"
              class="code-input"
              maxlength="6"
              placeholder="例如 7K2M9Q"
              aria-label="6 位绑定码"
              autocomplete="one-time-code"
              :disabled="approving"
              clearable
              @focus="onCodeFocus"
              @keyup.enter="handleApprove"
            />
            <el-button
              type="primary"
              :loading="approving"
              :disabled="normalizedCode.length !== 6 || approving"
              @click="handleApprove"
            >
              绑定
            </el-button>
          </div>

          <!-- C1：长度不足时明确告诉用户还差几位（原来只体现为按钮 disabled） -->
          <p class="hint mono code-hint" role="status" aria-live="polite">
            <template v-if="normalizedCode.length === 0">
              绑定码为 6 位（去掉了容易看错的 I / O / 0 / 1），10 分钟内有效
            </template>
            <template v-else-if="normalizedCode.length < 6">
              还差 {{ 6 - normalizedCode.length }} 位
            </template>
            <template v-else>格式正确，点「绑定」即可</template>
          </p>

          <el-alert
            v-if="lastBound"
            class="bound-alert"
            type="success"
            :closable="true"
            show-icon
            :title="`已绑定 ${lastBound.mcName}`"
            :description="lastBound.mcUuid"
            @close="lastBound = null"
          />
          <!-- C3：绑定成功给一个直达衣柜的 CTA（原来只有一句提示） -->
          <div v-if="lastBound" class="bound-actions">
            <el-button size="small" type="primary" @click="goWardrobe(lastBound.identityId)">
              去衣柜换装
            </el-button>
          </div>

          <p class="hint mono">
            绑定成功后可用
            <router-link class="link" to="/fashion/wardrobe">衣柜</router-link>
            管理穿戴。
          </p>
        </div>
      </el-card>
    </div>

    <!-- 说明 -->
    <el-card shadow="never" class="bind-card explain-card">
      <template #header>
        <div class="card-header">
          <span class="card-title">为什么不是「输入 MC 密码」？</span>
        </div>
      </template>
      <div class="card-body">
        <p class="hint mono">
          网页拿不到 Minecraft 的 accessToken，所以本站不会、也不需要你输入 MC 密码。
          绑定走的是设备码流程：mod 端已经用标准 serverId 流程（Mojang 正版 / 皮肤站 Yggdrasil）
          向会话服务器验证过身份，网页只需要确认「这次绑定是我发起的」。
        </p>
        <ul class="explain-list mono">
          <li>绑定码 6 位、10 分钟有效、一次性使用；</li>
          <li>一个网站账号可以绑定多个 Minecraft 账号；</li>
          <li>解绑只会解除网站关联，不影响 mod 的认证状态。</li>
        </ul>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { InputInstance } from 'element-plus'
import {
  fashionApi,
  fashionErrorCode,
  fashionErrorMessage,
  fashionRetryAfter,
  isSilentFashionError,
  notifyFashionError,
  type DeviceApproveResult,
  type FashionIdentity,
} from '@/api/fashion'
import { fmtApiTime } from '@/utils/time'

const router = useRouter()

const identities = ref<FashionIdentity[]>([])
const loadingIdentities = ref(false)
const identitiesError = ref(false)
const userCode = ref('')
const approving = ref(false)
const unbindingId = ref('')
const lastBound = ref<DeviceApproveResult | null>(null)
const codeInputRef = ref<InputInstance | null>(null)
const codeRowRef = ref<HTMLElement | null>(null)

// 身份列表请求竞态保护：刷新与绑定后的重新加载可能并发
let identitiesSeq = 0
// P3：组件卸载后网络回来不再写状态 / 不再弹提示
let isUnmounted = false

/**
 * 输入框获得焦点后把这一行滚到视口中间（P3）。
 * 移动端软键盘弹出会压缩可视区（`interactive-widget=resizes-content` 已在
 * index.html 的 viewport 声明），滚到中间可避免输入框被顶栏 / 键盘遮住。
 */
function onCodeFocus(): void {
  codeRowRef.value?.scrollIntoView({ block: 'center', behavior: 'smooth' })
}

const normalizedCode = computed(() => userCode.value.replace(/[\s-_]/g, '').toUpperCase())

/**
 * 绑定链路的 6 类可区分错误（契约 §9）。
 *
 * 后端 `detail.message` 本身是中文，但通用兜底会把「过期」和「被占用」都显示成
 * 同一句「请确认绑定码是否正确/未过期」→ 用户不知道该重新生成还是去解绑。
 * 429 还带 `Retry-After`，能直接告诉用户等多少秒。
 */
const APPROVE_ERRORS: Record<string, string> = {
  device_not_found: '绑定码不存在，请在游戏内重新执行 /vmfashion bind',
  device_expired: '绑定码已过期（10 分钟有效），请在游戏内重新生成',
  device_bound_to_other: '这个绑定码已经被其他账号使用，请重新生成',
  identity_bound_to_other: '该 Minecraft 身份已绑定到其他网站账号，请先在那边解绑',
}

function approveErrorMessage(error: unknown): string {
  const code = fashionErrorCode(error)
  if (code === 'rate_limited') {
    const retry = fashionRetryAfter(error)
    return retry ? `操作太频繁，请等待 ${retry} 秒后再试` : '操作太频繁，请稍后再试'
  }
  const mapped = APPROVE_ERRORS[code]
  if (mapped) return mapped
  return fashionErrorMessage(error, '绑定失败，请确认绑定码是否正确/未过期')
}

async function loadIdentities(): Promise<void> {
  const seq = ++identitiesSeq
  loadingIdentities.value = true
  identitiesError.value = false
  try {
    const { data } = await fashionApi.myIdentities()
    if (isUnmounted || seq !== identitiesSeq) return // 已被更新的请求取代 / 组件已卸载
    identities.value = data.items || []
  } catch (error) {
    if (isUnmounted || seq !== identitiesSeq) return
    identitiesError.value = true
    notifyFashionError(error, '读取绑定信息失败')
  } finally {
    if (!isUnmounted && seq === identitiesSeq) loadingIdentities.value = false
  }
}

async function handleApprove(): Promise<void> {
  if (approving.value) return
  const code = normalizedCode.value
  if (code.length !== 6) {
    ElMessage.warning('请输入 6 位绑定码')
    return
  }
  approving.value = true
  try {
    const { data } = await fashionApi.approveDevice(code)
    lastBound.value = data
    userCode.value = ''
    ElMessage.success(`绑定成功：${data.mcName}`)
    await loadIdentities()
  } catch (error) {
    // 401 已由 api/client.ts 拦截器统一提示 + 跳登录；主动取消不是错误
    if (!isSilentFashionError(error)) ElMessage.error(approveErrorMessage(error))
  } finally {
    approving.value = false
  }
}

async function handleUnbind(identity: FashionIdentity): Promise<void> {
  if (unbindingId.value) return
  try {
    await ElMessageBox.confirm(
      `确定解绑 ${identity.mcName}（${identity.mcUuid}）吗？解绑后网页将无法再管理该账号的穿戴。`,
      '解绑确认',
      { type: 'warning', confirmButtonText: '解绑', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  unbindingId.value = identity.id
  try {
    await fashionApi.unbind(identity.id)
    await loadIdentities()
    // C4：解绑最后一个身份后列表变空态，得告诉用户下一步要回游戏敲命令
    if (identities.value.length === 0) {
      ElMessage.success('已解绑（最后一个身份）· 回游戏执行 /vmfashion bind 可重新绑定')
    } else {
      ElMessage.success('已解绑')
    }
  } catch (error) {
    notifyFashionError(error, '解绑失败')
  } finally {
    unbindingId.value = ''
  }
}

function goWardrobe(identityId: string): void {
  router.push({ path: '/fashion/wardrobe', query: { identityId } })
}

onMounted(() => {
  // C1：进入页面直接聚焦绑定码输入框（原来要点一下才输入）。
  // 放在 loadIdentities 之前：身份列表是网络请求，等它回来再聚焦会让自动聚焦来得太晚。
  // P3：移动端**不自动聚焦** —— 挂载即 focus 会立刻弹出软键盘、把页面顶飞；
  // 用户主动点输入框时由 @focus 的 onCodeFocus 把这一行滚到视口中间。
  const isMobile =
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(max-width: 768px)').matches
  if (!isMobile) {
    codeInputRef.value?.focus()
    codeRowRef.value?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }
  void loadIdentities()
})

onUnmounted(() => {
  isUnmounted = true
  ++identitiesSeq
})
</script>

<style scoped>
.fashion-bind-page {
  padding: var(--space-4);
  max-width: 1180px;
  margin: 0 auto;
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
  margin: 0 0 var(--space-5);
}

.bind-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr);
  gap: var(--space-4);
  margin-bottom: var(--space-4);
}

@media (max-width: 900px) {
  .bind-grid {
    grid-template-columns: 1fr;
  }
}

.bind-card {
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.card-title {
  font-family: var(--font-display);
  font-size: 16px;
  color: var(--text-primary);
}

.card-body {
  min-height: 120px;
}

.panel-error {
  margin-bottom: var(--space-3);
}

.identity-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.identity-item {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: var(--space-3);
  background: var(--bg-elevated);
}

.identity-main {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-1);
}

.mc-name {
  font-family: var(--font-display);
  font-size: 17px;
  color: var(--text-primary);
}

.identity-meta {
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.7;
  word-break: break-all;
}

.identity-actions {
  margin-top: var(--space-2);
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.code-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin: var(--space-3) 0;
}

.code-input {
  flex: 1 1 160px;
  min-width: 0;
  max-width: 240px;
}

.code-input :deep(input) {
  font-family: var(--font-mono);
  font-size: 20px;
  letter-spacing: 4px;
  text-transform: uppercase;
}

.code-hint {
  margin: calc(var(--space-1) * -1) 0 var(--space-3);
  font-size: 12px;
}

.empty-desc {
  text-align: center;
  color: var(--text-secondary);
  font-size: 13px;
}

.empty-desc .hint {
  margin-top: var(--space-1);
  font-size: 12px;
}

.bound-actions {
  margin-bottom: var(--space-3);
}

.bound-alert {
  margin-bottom: var(--space-3);
}

.hint {
  color: var(--text-secondary);
  font-size: 13px;
  line-height: 1.8;
  margin: 0 0 var(--space-2);
}

.link {
  color: var(--text-primary);
  text-decoration: underline;
  text-decoration-color: var(--accent);
  text-underline-offset: 2px;
}

.explain-card {
  margin-top: var(--space-2);
}

.explain-list {
  margin: var(--space-2) 0 0;
  padding-left: 20px;
  color: var(--text-secondary);
  font-size: 13px;
  line-height: 1.9;
}
</style>
