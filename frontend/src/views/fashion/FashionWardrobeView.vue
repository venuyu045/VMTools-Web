<template>
  <!-- @keydown.esc 挂在本页根节点而不是 window：焦点在本页内时 Esc 才退出批量模式，
       弹窗/确认框打开时焦点在它们自己的遮罩里，不会误触（见 onEscape） -->
  <div class="wardrobe-page" @keydown.esc="onEscape">
    <div class="page-head">
      <div>
        <h2 class="page-title">我的衣柜</h2>
        <p class="page-subtitle mono">
          选好每个槽位的时装后保存 · 游戏内 mod 会按 UUID 自动拉取同一份配置
        </p>
      </div>
      <div class="head-actions">
        <el-select
          v-if="identities.length > 1"
          v-model="activeIdentityId"
          size="small"
          class="identity-select"
          aria-label="选择 Minecraft 身份"
          :disabled="saving"
          @change="onIdentityChange"
        >
          <el-option
            v-for="identity in identities"
            :key="identity.id"
            :label="`${identity.mcName}（${identity.mcUuid.slice(0, 8)}…）`"
            :value="identity.id"
          />
        </el-select>
        <span v-else-if="currentIdentity" class="identity-chip mono">
          {{ currentIdentity.mcName }} · {{ currentIdentity.authType === 'mojang' ? '正版' : '皮肤站' }}
        </span>
        <el-button
          type="primary"
          :loading="saving"
          :disabled="saveDisabled"
          @click="save"
        >
          保存穿戴
        </el-button>
      </div>
    </div>

    <!-- 未绑定 / 身份读取失败**不再顶掉整页**：左侧「我的衣柜」是账号维度的收藏
         （商店页不绑身份也能加入），没绑定时它仍然要能看、能删；提示只替换右侧
         的「当前穿戴」面板。见 loadout-body 里的同名 alert。 -->
    <div v-loading="loadingIdentities" class="wardrobe-grid">
      <!-- 左：我的衣柜（收藏制：只有商店里主动「加入衣柜」的时装才在这里） -->
      <el-card shadow="never" class="panel">
        <template #header>
          <div class="panel-header">
            <span class="panel-title">
              我的衣柜
              <span class="mono count-hint">（{{ wardrobe.length }} 件）</span>
            </span>
            <div class="panel-tools">
              <el-input
                v-model="searchKeyword"
                size="small"
                clearable
                placeholder="搜索名称 / ID"
                aria-label="搜索衣柜中的时装名称或 ID"
                class="wardrobe-search"
              />
              <!-- 衣柜是收藏制：在别的标签页加过之后，玩家需要一个不刷新整页的同步入口 -->
              <el-button
                size="small"
                text
                :loading="loadingWardrobe"
                :disabled="loadingWardrobe"
                @click="loadWardrobe()"
              >
                刷新
              </el-button>
              <!-- 批量管理模式：开关 + 全选/取消全选/删除选中(n)/完成 -->
              <el-button
                v-if="!batchMode"
                size="small"
                text
                type="primary"
                :disabled="wardrobe.length === 0 || loadingWardrobe"
                @click="enterBatchMode"
              >
                批量管理
              </el-button>
              <template v-else>
                <el-button
                  size="small"
                  text
                  :disabled="filteredWardrobe.length === 0 || allFilteredSelected"
                  @click="selectAllFiltered"
                >
                  全选
                </el-button>
                <el-button size="small" text :disabled="selectedIds.size === 0" @click="clearSelection">
                  取消全选
                </el-button>
                <el-button
                  size="small"
                  type="danger"
                  plain
                  :loading="batchDeleting"
                  :disabled="selectedIds.size === 0 || batchDeleting"
                  @click="batchDelete"
                >
                  删除选中({{ selectedIds.size }})
                </el-button>
                <el-button size="small" text @click="exitBatchMode">完成</el-button>
              </template>
            </div>
          </div>
          <el-radio-group
            v-model="activeCategory"
            size="small"
            class="wardrobe-filters"
            aria-label="按槽位筛选衣柜中的时装"
          >
            <el-radio-button value="all">全部</el-radio-button>
            <el-radio-button v-for="slot in FASHION_SLOTS" :key="slot.value" :value="slot.value">
              {{ slot.label }}
            </el-radio-button>
          </el-radio-group>
          <p v-if="batchMode" class="batch-hint mono" role="status" aria-live="polite">
            批量管理中：点击卡片勾选要移除的时装，再点「删除选中」；按 Esc 可退出。
            <template v-if="hiddenSelectedCount > 0">
              另有 {{ hiddenSelectedCount }} 件已勾选的时装不在当前筛选 / 搜索范围内，删除时会一并移除。
            </template>
          </p>
        </template>

        <div v-loading="loadingWardrobe" class="wardrobe-body" :aria-busy="loadingWardrobe">
          <!-- 只有「一件都没读到」时才整块显示错误态：删除后静默回读失败时，
               列表里已有的数据仍然可信，不该被一个错误面板盖掉（错误另有 toast） -->
          <el-alert
            v-if="wardrobeError && wardrobe.length === 0"
            class="panel-error"
            type="error"
            :closable="false"
            show-icon
            title="我的衣柜加载失败"
          >
            <template #default>
              <el-button size="small" text type="primary" @click="loadWardrobe()">重试</el-button>
            </template>
          </el-alert>

          <!-- 空衣柜：这里不再是「全站目录」，空就是真的没收藏 → 引导去商店 -->
          <el-empty
            v-else-if="wardrobe.length === 0"
            :image-size="72"
            description="你的衣柜还是空的"
          >
            <p class="empty-hint">到「时装商店」挑中喜欢的时装，点「加入衣柜」后才会出现在这里。</p>
            <router-link to="/fashion/store" custom v-slot="{ navigate }">
              <el-button type="primary" size="small" @click="navigate">去时装商店挑选</el-button>
            </router-link>
          </el-empty>

          <el-empty
            v-else-if="filteredWardrobe.length === 0"
            :description="emptyFilterText"
            :image-size="72"
          />

          <template v-else>
            <div class="wardrobe-cards" role="group" :aria-label="`我的衣柜，共 ${filteredWardrobe.length} 件`">
              <div
                v-for="item in pagedWardrobe"
                :key="item.cosmeticId"
                class="cosmetic-card"
                :class="{
                  equipped: isEquipped(item),
                  selected: batchMode && isSelected(item),
                  missing: item.missing,
                  'batch-mode': batchMode,
                }"
                role="group"
                :aria-label="cardAriaLabel(item)"
                @click="onCardActivate(item)"
              >
                <!-- 批量管理：勾选框只是「选中的视觉」，真正的勾选逻辑在卡片点击上，
                     这样键盘（Enter/Space）和触屏都能操作，不必精确点中小方块 -->
                <el-checkbox
                  v-if="batchMode"
                  class="card-check"
                  :model-value="isSelected(item)"
                  :aria-label="`选择 ${displayName(item)}`"
                  @click.stop
                  @change="toggleSelect(item)"
                />
                <!-- 单件移除：批量模式下隐藏，避免和「删除选中」混淆 -->
                <button
                  v-else
                  type="button"
                  class="card-remove"
                  :aria-label="`从衣柜移除 ${displayName(item)}`"
                  :disabled="deletingIds.has(item.cosmeticId)"
                  @click.stop="removeOne(item)"
                  @keydown.enter.stop.prevent="removeOne(item)"
                  @keydown.space.stop.prevent="removeOne(item)"
                >
                  ✕
                </button>
                <div class="cosmetic-thumb">
                  <!--
                    P2-4：已下架（missing）条目的资产必然 404（后端 resolve_asset_file
                    对停用行返回 None），不渲染 img，直接给「已下架」占位。
                    P2-1：破图按失败的 URL 记录，旧请求 error 不会误杀新预览。
                    P1-2：低优先级 + 懒加载 + 异步解码，移动端少解码全尺寸大图。
                  -->
                  <span v-if="item.missing" class="thumb-fallback missing-fallback">已下架</span>
                  <img
                    v-else-if="thumbUrl(item) && !brokenThumbUrls.has(thumbUrl(item))"
                    :src="thumbUrl(item)"
                    :alt="displayName(item)"
                    loading="lazy"
                    fetchpriority="low"
                    decoding="async"
                    @error="markThumbBroken"
                  />
                  <span v-else class="thumb-fallback">{{ (displayName(item) || '?').slice(0, 1) }}</span>
                </div>
                <div class="cosmetic-name" :title="displayName(item)">{{ displayName(item) }}</div>
                <div class="cosmetic-meta mono">
                  {{ CATEGORY_LABELS[itemSlot(item)] || item.category }}
                  <!-- missing = 站点目录里已下架：仍显示、仍可移除，但不能穿戴 -->
                  <span v-if="item.missing" class="missing-tag" title="站点目录里已下架，无法穿戴，但仍可移除">
                    已下架
                  </span>
                  <span v-if="isEquipped(item)" class="equipped-tag">已穿戴</span>
                  <span
                    v-else-if="!item.missing && item.assetKey && !previewUrls.has(item.cosmeticId)"
                    class="meta-tag"
                    title="该资产不是图片（glb / json / zip 等），网页衣柜无法预览"
                  >
                    无预览
                  </span>
                  <!-- B3：点卡片 = 立即穿戴，没有「先看大图/看元信息」的中间态 -->
                  <button
                    type="button"
                    class="detail-link"
                    :aria-label="`查看 ${displayName(item)} 的详情`"
                    @click.stop="openDetail(item)"
                    @keydown.enter.stop.prevent="openDetail(item)"
                    @keydown.space.stop.prevent="openDetail(item)"
                  >
                    详情
                  </button>
                </div>
              </div>
            </div>
            <!-- B2：一次渲染全部卡片，200 件时每张卡还要跑 4 次 isEquipped → 分页 -->
            <div v-if="filteredWardrobe.length > pageSize" class="wardrobe-pager">
              <el-pagination
                layout="prev, pager, next"
                :total="filteredWardrobe.length"
                :page-size="pageSize"
                :current-page="wardrobePage"
                @current-change="wardrobePage = $event"
              />
            </div>
          </template>
        </div>
      </el-card>

      <!-- 右：穿戴 -->
      <el-card shadow="never" class="panel">
        <template #header>
          <div class="panel-header">
            <span class="panel-title">当前穿戴</span>
            <span class="mono count-hint">{{ equippedCount }} 个槽位</span>
          </div>
        </template>

        <div v-loading="loadingLoadout" class="loadout-body">
          <!-- 身份读取失败 / 未绑定：只替换右侧「当前穿戴」，左侧收藏照常可用 -->
          <el-alert
            v-if="identitiesError"
            class="panel-error"
            type="error"
            :closable="false"
            show-icon
            title="读取 Minecraft 身份失败"
          >
            <template #default>
              <p class="no-identity-line">暂时读不到绑定信息，左侧「我的衣柜」仍可查看与移除收藏。</p>
              <el-button size="small" text type="primary" @click="reloadIdentities">重试</el-button>
            </template>
          </el-alert>

          <el-alert
            v-else-if="noIdentity"
            class="panel-error"
            type="warning"
            :closable="false"
            show-icon
            title="当前账号还没有绑定 Minecraft 身份"
          >
            <template #default>
              <p class="no-identity-line">
                请先到
                <router-link class="link" to="/fashion/bind">时装绑定</router-link>
                页输入 mod 显示的 6 位绑定码，绑定后才能在这里选择穿戴并保存。
              </p>
              <!-- 衣柜收藏是**账号维度**（不是身份维度）：没绑定时左侧列表照常渲染，
                   避免用户以为「商店里加的收藏丢了 / 白加了」。 -->
              <p class="no-identity-line mono">
                你在「时装商店」加入衣柜的时装不会丢失 —— 左侧就是这份收藏，现在就能查看 / 移除。
              </p>
            </template>
          </el-alert>

          <template v-else>
            <el-alert
              v-if="loadoutError"
              class="panel-error"
              type="error"
              :closable="false"
              show-icon
              title="穿戴配置加载失败"
            >
              <template #default>
                <el-button size="small" text type="primary" @click="loadLoadout">重试</el-button>
              </template>
            </el-alert>
            <div v-for="slot in FASHION_SLOTS" :key="slot.value" class="slot-row">
              <div class="slot-head">
                <span class="slot-label">{{ slot.label }}</span>
                <el-button
                  v-if="slotSelection(slot.value)"
                  size="small"
                  text
                  type="danger"
                  :aria-label="`卸下${slot.label}`"
                  @click="unequip(slot.value)"
                >
                  卸下
                </el-button>
              </div>

              <template v-if="slotSelection(slot.value)">
                <div class="slot-cosmetic">{{ cosmeticName(slot.value) }}</div>
                <div class="scale-row">
                  <span class="mono scale-label">缩放</span>
                  <el-slider
                    :model-value="slotScale(slot.value)"
                    :aria-label="`${slot.label}缩放`"
                    :min="MIN_SCALE"
                    :max="MAX_SCALE"
                    :step="0.05"
                    size="small"
                    class="scale-slider"
                    @input="(value: number | number[]) => setScale(slot.value, value)"
                  />
                  <span class="mono scale-value">{{ slotScale(slot.value).toFixed(2) }}</span>
                </div>
              </template>
              <div v-else class="slot-empty mono">未穿戴</div>
            </div>

            <div class="save-row">
              <span class="mono save-hint" role="status" aria-live="polite">
                {{ loadingLoadout ? '正在读取穿戴配置…' : dirty ? '有未保存的修改' : '已与服务器同步' }}
              </span>
              <el-button type="primary" :loading="saving" :disabled="saveDisabled" @click="save">
                保存穿戴
              </el-button>
            </div>
          </template>
        </div>
      </el-card>
    </div>

    <!-- 详情弹窗（B3 / P2-7）：点卡片 = 打开详情，穿戴放到弹窗里二次确认 -->
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
        <!-- 只在真的有预览图（且未下架）时给容器 role=img / aria-label（P3） -->
        <div
          class="detail-preview"
          :role="detailUrl && !detailItem.missing ? 'img' : undefined"
          :aria-label="detailUrl && !detailItem.missing ? `${displayName(detailItem)} 预览图` : undefined"
        >
          <el-image
            v-if="detailUrl && !detailItem.missing"
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
          <span v-else-if="detailItem.missing" class="detail-fallback missing-fallback">已下架</span>
          <span v-else class="detail-fallback">{{ (displayName(detailItem) || '?').slice(0, 1) }}</span>
        </div>
        <p v-if="detailUrl && !detailItem.missing" class="zoom-hint mono">
          点击图片查看大图（可双指缩放 / 点空白处关闭）
        </p>
        <p v-if="detailHint" class="detail-hint mono">{{ detailHint }}</p>
        <el-descriptions :column="1" border size="small" class="detail-meta">
          <el-descriptions-item label="ID">
            <span class="mono">{{ detailItem.cosmeticId }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="槽位">{{ detailSlotLabel }}</el-descriptions-item>
          <el-descriptions-item label="时装包">
            <span class="mono">{{ detailItem.packId || '—' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="当前缩放">
            <span class="mono">{{ detailScale.toFixed(2) }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="状态">
            <span v-if="detailItem.missing" class="missing-tag">已下架（不可穿戴）</span>
            <template v-else>{{ isEquipped(detailItem) ? '已穿戴（保存后生效）' : '未穿戴' }}</template>
          </el-descriptions-item>
          <el-descriptions-item label="加入时间">
            <span class="mono">{{ fmtApiTime(detailItem.addedAt) }}</span>
          </el-descriptions-item>
        </el-descriptions>
      </div>
      <template #footer>
        <el-button @click="detailOpen = false">关闭</el-button>
        <el-button
          type="primary"
          :disabled="!detailItem || isEquipped(detailItem) || detailItem.missing"
          @click="equipFromDetail"
        >
          {{ detailEquipLabel }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  CATEGORY_LABELS,
  FASHION_SLOTS,
  fashionApi,
  fashionErrorCode,
  fashionErrorMessage,
  notifyFashionError,
  type FashionIdentity,
  type WardrobeItem,
} from '@/api/fashion'
import { fmtApiTime } from '@/utils/time'
import { useResponsivePageSize } from '@/composables/useResponsivePageSize'
import { useScrollLock } from '@/composables/useScrollLock'
import { useFashionImageViewer } from '@/composables/useFashionImageViewer'

const route = useRoute()
const router = useRouter()

// P2-6：详情弹窗 / 全屏大图打开时锁住 .app-main（真正的滚动容器）
const { lock, unlock } = useScrollLock()
// P1-1 + P2-9：viewer 的返回键 / 横竖屏重置 / 滚动锁
const { onShow: onViewerShow, onClose: onViewerClose } = useFashionImageViewer()
// P3：路由切走后网络回来不再写状态、不弹提示
let isUnmounted = false

// 缩放区间：与后端 core/fashion_service.py:54-55 的 0.25 / 4.0 对齐
const MIN_SCALE = 0.25
const MAX_SCALE = 4
/** 合法槽位集合：用于过滤后端返回的历史脏数据（未知 slot 会让保存整条 400） */
const validSlots = new Set<string>(FASHION_SLOTS.map((slot) => slot.value))

const identities = ref<FashionIdentity[]>([])
const activeIdentityId = ref('')
// 我的衣柜（收藏制）：左侧面板的唯一数据源。原来是「全站目录」，用户抱怨
// 「衣柜里还是全时装都在」，所以现在只渲染玩家主动加入的收藏。
const wardrobe = ref<WardrobeItem[]>([])
// 预览图加载失败（条目填了 assetKey 但资产还没上传 / 文件被删）的时装：
// P2-1 改成按**失败的 URL**记录，避免旧请求的 error 误杀换了 assetKey/sha256 的新预览
const brokenThumbUrls = ref<Set<string>>(new Set())
const activeCategory = ref('all')
// B1：衣柜件数多了只能靠肉眼扫 → 加搜索（名称 / ID，大小写不敏感）
const searchKeyword = ref('')
// B2 / P1-2：一次渲染全部卡片（每张卡还要跑 4 次 isEquipped）→ 分页；
// 移动端一页少渲染一些全尺寸大图（后端本期无缩略图端点），避免解码 OOM
const wardrobePage = ref(1)
const pageSize = useResponsivePageSize(60, 16)
// B3：详情弹窗（大图 + 元信息）
const detailOpen = ref(false)
const detailItem = ref<WardrobeItem | null>(null)
const detailRetrySeq = ref(0)
const detailImageError = ref(false)
const selection = ref<Record<string, { cosmeticId: string; scale: number }>>({})
const missingSlots = ref<Set<string>>(new Set())
const savedSnapshot = ref('{}')
const loadingWardrobe = ref(false)
const loadingLoadout = ref(false)
const loadingIdentities = ref(false)
const saving = ref(false)
const noIdentity = ref(false)
const identitiesError = ref(false)
const wardrobeError = ref(false)
const loadoutError = ref(false)

// ── 批量管理 / 删除 ──
// batchMode 开启后「点卡片」从「穿戴」变成「勾选」，避免误穿；
// 单件删除与批量删除都只动衣柜收藏，不动云端穿戴（见 dropWornSelection）。
const batchMode = ref(false)
const selectedIds = ref<Set<string>>(new Set())
// 正在删除的单件（给 ✕ 按钮上 disabled，防连点重复弹确认框）
const deletingIds = ref<Set<string>>(new Set())
const batchDeleting = ref(false)
// 穿戴配置里后端回填的名称（slot → name）：衣柜只存收藏，被穿戴但没收藏的时装
// 名字只能从这里取，否则右侧「当前穿戴」会退化成裸 cosmeticId。
const loadoutLabels = ref<Record<string, string>>({})

// 请求竞态保护：切换身份时旧响应不得覆盖新身份的数据
let loadoutSeq = 0
// 身份列表请求竞态保护：重试期间只采纳最后一次响应
let identitiesSeq = 0
// 衣柜请求竞态保护：连点重试时只采纳最后一次响应
let wardrobeSeq = 0
// 记住切换前的身份，用于用户取消「放弃未保存修改」时回退下拉框
let previousIdentityId = ''
// 身份失效自愈：正在重拉身份列表时不再嵌套触发，避免 404/403 循环
let identityRecovering = false

/**
 * 把当前生效的身份写回 URL（?identityId=）。
 *
 * 为什么需要：绑定页「去衣柜」会带上 identityId，但用户在下拉框里换了身份后
 * URL 仍是旧值 —— 此时刷新 / 收藏 / 分享都会回到另一个身份，看到的穿戴也不是
 * 刚才那一份。用 replace 而不是 push：切身份不该在浏览器历史里堆记录，
 * 否则用户按「后退」会莫名其妙切回上一个身份。
 */
function syncIdentityQuery(): void {
  const current = typeof route.query.identityId === 'string' ? route.query.identityId : ''
  const next = activeIdentityId.value
  if (current === next) return
  const query = { ...route.query }
  if (next) query.identityId = next
  else delete query.identityId
  void router.replace({ query })
}

/** 切换身份前确认是否丢弃未保存修改；返回 true 表示可以继续 */
async function confirmDiscardChanges(): Promise<boolean> {
  if (!dirty.value) return true
  try {
    await ElMessageBox.confirm(
      '切换 Minecraft 身份会丢弃当前未保存的穿戴修改，是否继续？',
      '切换身份',
      { type: 'warning', confirmButtonText: '切换', cancelButtonText: '取消' }
    )
    return true
  } catch {
    return false
  }
}

/**
 * 身份在别处被解绑 / 转绑（后端 404 identity_not_found、403 forbidden）时自愈。
 *
 * 为什么需要：衣柜页的身份列表只在挂载时拉一次。若用户另一个标签页里解绑了
 * 这个 MC 账号，本页的「重试」会拿着同一个失效 identityId 反复失败，除了手动
 * 刷新页面没有任何出路 —— 等于把用户卡死在错误态。
 *
 * @returns 是否已回落到有效身份并重新读到穿戴配置
 */
async function recoverIdentity(staleId: string): Promise<boolean> {
  if (identityRecovering || !staleId) return false
  identityRecovering = true
  try {
    await loadIdentities()
    if (identitiesError.value || noIdentity.value) return false
    // loadIdentities 只在原身份不存在时才会改 activeIdentityId；没变说明不是身份问题
    if (!activeIdentityId.value || activeIdentityId.value === staleId) return false
    syncIdentityQuery()
    await loadLoadout()
    return true
  } finally {
    identityRecovering = false
  }
}

const currentIdentity = computed(
  () => identities.value.find((item) => item.id === activeIdentityId.value) || null
)

/**
 * 全局 ID 的 localId（`pack:local` → `local`；没有冒号时原样返回）。
 *
 * 为什么需要：后端把 loadout 统一存成全局 ID（`<packId>:<localId>`），而目录里可能还有
 * 历史「简单 ID」条目（只有 localId）。两种形态指同一件时装，UI 匹配必须兼容。
 */
function localIdOf(cosmeticId: string): string {
  const idx = cosmeticId.indexOf(':')
  return idx > 0 && idx < cosmeticId.length - 1 ? cosmeticId.slice(idx + 1) : cosmeticId
}

/**
 * 两个 ID 是否指同一件时装。
 *
 * 只有「一个全局 + 一个简单、且 localId 相同」才视为同一件；两个不同的全局 ID
 * （不同包的同名 localId）或两个不同的简单 ID 都不算 —— 避免串包误高亮。
 */
function sameCosmetic(a: string, b: string): boolean {
  if (a === b) return true
  const aGlobal = a.includes(':')
  const bGlobal = b.includes(':')
  if (aGlobal === bGlobal) return false
  return localIdOf(a) === localIdOf(b)
}

const wardrobeMap = computed(() => {
  const map = new Map<string, WardrobeItem>()
  for (const item of wardrobe.value) {
    map.set(item.cosmeticId, item)
    // 全局 ID 再登记一个 localId 别名，供 loadout（可能存简单 ID）反查名称
    const local = localIdOf(item.cosmeticId)
    if (local !== item.cosmeticId && !map.has(local)) map.set(local, item)
  }
  return map
})

/**
 * 卡片/弹窗显示名：`name` 为空时回落 `cosmeticId`（B6）。
 * 后端 `name` 默认是空串，直接渲染会得到一张空白卡片。
 */
function displayName(item: WardrobeItem): string {
  return (item.name || '').trim() || item.cosmeticId
}

const filteredWardrobe = computed(() => {
  const keyword = searchKeyword.value.trim().toLowerCase()
  return wardrobe.value.filter((item) => {
    if (activeCategory.value !== 'all' && itemSlot(item) !== activeCategory.value) return false
    if (!keyword) return true
    return (
      displayName(item).toLowerCase().includes(keyword) ||
      item.cosmeticId.toLowerCase().includes(keyword)
    )
  })
})

const pagedWardrobe = computed(() => {
  const start = (wardrobePage.value - 1) * pageSize.value
  return filteredWardrobe.value.slice(start, start + pageSize.value)
})

// 搜索 / 切分类后回到第一页，否则可能停在一个空页上
watch([searchKeyword, activeCategory], () => {
  wardrobePage.value = 1
})
// P1-2：视口变化让 pageSize 改变时把页码夹回有效范围
watch(pageSize, () => {
  const maxPage = Math.max(1, Math.ceil(filteredWardrobe.value.length / pageSize.value))
  if (wardrobePage.value > maxPage) wardrobePage.value = maxPage
})

/** 缩放夹紧（B9）：后端两条写路径都会夹，但载入时也夹一次，避免「值 99 / 滑块卡在 4」 */
function clampScale(value: unknown): number {
  const num = Number(value)
  if (!Number.isFinite(num)) return 1
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, num))
}

// 预览图地址只算一次，模板里用 Map 取值（避免 v-if 与 :src 各自重复计算）
// P2-3：URL 带 sha256 版本戳，管理员替换资产后不会被 immutable 缓存卡一年
const previewUrls = computed(() => {
  const map = new Map<string, string>()
  for (const item of wardrobe.value) {
    if (!item.assetKey) continue
    if (!/\.(png|jpe?g|webp|gif)$/i.test(item.assetKey)) continue
    const url = fashionApi.assetUrl(item.assetKey, item.sha256)
    if (url) map.set(item.cosmeticId, url)
  }
  return map
})

function thumbUrl(item: WardrobeItem): string {
  return previewUrls.value.get(item.cosmeticId) || ''
}

/** 有收藏但筛不出来时的空态文案（真正的空衣柜有单独的引导，不共用这里） */
const emptyFilterText = computed(() => {
  const keyword = searchKeyword.value.trim()
  if (keyword) return `衣柜里没有匹配「${keyword}」的时装`
  return activeCategory.value === 'all' ? '衣柜里暂无时装' : '该分类下暂无收藏的时装'
})

const equippedCount = computed(() => Object.keys(selection.value).length)

function snapshot(): string {
  const ordered: Record<string, { cosmeticId: string; scale: number }> = {}
  for (const key of Object.keys(selection.value).sort()) {
    ordered[key] = { ...selection.value[key] }
  }
  return JSON.stringify(ordered)
}

const dirty = computed(() => snapshot() !== savedSnapshot.value)

/**
 * 两处「保存穿戴」按钮共用一个 disabled 条件（B8）。
 * 原来顶部按钮含 `noIdentity / identitiesError`、底部按钮不含，是「两处真理」；
 * 现在虽然被 `v-else` 挡住不可达，但一旦布局改动就会漏。
 */
const saveDisabled = computed(
  () =>
    !dirty.value ||
    noIdentity.value ||
    identitiesError.value ||
    loadingIdentities.value ||
    loadingLoadout.value ||
    loadoutError.value ||
    saving.value
)

// ── 详情弹窗 ──
/** 详情大图 URL；重试时追加时间戳强制重新请求（P2-2） */
const detailUrl = computed(() => {
  const item = detailItem.value
  if (!item) return ''
  const base = previewUrls.value.get(item.cosmeticId) || ''
  if (!base) return ''
  if (detailRetrySeq.value <= 0) return base
  return `${base}${base.includes('?') ? '&' : '?'}t=${detailRetrySeq.value}`
})

/** 区分「已下架」「没有资产」「不是图片格式」「图片加载失败」，文案不再张冠李戴 */
const detailHint = computed(() => {
  const item = detailItem.value
  if (!item) return ''
  if (item.missing) return '这件时装已从站点目录移除（已下架），无法预览 / 穿戴，但仍可从衣柜移除。'
  if (!item.assetKey) return '这件时装还没有上传资产（assetKey 为空）。'
  if (!/\.(png|jpe?g|webp|gif)$/i.test(item.assetKey)) {
    return '该资产不是图片格式（glb / json / zip 等），网页无法预览。'
  }
  if (detailImageError.value) return '预览图加载失败，可点上方「重试」重新加载；若持续失败请联系管理员检查资产文件。'
  return ''
})

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

const detailSlotLabel = computed(() => {
  const item = detailItem.value
  if (!item) return ''
  return CATEGORY_LABELS[itemSlot(item)] || item.category || '其它'
})

const detailScale = computed(() => {
  const item = detailItem.value
  if (!item) return 1
  return selection.value[itemSlot(item)]?.scale ?? 1
})

/** 详情弹窗主按钮文案：已下架 / 已穿戴时给出「为什么点不动」的原因，而不是灰着一个「穿戴」 */
const detailEquipLabel = computed(() => {
  const item = detailItem.value
  if (!item) return '穿戴'
  if (item.missing) return '已下架，不可穿戴'
  if (isEquipped(item)) return '已穿戴'
  return `穿戴到${detailSlotLabel.value}`
})

function openDetail(item: WardrobeItem): void {
  // P2-12：操作前校验条目仍在当前衣柜数据里，避免对过期引用开弹窗
  if (!wardrobe.value.some((row) => row.cosmeticId === item.cosmeticId)) {
    ElMessage.warning('这件时装已不在衣柜里，请刷新后再试')
    return
  }
  detailRetrySeq.value = 0
  detailImageError.value = false
  detailItem.value = item
  detailOpen.value = true
}

function onDetailOpen(): void {
  lock('wardrobe-detail')
}

function onDetailClosed(): void {
  unlock('wardrobe-detail')
  // P2-12 / P3：关闭后清掉引用与加载态，避免对着过期条目操作
  detailItem.value = null
  detailRetrySeq.value = 0
  detailImageError.value = false
}

/**
 * 衣柜列表刷新后，让详情弹窗跟着换成**新数组里的同一个条目**（或关掉）。
 *
 * 弹窗持有的是对象引用：不刷新引用时，弹窗里的「加入时间 / 状态」是旧快照，
 * 条目被移出衣柜后还能对着它点「穿戴」。这里按 cosmeticId 重新定位。
 */
function syncDetailWithWardrobe(): void {
  const current = detailItem.value
  if (!current) return
  const fresh = wardrobe.value.find((item) => item.cosmeticId === current.cosmeticId)
  if (fresh) {
    detailItem.value = fresh
    return
  }
  detailItem.value = null
  detailOpen.value = false
}

function equipFromDetail(): void {
  const item = detailItem.value
  if (!item) return
  // P2-12：条目可能已被别的标签页移出衣柜，操作前再校验一次
  if (!wardrobe.value.some((row) => row.cosmeticId === item.cosmeticId)) {
    ElMessage.warning('这件时装已不在衣柜里，请刷新后再试')
    detailOpen.value = false
    return
  }
  if (item.missing) {
    ElMessage.warning('这件时装已下架，无法穿戴')
    return
  }
  equip(item)
  detailOpen.value = false
}

/** 衣柜分类归一化：大小写/非法分类归入合法槽位，避免写入后端不认的槽位 */
function itemSlot(item: WardrobeItem): string {
  const category = String(item.category || 'other').trim().toLowerCase()
  return FASHION_SLOTS.some((slot) => slot.value === category) ? category : 'other'
}

function isEquipped(item: WardrobeItem): boolean {
  const selected = selection.value[itemSlot(item)]
  return !!selected && sameCosmetic(selected.cosmeticId, item.cosmeticId)
}

/** 卡片无障碍标签：批量模式下读「选中状态」，否则读「穿戴状态 + 是否已下架」 */
function cardAriaLabel(item: WardrobeItem): string {
  const name = displayName(item)
  const slot = CATEGORY_LABELS[itemSlot(item)] || item.category || '其它'
  if (batchMode.value) {
    return `${isSelected(item) ? '已选中' : '未选中'}，${name}，${slot}`
  }
  if (item.missing) return `${name}，${slot}，已下架不可穿戴`
  return `${name}，${slot}，${isEquipped(item) ? '已穿戴' : '未穿戴'}`
}

/** P2-1：预览图 404 / 解码失败按**失败的 URL**记录，旧请求不会误杀新预览 */
function markThumbBroken(event: Event): void {
  const el = event.target as HTMLImageElement | null
  const url = el?.getAttribute('src') || el?.currentSrc || ''
  if (!url || brokenThumbUrls.value.has(url)) return
  brokenThumbUrls.value = new Set(brokenThumbUrls.value).add(url)
}

function cosmeticName(slot: string): string {
  const selected = selection.value[slot]
  if (!selected) return ''
  // 先精确、再按 localId 兜底（衣柜简单 ID ↔ loadout 全局 ID 的历史数据）
  const item =
    wardrobeMap.value.get(selected.cosmeticId) ||
    wardrobeMap.value.get(localIdOf(selected.cosmeticId))
  if (item) return displayName(item)
  // 衣柜只存「收藏」，正在穿戴的时装可能早就没收藏了（或本就不是从衣柜选的）：
  // 此时用 loadout 响应里后端回填的 name 兜底，别把右侧面板退化成裸 ID
  const fromLoadout = (loadoutLabels.value[slot] || '').trim()
  if (fromLoadout) return fromLoadout
  return missingSlots.value.has(slot) ? `${selected.cosmeticId}（已下架）` : selected.cosmeticId
}

function slotSelection(slot: string) {
  return selection.value[slot]
}

function slotScale(slot: string): number {
  return selection.value[slot]?.scale ?? 1
}

/** 槽位内容一换，它就不再是「已下架」条目：必须清掉标记 */
function clearMissingSlot(slot: string): void {
  if (!missingSlots.value.has(slot)) return
  const next = new Set(missingSlots.value)
  next.delete(slot)
  missingSlots.value = next
}

/**
 * 卡片激活（P2-7）：普通模式 = **打开详情**（穿戴改到详情弹窗里），批量模式 = 勾选。
 *
 * 原来「点整张卡 = 立即穿戴」，手指稍偏就会误改 selection / dirty，甚至误删；
 * 穿戴是会影响「保存穿戴」的写操作，放到详情弹窗里二次确认最安全。
 * 批量模式下仍接管卡片点击，让整张卡（含键盘 Enter/Space）都是勾选热区。
 */
function onCardActivate(item: WardrobeItem): void {
  if (batchMode.value) {
    toggleSelect(item)
    return
  }
  openDetail(item)
}

function equip(item: WardrobeItem): void {
  const slot = itemSlot(item)
  const existing = selection.value[slot]
  selection.value = {
    ...selection.value,
    [slot]: { cosmeticId: item.cosmeticId, scale: existing?.scale ?? 1 },
  }
  // 衣柜里点得到的时装一定没下架。不清标记的话，保存时 save() 会把这件
  // 刚选好的时装当成「已下架」自动移除（只留一句误导性的警告），
  // 用户以为换装成功、进游戏却是旧的。
  clearMissingSlot(slot)
}

function unequip(slot: string): void {
  const next = { ...selection.value }
  delete next[slot]
  selection.value = next
  clearMissingSlot(slot)
}

function setScale(slot: string, value: number | number[]): void {
  const current = selection.value[slot]
  if (!current) return
  const raw = Array.isArray(value) ? value[0] : value
  const scale = clampScale(raw)
  selection.value = { ...selection.value, [slot]: { ...current, scale } }
}

async function loadIdentities(): Promise<void> {
  const seq = ++identitiesSeq
  loadingIdentities.value = true
  identitiesError.value = false
  try {
    const { data } = await fashionApi.myIdentities()
    if (isUnmounted || seq !== identitiesSeq) return // 已被更新的请求取代 / 组件已卸载
    identities.value = data.items || []
    const queryId = typeof route.query.identityId === 'string' ? route.query.identityId : ''
    const activeStillExists = identities.value.some((item) => item.id === activeIdentityId.value)
    if (!activeStillExists) {
      // 原身份已被解绑/删除时回落到 query 指定或第一个，避免拿失效 identityId 请求
      const preferred =
        queryId && identities.value.some((item) => item.id === queryId)
          ? queryId
          : identities.value[0]?.id || ''
      activeIdentityId.value = preferred
    }
    previousIdentityId = activeIdentityId.value
    noIdentity.value = identities.value.length === 0
    // URL 里的 identityId 已失效（例如该 MC 账号刚被解绑）：改写成实际生效的身份，
    // 否则地址栏一直挂着一个后端不认的身份，刷新/分享出去都还要再回落一次
    if (queryId && queryId !== activeIdentityId.value) syncIdentityQuery()
  } catch (error) {
    if (isUnmounted || seq !== identitiesSeq) return
    identitiesError.value = true
    notifyFashionError(error, '读取绑定信息失败')
  } finally {
    if (!isUnmounted && seq === identitiesSeq) loadingIdentities.value = false
  }
}

async function reloadIdentities(): Promise<void> {
  await loadIdentities()
  if (!identitiesError.value) await loadLoadout()
}

/**
 * 读我的衣柜。
 *
 * @param options.silent 静默刷新（删除成功后回读列表用）：不点亮面板 loading，
 *        避免整块卡片闪一下遮罩 —— 删除本身已经有确认框和 toast 作为反馈。
 */
async function loadWardrobe(options: { silent?: boolean } = {}): Promise<void> {
  const seq = ++wardrobeSeq
  const silent = options.silent === true
  if (!silent) loadingWardrobe.value = true
  wardrobeError.value = false
  try {
    const { data } = await fashionApi.wardrobe()
    if (isUnmounted || seq !== wardrobeSeq) return // 已被更新的请求取代 / 组件已卸载
    wardrobe.value = data.items || []
    // B4（真 bug）+ P2-1：brokenThumbUrls 原来一旦标记就永不重置 —— 管理员补传
    // 资产后玩家必须硬刷新才看得到缩略图。衣柜重新拉成功 = 资产可能已补齐，
    // 全部按 URL 重试一次（换了 sha256 的条目本来也是新 URL）。
    brokenThumbUrls.value = new Set()
    // 选中集合里可能留着已经不存在的 id（本页刚删 / 别的标签页删的）：
    // 不收敛的话「删除选中(n)」会带着幽灵 id 去请求，计数也会骗人
    pruneSelection()
    // 衣柜变短时把页码夹回有效范围，避免停在空页
    const maxPage = Math.max(1, Math.ceil(filteredWardrobe.value.length / pageSize.value))
    if (wardrobePage.value > maxPage) wardrobePage.value = maxPage
    // 衣柜空了就没有可管理的东西，直接退出批量模式，别让工具栏停在空状态
    if (wardrobe.value.length === 0 && batchMode.value) exitBatchMode()
    // 详情弹窗持有的是**旧数组里的对象引用**：这件时装已不在衣柜里（本页删 / 别的
    // 标签页删）时必须关掉弹窗，否则用户还能对着一个已移除的条目点「穿戴」，
    // 而弹窗里的名称/加入时间也是过期数据
    syncDetailWithWardrobe()
  } catch (error) {
    if (isUnmounted || seq !== wardrobeSeq) return
    wardrobeError.value = true
    notifyFashionError(error, '读取我的衣柜失败')
  } finally {
    if (!isUnmounted && seq === wardrobeSeq) loadingWardrobe.value = false
  }
}

// ── 批量管理 / 删除 ────────────────────────────────────────────────────────

function isSelected(item: WardrobeItem): boolean {
  return selectedIds.value.has(item.cosmeticId)
}

function toggleSelect(item: WardrobeItem): void {
  const next = new Set(selectedIds.value)
  if (next.has(item.cosmeticId)) next.delete(item.cosmeticId)
  else next.add(item.cosmeticId)
  selectedIds.value = next
}

function enterBatchMode(): void {
  if (wardrobe.value.length === 0) return
  batchMode.value = true
  selectedIds.value = new Set()
}

/** 退出批量管理：清空勾选，卡片恢复「点击即穿戴」 */
function exitBatchMode(): void {
  batchMode.value = false
  selectedIds.value = new Set()
}

/** 全选作用于「当前筛选结果」（跨分页），而不是只有当前这一页 —— 与筛选语义一致 */
const filteredIds = computed(() => filteredWardrobe.value.map((item) => item.cosmeticId))

const allFilteredSelected = computed(
  () => filteredIds.value.length > 0 && filteredIds.value.every((id) => selectedIds.value.has(id))
)

/**
 * 批量模式下「已勾选、但被当前筛选 / 搜索隐藏」的件数。
 *
 * 「全选」作用于筛选结果全集（跨分页），勾选也会跨筛选保留 —— 用户切了分类或搜了
 * 关键词之后，看不见的勾选仍会跟着「删除选中(n)」一起被删。把差额显式说出来，
 * 避免「我只想删当前这一页」的误操作。
 */
const hiddenSelectedCount = computed(() => {
  if (selectedIds.value.size === 0) return 0
  const visible = new Set(filteredIds.value)
  let hidden = 0
  for (const id of selectedIds.value) if (!visible.has(id)) hidden += 1
  return hidden
})

function selectAllFiltered(): void {
  selectedIds.value = new Set(filteredIds.value)
}

function clearSelection(): void {
  selectedIds.value = new Set()
}

/** 列表刷新后收敛选中集合：只保留还存在于衣柜里的 id */
function pruneSelection(): void {
  if (selectedIds.value.size === 0) return
  const alive = new Set(wardrobe.value.map((item) => item.cosmeticId))
  const next = new Set<string>()
  for (const id of selectedIds.value) if (alive.has(id)) next.add(id)
  if (next.size !== selectedIds.value.size) selectedIds.value = next
}

/**
 * 把刚被移出衣柜的时装，从本地 `selection` 里清掉，并返回被清掉的槽位。
 *
 * 为什么只改本地、**不**自动调 putLoadout：
 * 1) 删除是衣柜侧的动作，穿戴是另一份数据；自动提交会把用户其它没保存的
 *    修改一并写上去，等于替他做了决定；
 * 2) 衣柜页的模型一直是「本地改 → 点保存穿戴」，自动提交会绕过这个约定。
 * 所以这里只清槽位 + 让 dirty 变 true，并明确提示用户去保存。
 */
function dropWornSelection(removedIds: Set<string>): string[] {
  const dropped: string[] = []
  const next = { ...selection.value }
  for (const [slot, value] of Object.entries(selection.value)) {
    for (const id of removedIds) {
      if (sameCosmetic(value.cosmeticId, id)) {
        delete next[slot]
        dropped.push(slot)
        break
      }
    }
  }
  if (dropped.length === 0) return []
  selection.value = next
  // 槽位已经空了，它就不再是「已下架条目」，标记必须一起清掉
  const remaining = new Set(missingSlots.value)
  for (const slot of dropped) remaining.delete(slot)
  missingSlots.value = remaining
  return dropped
}

/** 被清掉的槽位中文名，用于提示（「披风、翅膀」） */
function slotLabelsOf(slots: string[]): string {
  return slots.map((slot) => CATEGORY_LABELS[slot] || slot).join('、')
}

/** 单件移除：确认 → DELETE → 回读列表 */
async function removeOne(item: WardrobeItem): Promise<void> {
  if (deletingIds.value.has(item.cosmeticId) || batchDeleting.value) return
  const name = displayName(item)
  try {
    await ElMessageBox.confirm(`确定从衣柜移除「${name}」？`, '移除时装', {
      type: 'warning',
      confirmButtonText: '移除',
      cancelButtonText: '取消',
    })
  } catch {
    return // 用户取消
  }
  deletingIds.value = new Set(deletingIds.value).add(item.cosmeticId)
  try {
    await fashionApi.removeFromWardrobe(item.cosmeticId)
    // 服务端已确认删除 → 先本地摘掉这张卡；万一随后的回读失败，列表也不会
    // 继续显示一件已经不存在的时装（回读成功会以服务端数据覆盖一次）
    wardrobe.value = wardrobe.value.filter((row) => row.cosmeticId !== item.cosmeticId)
    const dropped = dropWornSelection(new Set([item.cosmeticId]))
    await loadWardrobe({ silent: true })
    if (dropped.length > 0) {
      ElMessage.warning(
        `已从衣柜移除「${name}」，同时取消了${slotLabelsOf(dropped)}槽位的穿戴（记得点保存穿戴）`
      )
    } else {
      ElMessage.success(`已从衣柜移除「${name}」`)
    }
  } catch (error) {
    // 失败时列表原样保留（没做本地删除），用户可以直接重试
    notifyFashionError(error, '移除失败')
  } finally {
    const rest = new Set(deletingIds.value)
    rest.delete(item.cosmeticId)
    deletingIds.value = rest
  }
}

/**
 * 批量移除：确认 → batch-delete → 回读列表并退出管理模式。
 *
 * **分片提交**：后端 `POST /wardrobe/batch-delete` 的上限是**去重后 500 条**
 * （`fashion_service.MAX_WARDROBE_BATCH_DELETE`，超出返回 400 `too_many_items`）。
 * 而「全选」作用于**当前筛选结果（跨分页）**，衣柜件数多时一次勾选可能超过 500 件 ——
 * 不分片的话用户只会看到一句「一次最多删除 500 件」却无从下手（要么手动取消勾选，
 * 要么反复操作）。这里按 500 一件一件切开发，逐片累加 `removed`。
 *
 * 分片失败时的语义：前面几片可能已经删成功，后面没删 —— 所以失败也要
 * **把已删的从本地列表摘掉**并提示「已删除 X 件，剩余失败可重试」，否则
 * 用户重试会把已删的再发一遍（幂等无害，但计数会骗人）。
 */
const BATCH_DELETE_CHUNK = 500

async function batchDelete(): Promise<void> {
  if (batchDeleting.value) return
  const ids = [...selectedIds.value]
  if (ids.length === 0) return
  try {
    await ElMessageBox.confirm(`确定从衣柜移除选中的 ${ids.length} 件时装？`, '批量移除', {
      type: 'warning',
      confirmButtonText: '移除',
      cancelButtonText: '取消',
    })
  } catch {
    return // 用户取消
  }
  batchDeleting.value = true
  let removedTotal = 0
  const removedIdSet = new Set<string>()
  try {
    for (let start = 0; start < ids.length; start += BATCH_DELETE_CHUNK) {
      const chunk = ids.slice(start, start + BATCH_DELETE_CHUNK)
      const { data } = await fashionApi.batchRemoveFromWardrobe(chunk)
      // 后端可能只删掉其中一部分（别的标签页已删）：以它返回的 removed 为准显示数量
      removedTotal += typeof data.removed === 'number' ? data.removed : chunk.length
      for (const id of chunk) removedIdSet.add(id)
      // 每片成功就先从本地摘掉：后面某片失败时，已删的卡片不会留在列表里
      if (removedIdSet.size > 0) {
        wardrobe.value = wardrobe.value.filter((row) => !removedIdSet.has(row.cosmeticId))
      }
    }
    const dropped = dropWornSelection(removedIdSet)
    // 成功即退出管理模式（并在 loadWardrobe 里清掉勾选）：
    // 一次批量清理通常就结束了，留在管理模式里只剩一个空选择没有意义
    exitBatchMode()
    await loadWardrobe({ silent: true })
    if (dropped.length > 0) {
      ElMessage.warning(
        `已从衣柜移除 ${removedTotal} 件，同时取消了${slotLabelsOf(dropped)}槽位的穿戴（记得点保存穿戴）`
      )
    } else {
      ElMessage.success(`已从衣柜移除 ${removedTotal} 件时装`)
    }
  } catch (error) {
    // 部分分片可能已经删成功：先把它们从本地列表摘掉、收敛勾选，再报错
    if (removedIdSet.size > 0) {
      wardrobe.value = wardrobe.value.filter((row) => !removedIdSet.has(row.cosmeticId))
      dropWornSelection(removedIdSet)
      selectedIds.value = new Set(
        [...selectedIds.value].filter((id) => !removedIdSet.has(id))
      )
      await loadWardrobe({ silent: true })
      ElMessage.error(
        `${fashionErrorMessage(error, '批量移除失败')}（已成功移除 ${removedTotal} 件，剩余可重试）`
      )
    } else {
      // 一件都没删成功：保留勾选，用户可直接重试（不用重新挑一遍）
      notifyFashionError(error, '批量移除失败')
    }
  } finally {
    batchDeleting.value = false
  }
}

/**
 * 批量管理模式下按 Esc 退出。
 *
 * 为什么：进入批量模式后卡片点击变成勾选，用户如果只是想「看一眼」，
 * 需要一条不必精确点中「完成」小按钮的退路（键盘用户尤其需要）。
 *
 * 监听挂在页面根节点（模板上的 @keydown.esc）而不是 window：
 * 确认框 / 详情弹窗打开时焦点在它们的遮罩里，Esc 不会冒泡到这里，
 * 于是「关弹窗」和「退出批量模式」不会同时发生。
 */
function onEscape(): void {
  if (batchMode.value) exitBatchMode()
}

async function loadLoadout(): Promise<void> {
  const seq = ++loadoutSeq
  loadoutError.value = false
  if (noIdentity.value) {
    loadingLoadout.value = false
    selection.value = {}
    missingSlots.value = new Set()
    loadoutLabels.value = {}
    savedSnapshot.value = snapshot()
    return
  }
  loadingLoadout.value = true
  const identityId = activeIdentityId.value || undefined
  try {
    const { data } = await fashionApi.getLoadout(identityId)
    if (isUnmounted || seq !== loadoutSeq) return // 已被更新的请求取代 / 组件已卸载
    const next: Record<string, { cosmeticId: string; scale: number }> = {}
    const missing = new Set<string>()
    // 衣柜只存收藏，正在穿戴的时装可能不在衣柜里：名称用 loadout 回填的 name 兜底
    const labels: Record<string, string> = {}
    for (const item of data.items || []) {
      // B10：未知 slot 不可见但会计入 equippedCount、并在保存时回传 ——
      // 后端 put_loadout 对未知 slot 是**整条 400 失败**，所以载入时直接丢弃。
      if (!validSlots.has(item.slot)) continue
      // B9：载入时夹紧 scale（后端两条写路径都夹，这里再兜一层，避免手改 DB 后自相矛盾）
      next[item.slot] = { cosmeticId: item.cosmeticId, scale: clampScale(item.scale ?? 1) }
      labels[item.slot] = (item.name || '').trim()
      if (item.missing) missing.add(item.slot)
    }
    selection.value = next
    missingSlots.value = missing
    loadoutLabels.value = labels
    savedSnapshot.value = snapshot()
  } catch (error) {
    if (isUnmounted || seq !== loadoutSeq) return
    const code = fashionErrorCode(error)
    if (code === 'no_identity') {
      noIdentity.value = true
      selection.value = {}
      missingSlots.value = new Set()
      loadoutLabels.value = {}
      savedSnapshot.value = snapshot()
      return
    }
    loadoutError.value = true
    // 加载失败时清空选择，避免旧身份的穿戴被误存到当前身份
    selection.value = {}
    missingSlots.value = new Set()
    loadoutLabels.value = {}
    savedSnapshot.value = snapshot()
    // 身份已失效（别处解绑 / 被转绑）：先回落到有效身份再读一次，
    // 否则「重试」会一直用同一个失效 identityId 失败，用户只能手动刷新页面
    if ((code === 'identity_not_found' || code === 'forbidden') && identityId) {
      if (await recoverIdentity(identityId)) {
        ElMessage.info('该 Minecraft 身份已失效，已自动切换到当前账号下的有效身份')
        return
      }
    }
    notifyFashionError(error, '读取穿戴配置失败')
  } finally {
    if (!isUnmounted && seq === loadoutSeq) loadingLoadout.value = false
  }
}

async function onIdentityChange(): Promise<void> {
  if (!(await confirmDiscardChanges())) {
    activeIdentityId.value = previousIdentityId
    return
  }
  previousIdentityId = activeIdentityId.value
  // 下拉框换身份后同步 URL：刷新/收藏/分享都停在这一个身份上
  syncIdentityQuery()
  await loadLoadout()
}

// URL 里的 identityId 变化时反向同步下拉框与穿戴数据，避免「地址栏是 A、页面是 B」：
// 侧边栏「我的衣柜」是不带 query 的链接，浏览器前进/后退也可能落到别的 identityId 上。
watch(
  () => route.query.identityId,
  async (raw) => {
    const next = typeof raw === 'string' ? raw : ''
    if (!next) {
      // query 被清掉（例如点了侧边栏同名链接）：保留当前身份，把 URL 补回去
      syncIdentityQuery()
      return
    }
    if (next === activeIdentityId.value) return
    // 未知 / 无权访问的身份：切过去后端只会 403/404，忽略并把 URL 改回当前身份
    if (!identities.value.some((item) => item.id === next)) {
      syncIdentityQuery()
      return
    }
    // 保存中不允许换身份：保存成功的快照会记到另一个身份头上
    if (saving.value) {
      syncIdentityQuery()
      return
    }
    if (!(await confirmDiscardChanges())) {
      syncIdentityQuery()
      return
    }
    activeIdentityId.value = next
    previousIdentityId = next
    await loadLoadout()
  }
)

async function save(): Promise<void> {
  // 穿戴配置没加载成功时绝不允许保存：全量覆盖会把云端其它槽位清空
  if (saving.value || loadingIdentities.value || loadingLoadout.value || loadoutError.value) return
  // 已下架/已删除的时装提交上去也只会被后端跳过（warnings 里列出来），
  // 用户没动过的失效槽位在保存前先移除，免得每次保存都弹一串「未同步」
  const missingToDrop = Object.keys(selection.value).filter((slot) => missingSlots.value.has(slot))
  if (missingToDrop.length > 0) {
    const next = { ...selection.value }
    for (const slot of missingToDrop) delete next[slot]
    selection.value = next
    const remaining = new Set(missingSlots.value)
    for (const slot of missingToDrop) remaining.delete(slot)
    missingSlots.value = remaining
    ElMessage.warning(`已自动移除 ${missingToDrop.length} 件已下架时装`)
  }
  const identityId = activeIdentityId.value || undefined
  // 记录点击保存时的快照：请求期间用户继续改动的部分仍应保持「未保存」
  const snapshotAtSave = snapshot()
  saving.value = true
  try {
    const items = Object.entries(selection.value).map(([slot, value]) => ({
      slot,
      cosmeticId: value.cosmeticId,
      scale: value.scale,
    }))
    const { data } = await fashionApi.putLoadout(items, identityId)
    if ((activeIdentityId.value || undefined) !== identityId) return
    savedSnapshot.value = snapshotAtSave
    // 后端会跳过目录里不存在的条目（例如本地包里的时装），只保存其余槽位：
    // 这里必须把"被跳过"告诉用户，否则他会以为全部保存成功了。
    const skipped = data.warnings || []
    if (skipped.length > 0) {
      ElMessage.warning(
        `已保存其余槽位；${skipped.length} 件时装不在站点目录里，未同步：` +
          skipped.map((item) => item.cosmeticId).join('、')
      )
    } else {
      ElMessage.success('已保存，进入服务器后生效')
    }
  } catch (error) {
    const code = fashionErrorCode(error)
    if (code === 'no_identity') {
      noIdentity.value = true
      ElMessage.warning('请先在「时装绑定」页完成绑定')
      return
    }
    // 身份已失效（别处解绑 / 被转绑）：回落到有效身份，别让用户对着必然失败的
    // 保存按钮反复点。回落后会重读穿戴配置，本地未保存的修改随之作废 —— 必须说清楚。
    if ((code === 'identity_not_found' || code === 'forbidden') && identityId) {
      if (await recoverIdentity(identityId)) {
        ElMessage.warning('该 Minecraft 身份已失效，已自动切换身份，请确认穿戴后重新保存')
        return
      }
    }
    notifyFashionError(error, '保存失败')
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  // 同步注册（放在 await 之前）：若初始化期间用户就离开了本页，onUnmounted 已经跑过，
  // 之后再 addEventListener 会留下一个永不释放的监听器（还会按旧状态弹刷新确认）
  window.addEventListener('beforeunload', onBeforeUnload)
  await loadIdentities()
  // P3：卸载后不再继续发后续请求 / 弹窗 / 改 URL
  if (isUnmounted) return
  await Promise.all([loadWardrobe(), identitiesError.value ? Promise.resolve() : loadLoadout()])
  if (isUnmounted) return
  applyStoreDeepLink()
})

/**
 * 「时装商店」的「去穿戴」深链：`/fashion/wardrobe?cosmeticId=xxx`。
 *
 * 只做定位提示（打开详情弹窗），**不强制穿戴** —— 用户仍可在衣柜里改选、调缩放，
 * 也不该因为从商店点进来就悄悄改掉他的穿戴。
 * 处理完把 query 删掉：否则刷新 / 前进后退都会再弹一次，且地址栏会挂着一个
 * 与当前状态无关的参数。
 *
 * 注意：商店只对「已在衣柜」的时装给「去穿戴」入口，所以正常情况下一定能命中；
 * 命中不了说明这件在别的标签页被移出了衣柜 —— 提示去商店重新加入。
 */
function applyStoreDeepLink(): void {
  if (isUnmounted) return
  const cosmeticId = typeof route.query.cosmeticId === 'string' ? route.query.cosmeticId : ''
  if (!cosmeticId) return
  const item = wardrobeMap.value.get(cosmeticId) || wardrobeMap.value.get(localIdOf(cosmeticId))
  // P2-12：移动端不自动弹详情 —— 网络回来时用户可能已经在操作，突然盖一层遮罩
  // 抢焦点体验很差；改成一条提示，让用户自己点卡片进详情。
  const isMobile =
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(max-width: 768px)').matches
  if (item) {
    if (isMobile) {
      ElMessage.info(`已在衣柜中找到「${displayName(item)}」，点卡片可查看详情 / 穿戴`)
    } else {
      openDetail(item)
    }
  } else if (wardrobeError.value) {
    // 衣柜根本没读到（网络 / 500）：不能断言「这件不在你衣柜里」——
    // 那是把一次读取失败说成业务结论，用户会以为收藏丢了
    ElMessage.error('我的衣柜读取失败，暂时无法定位这件时装；可点左侧「刷新」重试')
  } else {
    ElMessage.info('这件时装还没在你的衣柜里，可到「时装商店」点「加入衣柜」后再来穿戴')
  }
  if (isUnmounted) return
  const query = { ...route.query }
  delete query.cosmeticId
  void router.replace({ query })
}

onUnmounted(() => {
  // P3：卸载后所有 pending 请求失效，不再写状态 / 弹提示 / 改 URL
  isUnmounted = true
  ++identitiesSeq
  ++wardrobeSeq
  ++loadoutSeq
  window.removeEventListener('beforeunload', onBeforeUnload)
})

/**
 * 有未保存修改时，刷新/关标签页先弹浏览器原生确认。
 *
 * 为什么需要：衣柜是「本地改 → 点保存才提交」的模型，直接刷新会把整页修改丢掉，
 * 而用户往往以为已经生效（页面上确实显示着新时装）。路由内跳转由
 * onBeforeRouteLeave 拦，浏览器级刷新/关闭只能靠 beforeunload。
 */
function onBeforeUnload(event: BeforeUnloadEvent): void {
  if (!dirty.value || saving.value) return
  event.preventDefault()
  // 部分浏览器（旧版 Chrome / Safari）仍要求设置 returnValue 才会弹确认框
  event.returnValue = ''
}

// 路由内跳转（点侧边栏去别的页面）同样要拦住未保存的修改
onBeforeRouteLeave(async () => {
  if (!dirty.value || saving.value) return true
  try {
    await ElMessageBox.confirm(
      '当前穿戴还有未保存的修改，离开后这些修改会丢失，确定离开吗？',
      '未保存的修改',
      { type: 'warning', confirmButtonText: '离开', cancelButtonText: '留在此页' }
    )
    return true
  } catch {
    return false
  }
})
</script>

<style scoped>
.wardrobe-page {
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
}

.head-actions {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.identity-select {
  width: 220px;
  max-width: 100%;
}

.identity-chip {
  color: var(--text-secondary);
  font-size: 12px;
  padding: 4px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  background: var(--bg-elevated);
}

/* 未绑定 / 身份读取失败时的说明：第一行给操作路径，第二行说明收藏不会丢。
   提示现在只出现在右侧「当前穿戴」卡片里（左侧衣柜列表照常可用），
   所以不再需要原来的整页 alert 外边距。 */
.no-identity-line {
  margin: 0;
  line-height: 1.8;
}

.no-identity-line + .no-identity-line {
  margin-top: var(--space-1);
  color: var(--text-secondary);
  font-size: 12px;
}

.link {
  color: var(--text-primary);
  text-decoration: underline;
  text-decoration-color: var(--accent);
  text-underline-offset: 2px;
}

.wardrobe-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.6fr) minmax(320px, 1fr);
  gap: var(--space-4);
}

@media (max-width: 1000px) {
  .wardrobe-grid {
    grid-template-columns: 1fr;
  }
}

.panel {
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.panel-tools {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.wardrobe-search {
  width: 200px;
  max-width: 100%;
}

/* 分类按钮在窄屏必须换行，否则横向溢出卡片 */
.wardrobe-filters {
  display: flex;
  flex-wrap: wrap;
  row-gap: var(--space-2);
  margin-top: var(--space-3);
}

.panel-header :deep(.el-radio-group) {
  flex-wrap: wrap;
  row-gap: var(--space-2);
}

.panel-title {
  font-family: var(--font-display);
  font-size: 16px;
  color: var(--text-primary);
}

.count-hint {
  color: var(--text-secondary);
  font-size: 12px;
}

/* 批量管理模式的操作提示：进模式后必须让用户知道「点卡片=勾选」这条规则变了 */
.batch-hint {
  margin: var(--space-2) 0 0;
  font-size: 12px;
  color: var(--accent);
}

/* 空衣柜引导文案（真正的空收藏，不是筛选筛空） */
.empty-hint {
  margin: 0 0 var(--space-3);
  font-size: 13px;
  color: var(--text-secondary);
}

.panel-error {
  margin-bottom: var(--space-3);
}

.wardrobe-body,
.loadout-body {
  min-height: 200px;
}

.wardrobe-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(132px, 1fr));
  gap: var(--space-3);
}

.cosmetic-card {
  /* ✕ 移除按钮与批量勾选框都绝对定位在卡片角上 */
  position: relative;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--bg-elevated);
  padding: var(--space-2);
  cursor: pointer;
  transition: border-color 150ms var(--ease-out), transform 150ms var(--ease-out),
    background-color 150ms var(--ease-out);
}

