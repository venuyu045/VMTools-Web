<template>
  <div class="mango-admin">
    <div class="pane-toolbar">
      <div class="toolbar-left">
        <el-input
          v-model="keyword"
          size="small"
          clearable
          placeholder="搜索 gameId / 昵称"
          aria-label="搜索用户"
          class="search-input"
          @keyup.enter="onSearch"
          @clear="onSearch"
        />
        <el-button size="small" type="primary" :disabled="loading" @click="onSearch">搜索</el-button>
      </div>
      <div class="toolbar-right">
        <span class="count-hint mono">{{ total }} 位用户</span>
        <el-button size="small" text :loading="loading" :disabled="loading" @click="reload">刷新</el-button>
      </div>
    </div>

    <div class="table-scroll">
      <el-table
        v-loading="loading"
        :data="users"
        size="small"
        stripe
        class="admin-table"
        empty-text="没有匹配的用户"
        :header-cell-style="headerCellStyle"
      >
        <el-table-column label="用户" min-width="160">
          <template #default="{ row }">
            <div class="user-cell">
              <span class="user-name">{{ row.displayName || row.gameId || '未命名' }}</span>
              <span class="user-id mono">{{ row.gameId || row.userId }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="角色" width="90">
          <template #default="{ row }">
            <span class="mono">{{ row.role || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="余额（芒果块）" width="130">
          <template #default="{ row }">
            <span class="balance-cell">🥭 {{ fmtNum(row.balance) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="累计获得 / 消费" width="150">
          <template #default="{ row }">
            <span class="mono">+{{ fmtNum(row.totalEarned) }} / -{{ fmtNum(row.totalSpent) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="openAdjust(row)">发放 / 扣减</el-button>
            <el-button size="small" text type="primary" @click="openGrant(row)">发放时装</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div v-if="total > limit" class="pane-footer">
      <el-pagination
        layout="prev, pager, next"
        :total="total"
        :page-size="limit"
        :current-page="page"
        @current-change="onPageChange"
      />
    </div>

    <p class="pane-hint mono">
      充值通道本期未开放，管理端发放 / 扣减是唯一的入金途径。delta 为正发放、为负扣回，
      扣减后余额不得为负；每次调整都会写操作者审计流水。
    </p>

    <!-- 发放 / 扣减 -->
    <el-dialog
      v-model="adjust.visible"
      title="发放 / 扣减芒果块"
      width="min(520px, 92vw)"
      :close-on-click-modal="false"
      :close-on-press-escape="!adjust.submitting"
      destroy-on-close
    >
      <el-form :model="adjust" label-width="104px" class="dialog-form">
        <el-form-item label="目标用户">
          <span class="mono">{{ adjust.target ? (adjust.target.displayName || adjust.target.gameId || adjust.target.userId) : '—' }}</span>
          <span class="field-hint mono">当前余额 🥭 {{ adjust.target ? fmtNum(adjust.target.balance) : 0 }}</span>
        </el-form-item>
        <el-form-item label="数量（delta）">
          <el-input-number
            v-model="adjust.delta"
            :min="-1000000"
            :max="1000000"
            :step="10"
            class="delta-input"
            aria-label="发放为正、扣减为负"
          />
          <span class="field-hint mono inline">正数发放、负数扣减，不能为 0（范围 ±1,000,000）</span>
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="adjust.note"
            maxlength="120"
            placeholder="如：活动奖励 / 误发扣回"
            aria-label="调整备注"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="adjust.submitting" @click="adjust.visible = false">取消</el-button>
        <el-button type="primary" :loading="adjust.submitting" :disabled="adjust.submitting" @click="submitAdjust">
          确认{{ adjust.delta >= 0 ? '发放' : '扣减' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 直接发放时装（不扣币，自动加入衣柜） -->
    <el-dialog
      v-model="grant.visible"
      title="直接发放时装"
      width="min(520px, 92vw)"
      :close-on-click-modal="false"
      :close-on-press-escape="!grant.submitting"
      destroy-on-close
    >
      <el-form :model="grant" label-width="104px" class="dialog-form">
        <el-form-item label="目标用户">
          <span class="mono">{{ grant.target ? (grant.target.displayName || grant.target.gameId || grant.target.userId) : '—' }}</span>
        </el-form-item>
        <el-form-item label="时装">
          <el-select
            v-model="grant.cosmeticId"
            filterable
            clearable
            placeholder="选择目录里的时装"
            style="width: 100%"
            aria-label="选择要发放的时装"
          >
            <el-option
              v-for="item in cosmetics"
              :key="item.cosmeticId"
              :label="`${item.cosmeticId}${item.name ? ' · ' + item.name : ''}`"
              :value="item.cosmeticId"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="grant.note" maxlength="120" placeholder="如：补发 / 活动赠送" aria-label="发放备注" />
        </el-form-item>
        <p class="field-hint mono">发放不扣芒果块，会写所有权并自动加入该用户衣柜。用户此前已拥有时幂等返回，不重复写入。</p>
      </el-form>
      <template #footer>
        <el-button :disabled="grant.submitting" @click="grant.visible = false">取消</el-button>
        <el-button type="primary" :loading="grant.submitting" :disabled="grant.submitting || !grant.cosmeticId" @click="submitGrant">
          确认发放
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  fashionApi,
  notifyFashionError,
  type AdminCatalogItem,
  type AdminWalletUser,
} from '@/api/fashion'

/** 芒果块管理（任务 A3）：用户搜索 / 余额 / 发放扣减 / 直接发放时装。 */
const props = defineProps<{ cosmetics: AdminCatalogItem[] }>()

const limit = 20
const keyword = ref('')
const appliedKeyword = ref('')
const users = ref<AdminWalletUser[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)

const adjust = reactive({ visible: false, submitting: false, delta: 100, note: '', target: null as AdminWalletUser | null })
const grant = reactive({ visible: false, submitting: false, cosmeticId: '', note: '', target: null as AdminWalletUser | null })

// 竞态 + 卸载保护
let seq = 0
let isUnmounted = false

function fmtNum(value: number): string {
  const n = Number(value)
  return (Number.isFinite(n) ? n : 0).toLocaleString('zh-CN')
}

function headerCellStyle() {
  return {
    background: 'var(--bg-card)',
    color: 'var(--text-secondary)',
    fontWeight: 600,
    fontSize: '12.5px',
  }
}

async function loadUsers(): Promise<void> {
  const mySeq = ++seq
  loading.value = true
  try {
    const { data } = await fashionApi.adminWalletUsers({
      keyword: appliedKeyword.value,
      limit,
      offset: (page.value - 1) * limit,
    })
    if (isUnmounted || mySeq !== seq) return
    users.value = data.items || []
    total.value = data.total || 0
    const maxPage = Math.max(1, Math.ceil(total.value / limit))
    if (page.value > maxPage) page.value = maxPage
  } catch (error) {
    if (isUnmounted || mySeq !== seq) return
    notifyFashionError(error, '读取用户钱包列表失败')
  } finally {
    if (!isUnmounted && mySeq === seq) loading.value = false
  }
}

function onSearch(): void {
  appliedKeyword.value = keyword.value.trim()
  page.value = 1
  void loadUsers()
}

function reload(): void {
  void loadUsers()
}

function onPageChange(next: number): void {
  page.value = next
  void loadUsers()
}

function openAdjust(user: AdminWalletUser): void {
  adjust.target = user
  adjust.delta = 100
  adjust.note = ''
  adjust.visible = true
}

async function submitAdjust(): Promise<void> {
  const target = adjust.target
  if (!target || adjust.submitting) return
  const delta = Math.trunc(Number(adjust.delta))
  if (!Number.isFinite(delta) || delta === 0) {
    ElMessage.warning('数量必须是非 0 整数（正数发放、负数扣减）')
    return
  }
  if (delta < -1000000 || delta > 1000000) {
    ElMessage.warning('单次调整范围为 ±1,000,000 芒果块')
    return
  }
  const note = adjust.note.trim()
  const action = delta > 0 ? '发放' : '扣减'
  try {
    await ElMessageBox.confirm(
      `确认给用户「${target.displayName || target.gameId || target.userId}」${action} ${Math.abs(delta)} 芒果块？` +
        (delta < 0 ? '扣减后余额不得为负。' : '') + (note ? ` 备注：${note}` : ''),
      `确认${action}芒果块`,
      { type: 'warning', confirmButtonText: `确认${action}`, cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  adjust.submitting = true
  try {
    const { data } = await fashionApi.adminAdjustWallet({
      userId: target.userId,
      delta,
      note,
    })
    if (isUnmounted) return
    ElMessage.success(`已${action} ${Math.abs(delta)} 芒果块，该用户当前余额 🥭 ${data.balance}`)
    adjust.visible = false
    // 就地更新余额（列表不重拉也能看到），再静默回读一次保证权威
    const idx = users.value.findIndex((u) => u.userId === target.userId)
    if (idx >= 0) users.value[idx] = { ...users.value[idx], balance: data.balance }
    await loadUsers()
  } catch (error) {
    if (isUnmounted) return
    notifyFashionError(error, `${action}失败`)
  } finally {
    adjust.submitting = false
  }
}

function openGrant(user: AdminWalletUser): void {
  grant.target = user
  grant.cosmeticId = ''
  grant.note = ''
  grant.visible = true
}

async function submitGrant(): Promise<void> {
  const target = grant.target
  const cosmeticId = grant.cosmeticId.trim()
  if (!target || !cosmeticId || grant.submitting) return
  const note = grant.note.trim()
  const cosmeticName =
    props.cosmetics.find((item) => item.cosmeticId === cosmeticId)?.name || cosmeticId
  try {
    await ElMessageBox.confirm(
      `确认给用户「${target.displayName || target.gameId || target.userId}」直接发放「${cosmeticName}」？` +
        '此操作不扣芒果块，会自动加入该用户衣柜。',
      '确认发放时装',
      { type: 'warning', confirmButtonText: '确认发放', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  grant.submitting = true
  try {
    const { data } = await fashionApi.adminGrantCosmetic({
      userId: target.userId,
      cosmeticId,
      note,
    })
    if (isUnmounted) return
    ElMessage.success(data.granted ? `已发放「${cosmeticName}」` : `该用户已拥有「${cosmeticName}」（无需重复发放）`)
    grant.visible = false
  } catch (error) {
    if (isUnmounted) return
    notifyFashionError(error, '发放时装失败')
  } finally {
    grant.submitting = false
  }
}

onMounted(() => {
  void loadUsers()
})

onUnmounted(() => {
  isUnmounted = true
  ++seq
})
</script>

<style scoped>
.mango-admin {
  padding-top: var(--space-2);
}

.pane-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  margin-bottom: var(--space-3);
  flex-wrap: wrap;
}

.toolbar-left,
.toolbar-right {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.search-input {
  width: 220px;
  max-width: 100%;
}

.count-hint {
  font-size: 12px;
  color: var(--text-secondary);
}

.table-scroll {
  width: 100%;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}

.admin-table {
  min-width: 620px;
}

.user-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.user-name {
  font-size: 13px;
  color: var(--text-primary);
}

.user-id {
  font-size: 11px;
  color: var(--text-muted);
  overflow-wrap: anywhere;
}

.balance-cell {
  color: var(--accent);
  font-weight: 600;
}

.mono {
  font-family: var(--font-mono);
  font-size: 12px;
}

.pane-footer {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--space-3);
}

.pane-hint {
  margin: var(--space-3) 0 0;
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.8;
}

.dialog-form {
  padding-top: var(--space-2);
}

.field-hint {
  margin: var(--space-1) 0 0;
  font-size: 11.5px;
  color: var(--text-muted);
  line-height: 1.7;
}

.field-hint.inline {
  margin: 0 0 0 var(--space-2);
}

.delta-input {
  width: 100%;
  max-width: 200px;
}

@media (max-width: 900px) {
  .search-input {
    width: 100%;
  }

  .pane-toolbar {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