.cosmetic-card:hover {
  border-color: var(--accent-active);
  transform: translateY(-2px);
}

.cosmetic-card:focus-visible {
  outline: 2px solid var(--accent-active);
  outline-offset: 2px;
  border-color: var(--accent-active);
}

.cosmetic-card.equipped {
  border-color: var(--accent-active);
  box-shadow: 0 0 0 1px var(--accent-ring, var(--accent-active)) inset;
}

/* 批量管理中已勾选的卡片：底色 + 边框双重强调，避免只靠小复选框判断 */
.cosmetic-card.selected {
  border-color: var(--accent-active);
  background: var(--accent-soft);
}

/* 已下架（missing）：可以看、可以删，但不能穿 —— 用灰度而不是整体降透明度，
   否则角上的 ✕ 也会一起变淡，用户会以为它不可点。
   `:not(.batch-mode)`：批量管理下点卡片是「勾选」，仍然可点，不能给禁止光标 */
.cosmetic-card.missing:not(.batch-mode) {
  cursor: not-allowed;
}

.cosmetic-card.missing:not(.batch-mode):hover {
  transform: none;
  border-color: var(--border-subtle);
}

/* 卡片内仍然可点的子元素（✕ / 详情）要拿回自己的光标 */
.cosmetic-card.missing .card-remove,
.cosmetic-card.missing .detail-link {
  cursor: pointer;
}

.cosmetic-card.missing .cosmetic-thumb {
  opacity: 0.55;
}

.cosmetic-card.missing .cosmetic-name {
  color: var(--text-secondary);
}

/* 批量勾选框：叠在缩略图左上角，带底色保证在任何图片上都看得清 */
.card-check {
  position: absolute;
  top: 6px;
  left: 6px;
  z-index: 1;
  height: 22px;
  padding: 0 4px;
  border-radius: 4px;
  background: var(--bg-elevated);
  box-shadow: var(--shadow-sm);
}

/* 单件移除按钮：常显（不靠 hover 才出现），键盘用户也能 Tab 到 */
.card-remove {
  position: absolute;
  top: 4px;
  right: 4px;
  z-index: 1;
  width: 20px;
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  font-size: 11px;
  line-height: 1;
  color: var(--text-secondary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  cursor: pointer;
  opacity: 0.8;
  transition: color 150ms var(--ease-out), border-color 150ms var(--ease-out),
    opacity 150ms var(--ease-out);
}

.card-remove:hover {
  color: var(--danger);
  border-color: var(--danger);
  opacity: 1;
}

.card-remove:focus-visible {
  outline: 2px solid var(--accent-active);
  outline-offset: 1px;
  opacity: 1;
}

.card-remove:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.cosmetic-thumb {
  height: 84px;
  border-radius: var(--radius-md);
  background: var(--bg-hover);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  margin-bottom: var(--space-2);
}

.cosmetic-thumb img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}

.thumb-fallback {
  font-family: var(--font-display);
  font-size: 28px;
  color: var(--text-secondary);
}

.cosmetic-name {
  font-size: 13px;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.cosmetic-meta {
  font-size: 11px;
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  /* 「已下架」+「已穿戴」+「无预览」可能同时出现，窄卡片里必须能换行 */
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 2px;
}

.equipped-tag {
  color: var(--text-primary);
  background: var(--accent-soft);
  padding: 0 4px;
  border-radius: 4px;
}

/* 已下架角标：用危险色，和「已穿戴」的强调色区分开 */
.missing-tag {
  color: var(--danger);
  background: var(--danger-bg);
  padding: 0 4px;
  border-radius: 4px;
  white-space: nowrap;
}

.meta-tag {
  color: var(--text-muted);
  border: 1px solid var(--border-subtle);
  padding: 0 4px;
  border-radius: 4px;
}

/* 卡片内的「详情」按钮：用原生 button 以免多一层 el-button 的盒模型开销 */
.detail-link {
  margin-left: auto;
  background: none;
  border: none;
  padding: 0 2px;
  font: inherit;
  color: var(--text-secondary);
  text-decoration: underline;
  text-decoration-color: var(--border-strong);
  text-underline-offset: 2px;
  cursor: pointer;
}

.detail-link:hover {
  color: var(--accent);
  text-decoration-color: var(--accent);
}

.detail-link:focus-visible {
  outline: 2px solid var(--accent-active);
  outline-offset: 1px;
  border-radius: 2px;
}

.wardrobe-pager {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--space-3);
}

/* 详情弹窗 */
.detail-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.detail-preview {
  height: 200px;
  border-radius: var(--radius-md);
  background: var(--bg-hover);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}

.detail-image {
  max-width: 100%;
  max-height: 200px;
  cursor: zoom-in;
}

.detail-fallback {
  font-family: var(--font-display);
  font-size: 56px;
  color: var(--text-secondary);
}

.detail-hint {
  margin: 0;
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.7;
}

.detail-meta :deep(.el-descriptions__label) {
  width: 96px;
}

.slot-row {
  border-bottom: 1px dashed var(--border-subtle);
  padding: var(--space-3) 0;
}

.slot-row:last-of-type {
  border-bottom: none;
}

.slot-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.slot-label {
  font-size: 13px;
  color: var(--text-secondary);
}

.slot-cosmetic {
  font-size: 14px;
  color: var(--text-primary);
  margin: 2px 0 var(--space-2);
}

.slot-empty {
  font-size: 12px;
  color: var(--text-secondary);
  padding: 4px 0;
}

.scale-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.scale-label {
  font-size: 11px;
  color: var(--text-secondary);
}

.scale-slider {
  flex: 1;
}

.scale-value {
  font-size: 11px;
  color: var(--text-secondary);
  width: 36px;
  text-align: right;
}

.save-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  /* B13：320px 下「提示 + 按钮」会挤成一行放不下 */
  flex-wrap: wrap;
  margin-top: var(--space-4);
  padding-top: var(--space-3);
  border-top: 1px solid var(--border-subtle);
}

.save-hint {
  font-size: 12px;
  color: var(--text-secondary);
}

/* ===== 预览大图加载态 / 失败兜底（P2-2）===== */
.detail-image {
  max-height: 100%;
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
  animation: fashion-wardrobe-spin 0.8s linear infinite;
}

@keyframes fashion-wardrobe-spin {
  to { transform: rotate(360deg); }
}

.zoom-hint {
  margin: 0;
  font-size: 11.5px;
  color: var(--text-muted);
  text-align: center;
}

.missing-fallback {
  font-family: var(--font-sans);
  font-size: 13px;
  color: var(--text-muted);
}

/* ===== 触控目标 ≥44px（P2-7）=====
   用绝对定位的伪元素把「✕ 移除」「详情」「批量勾选框」的热区扩到 44×44，
   不撑布局、不改变视觉大小；按钮本身仍保持原来的紧凑外观。 */
.card-remove,
.detail-link,
.card-check {
  touch-action: manipulation;
}

.detail-link {
  position: relative;
}

.card-remove::after,
.detail-link::after,
.card-check::after {
  content: '';
  position: absolute;
  top: 50%;
  left: 50%;
  width: 44px;
  height: 44px;
  transform: translate(-50%, -50%);
}

.cosmetic-card,
.item-thumb,
.card-remove,
.detail-link {
  touch-action: manipulation;
}

/* hover 变换只在真有 hover 的设备生效，避免触屏「粘住」 */
@media (hover: none) {
  .cosmetic-card:hover,
  .card-remove:hover {
    transform: none;
    border-color: var(--border-subtle);
  }
}

/* ===== 移动端（P2-8）=====
   原文件只有 1000px 网格断点，320px 屏上 minmax(132px,1fr) 需要 276px
   而内容宽只有 272px → 退化成超长单列；详情大图 200px 在横屏几乎占满弹窗。 */
@media (max-width: 768px) {
  .wardrobe-page {
    padding: 8px;
  }

  .page-title {
    font-size: 22px;
  }

  .wardrobe-cards {
    grid-template-columns: repeat(auto-fill, minmax(112px, 1fr));
    gap: 8px;
  }

  .detail-preview {
    height: min(180px, 32dvh);
  }
}

@media (max-height: 480px) {
  .detail-preview {
    height: 120px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .cosmetic-card { transition: none; }
  .loading-spinner { animation: none; }
}
</style>
