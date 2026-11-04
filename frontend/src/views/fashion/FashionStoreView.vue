<template>
  <div class="fashion-store-page">
    <div class="page-head">
      <div>
        <h2 class="page-title">时装商店</h2>
        <p class="page-subtitle mono">
          浏览站点全部时装 · 点「加入衣柜」收藏后才会出现在「我的衣柜」，穿戴后游戏内 mod 会同步生效
        </p>
      </div>
      <div class="head-actions">
        <span class="stat-chip mono">{{ filteredCatalog.length }} 件</span>
        <!-- 衣柜件数用「已加入收藏」的集合大小，而不是目录条数：用户在商店页
             一眼就能确认刚才的「加入衣柜」有没有生效 -->
        <span class="stat-chip mono" :title="wardrobeError ? '衣柜读取失败，点击「刷新」重试' : '已加入衣柜的时装数量'">
          我的衣柜 {{ wardrobeChipText }}
        </span>
        <el-button
          size="small"
          text
          :loading="loading || loadingWardrobe"
          :disabled="loading || loadingWardrobe"
          @click="loadAll"
        >
          刷新
        </el-button>
        <el-button size="small" type="primary" plain @click="goWardrobe">去我的衣柜</el-button>
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
          title="时装目录加载失败"
        >
          <template #default>
            <!-- 目录和衣柜是并行加载的：重试时一起重来，避免「目录好了但衣柜还是空的」 -->
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
                  v-if="previewUrls.has(item.cosmeticId) && !brokenThumbs.has(item.cosmeticId)"
                  :src="previewUrls.get(item.cosmeticId) || ''"
                  :alt="displayName(item)"
                  loading="lazy"
                  decoding="async"
                  @error="markThumbBroken(item.cosmeticId)"
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
                </div>
                <div class="item-actions">
                  <el-button size="small" text class="detail-btn" @click="openDetail(item)">详情</el-button>
                  <div class="action-right">
                    <!-- 已在衣柜：状态标签 + 去穿戴入口；未加入：主按钮「加入衣柜」 -->
                    <span v-if="isInWardrobe(item)" class="ward-tag">已在衣柜 ✓</span>
                    <el-button
                      v-if="isInWardrobe(item)"
                      size="small"
                      text
                      type="primary"
                      class="equip-btn"
                      @click="goWardrobe(item)"
                    >
                      去穿戴 →
                    </el-button>
                    <el-button
                      v-else
                      size="small"
                      text
                      type="primary"
                      class="equip-btn"
                      :loading="addingIds.has(item.cosmeticId)"
                      :disabled="addingIds.has(item.cosmeticId)"
                      @click="addToWardrobe(item)"
                    >
                      加入衣柜
                    </el-button>
                  </div>
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

    <!-- 详情弹窗：先看大图 / 元信息，再决定去衣柜穿戴 -->
    <el-dialog
      v-model="detailOpen"
      :title="detailItem ? displayName(detailItem) : '时装详情'"
      width="520px"
      class="detail-dialog"
    >
      <div v-if="detailItem" class="detail-body">
        <!-- el-image 没有 alt prop（`:alt` 只会落到根 div 上），读屏拿不到名称：
             由容器给 role=img + aria-label，替大图提供可读的替代文本 -->
        <div
          class="detail-preview"
          :role="detailUrl ? 'img' : undefined"
          :aria-label="detailUrl ? `${displayName(detailItem)} 预览图` : undefined"
        >
          <el-image
            v-if="detailUrl"
            :src="detailUrl"
            :preview-src-list="[detailUrl]"
            preview-teleported
            fit="contain"
            class="detail-image"
            :alt="displayName(detailItem)"
          />
          <span v-else class="detail-fallback">{{ (displayName(detailItem) || '?').slice(0, 1) }}</span>
        </div>
        <p v-if="!detailUrl" class="detail-hint mono">
          {{ detailItem.assetKey ? '该资产不是图片格式，网页无法预览。' : '这件时装还没有上传资产（assetKey 为空）。' }}
        </p>
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
          <el-descriptions-item label="资源大小">
            <span class="mono">{{ formatBytes(detailItem.bytes) }}</span>
          </el-descriptions-item>
        </el-descriptions>
      </div>
      <template #footer>
        <el-button @click="detailOpen = false">关闭</el-button>
        <!-- 详情弹窗与卡片同款动作：未加入 → 加入衣柜；已加入 → 状态 + 去穿戴 -->
        <template v-if="detailItem">
          <span v-if="isInWardrobe(detailItem)" class="ward-tag dialog-ward-tag">已在衣柜 ✓</span>
          <el-button
            v-if="!isInWardrobe(detailItem)"
            type="primary"
            :loading="addingIds.has(detailItem.cosmeticId)"
            :disabled="addingIds.has(detailItem.cosmeticId)"
            @click="addToWardrobe(detailItem)"
          >
            加入衣柜
          </el-button>
          <el-button v-else type="primary" @click="goWardrobe(detailItem)">去穿戴 →</el-button>
        </template>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  CATEGORY_LABELS,
  FASHION_SLOTS,
  fashionApi,
  notifyFashionError,
  type CatalogItem,
} from '@/api/fashion'

const router = useRouter()

const catalog = ref<CatalogItem[]>([])
const keyword = ref('')
const activeCategory = ref('all')
const page = ref(1)
const pageSize = 48
const loading = ref(false)
const loadError = ref(false)
const brokenThumbs = ref<Set<string>>(new Set())

const detailOpen = ref(false)
const detailItem = ref<CatalogItem | null>(null)

// ── 我的衣柜（收藏制）──
// 商店页只需要知道「哪些 cosmeticId 已经在衣柜里」，所以只存 id 集合，
// 不缓存整份 WardrobeItem（衣柜页才是它的唯一渲染方）。
const wardrobeIds = ref<Set<string>>(new Set())
/**
 * 衣柜里「全局 ID」的 localId 别名集合（`pack:local` → `local`）。
 *
 * 为什么要单独一个集合：目录里可能还有历史**简单 ID** 条目（`backboard`），
 * 而衣柜里存的是同一件时装的**全局 ID**（`example:backboard`，mod 或旧版加入的）。
 * 只做精确匹配时这类条目会一直显示「加入衣柜」，点下去才被后端告知「已经在衣柜里」。
 *
 * 不把简单 ID 直接塞进 `wardrobeIds` 的原因：页头「我的衣柜 N 件」用的是
 * `wardrobeIds.size`，混入别名会把件数算多。
 */
const wardrobeLocalIds = ref<Set<string>>(new Set())
const wardrobeError = ref(false)
const loadingWardrobe = ref(false)
// 正在「加入衣柜」的 cosmeticId：给按钮上 loading，防连点（同一件重复点只会发一次请求）
const addingIds = ref<Set<string>>(new Set())

// 目录请求竞态保护：连点刷新时只采纳最后一次响应
let catalogSeq = 0
// 衣柜请求竞态保护：同上
let wardrobeSeq = 0

/** 槽位归一化：后端历史脏数据（大小写 / 未知分类）统一落到 other */
function itemSlot(item: CatalogItem): string {
  const category = String(item.category || 'other').trim().toLowerCase()
  return FASHION_SLOTS.some((slot) => slot.value === category) ? category : 'other'
}

/** 显示名：name 为空时回落 cosmeticId，避免渲染成空白卡片 */
function displayName(item: CatalogItem): string {
  return (item.name || '').trim() || item.cosmeticId
}

function formatBytes(bytes: number): string {
  const value = Number(bytes)
  if (!Number.isFinite(value) || value <= 0) return '—'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
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
  const start = (page.value - 1) * pageSize
  return filteredCatalog.value.slice(start, start + pageSize)
})

const emptyText = computed(() => {
  const kw = keyword.value.trim()
  if (kw) return `没有匹配「${kw}」的时装`
  return activeCategory.value === 'all'
    ? '目录里还没有时装（站点管理员可在「时装管理」里导入）'
    : '该分类下暂无时装'
})

/** 页头「我的衣柜 N 件」：衣柜没读到时显示「—」，避免把失败渲染成「0 件」误导用户 */
const wardrobeChipText = computed(() =>
  wardrobeError.value ? '—' : `${wardrobeIds.value.size} 件`
)

// 预览图只接受图片扩展名；其余资产（glb/json/zip）无法在网页预览
const previewUrls = computed(() => {
  const map = new Map<string, string>()
  for (const item of catalog.value) {
    if (!item.assetKey) continue
    if (!/\.(png|jpe?g|webp|gif)$/i.test(item.assetKey)) continue
    map.set(item.cosmeticId, fashionApi.assetUrl(item.assetKey))
  }
  return map
})

const detailUrl = computed(() => {
  const item = detailItem.value
  if (!item) return ''
  return previewUrls.value.get(item.cosmeticId) || ''
})

// 搜索 / 切分类后回到第一页，否则可能停在一个空页上
watch([keyword, activeCategory], () => {
  page.value = 1
})

function markThumbBroken(cosmeticId: string): void {
  if (brokenThumbs.value.has(cosmeticId)) return
  const next = new Set(brokenThumbs.value)
  next.add(cosmeticId)
  brokenThumbs.value = next
}

function openDetail(item: CatalogItem): void {
  detailItem.value = item
  detailOpen.value = true
}

/**
 * 去衣柜穿戴。
 *
 * 带上 cosmeticId 让衣柜能定位到这一件（衣柜只把它当提示，不强制穿戴），
 * 用户仍可在衣柜里自由改选 / 调缩放。
 */
function goWardrobe(item?: CatalogItem): void {
  const query = item ? { cosmeticId: item.cosmeticId } : {}
  void router.push({ path: '/fashion/wardrobe', query })
}

/** 全局 ID 的 localId（`pack:local` → `local`；没有冒号时原样返回），与衣柜页同规则 */
function localIdOf(cosmeticId: string): string {
  const idx = cosmeticId.indexOf(':')
  return idx > 0 && idx < cosmeticId.length - 1 ? cosmeticId.slice(idx + 1) : cosmeticId
}

/**
 * 这件时装是否已在衣柜里。
 *
 * 精确匹配优先；**目录是历史简单 ID** 时再按 localId 兜底（衣柜里可能存的是
 * 同一件时装的全局 ID）。两个不同的全局 ID（不同包同名 localId）不算同一件，
 * 避免串包误标「已在衣柜」。
 */
function isInWardrobe(item: CatalogItem): boolean {
  if (wardrobeIds.value.has(item.cosmeticId)) return true
  if (item.cosmeticId.includes(':')) return false
  return wardrobeLocalIds.value.has(item.cosmeticId)
}

/**
 * 加入衣柜。
 *
 * 这里**不做乐观切换**：一旦本地先把卡片切成「已在衣柜 ✓」，加入按钮就消失了，
 * 用户既看不到 loading 也无法在失败后立刻重试（回滚会让卡片闪一下）。
 * 用按钮 loading 挡住连点、成功后翻转卡片，失败只弹错误、卡片保持原样 ——
 * 交互最稳，且后端是幂等接口，重复点也不会写坏数据。
 */
async function addToWardrobe(item: CatalogItem): Promise<void> {
  const cosmeticId = item.cosmeticId
  if (addingIds.value.has(cosmeticId)) return
  addingIds.value = new Set(addingIds.value).add(cosmeticId)
  try {
    const { data } = await fashionApi.addToWardrobe(cosmeticId)
    // 幂等：added=false 表示本来就在（例如另一个标签页刚加过）→ 同样切成「已在衣柜」
    //
    // 以**响应里的落库 ID** 为准：后端 `add_wardrobe_item` 会把历史简单 ID 就地升级成
    // 目录的规范全局 ID（`canonical_loadout_cosmetic_id`），只认自己发出去的值会出现
    // 「加了却还显示加入衣柜」（v1.3.0 审查 F2）。上报值不塞进 wardrobeIds —— 页头件数
    // 用的是它的 size，混入别名会算多；简单 ID 的匹配交给 wardrobeLocalIds。
    const storedId = (data.item && data.item.cosmeticId) || cosmeticId
    wardrobeIds.value = new Set(wardrobeIds.value).add(storedId)
    const storedLocal = localIdOf(storedId)
    if (storedLocal !== storedId) {
      wardrobeLocalIds.value = new Set(wardrobeLocalIds.value).add(storedLocal)
    }
    if (data.added) ElMessage.success(`已把「${displayName(item)}」加入衣柜`)
    else ElMessage.info(`「${displayName(item)}」已经在你的衣柜里了`)
  } catch (error) {
    notifyFashionError(error, '加入衣柜失败')
  } finally {
    const rest = new Set(addingIds.value)
    rest.delete(cosmeticId)
    addingIds.value = rest
  }
}

/**
 * 读我的衣柜（只要 id 集合）。
 *
 * 失败不阻断商店浏览：目录照常可用，只是所有卡片都会显示「加入衣柜」——
 * 此时点击也是安全的（后端幂等），所以只提示、不把整页打成错误态。
 */
async function loadWardrobe(): Promise<void> {
  const seq = ++wardrobeSeq
  loadingWardrobe.value = true
  wardrobeError.value = false
  try {
    const { data } = await fashionApi.wardrobe()
    if (seq !== wardrobeSeq) return
    // 精确 ID 与 localId 别名分开收集：前者用于「件数」，后者只用于匹配历史简单 ID
    const ids = new Set<string>()
    const localIds = new Set<string>()
    for (const item of data.items || []) {
      ids.add(item.cosmeticId)
      const local = localIdOf(item.cosmeticId)
      if (local !== item.cosmeticId) localIds.add(local)
    }
    wardrobeIds.value = ids
    wardrobeLocalIds.value = localIds
  } catch (error) {
    if (seq !== wardrobeSeq) return
    wardrobeError.value = true
    notifyFashionError(error, '读取我的衣柜失败')
  } finally {
    if (seq === wardrobeSeq) loadingWardrobe.value = false
  }
}

/** 目录 + 衣柜并行加载：挂载与「刷新」都走这里 */
async function loadAll(): Promise<void> {
  await Promise.all([loadCatalog(), loadWardrobe()])
}

async function loadCatalog(): Promise<void> {
  const seq = ++catalogSeq
  loading.value = true
  loadError.value = false
  try {
    const { data } = await fashionApi.catalog()
    if (seq !== catalogSeq) return
    catalog.value = data.items || []
    // 目录重拉成功 = 资产可能已补齐，全部缩略图重试一次
    brokenThumbs.value = new Set()
    const maxPage = Math.max(1, Math.ceil(filteredCatalog.value.length / pageSize))
    if (page.value > maxPage) page.value = maxPage
  } catch (error) {
    if (seq !== catalogSeq) return
    loadError.value = true
    notifyFashionError(error, '读取时装目录失败')
  } finally {
    if (seq === catalogSeq) loading.value = false
  }
}

onMounted(loadAll)
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
}

.store-item:hover {
  border-color: var(--accent-active);
  transform: translateY(-2px);
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

.item-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 4px;
  margin-top: auto;
  padding-top: 2px;
  /* 「已在衣柜 ✓」+「去穿戴 →」两个元素在窄卡片里必须能换行，否则会被压成省略号 */
  flex-wrap: wrap;
}

/* 卡片右侧动作组：状态标签 + 文字按钮，右对齐 */
.action-right {
  display: flex;
  align-items: center;
  gap: 2px;
  margin-left: auto;
}

/* 「已在衣柜 ✓」状态标签：用语义色而不是强调橙，和可点击的橙色动作区分开 */
.ward-tag {
  font-size: 11px;
  line-height: 18px;
  white-space: nowrap;
  color: var(--success);
  background: var(--success-bg);
  border-radius: 999px;
  padding: 0 8px;
}

.dialog-ward-tag {
  margin: 0 var(--space-2);
  vertical-align: middle;
}

/* 卡片内两个动作都走文字按钮：12 张卡各一个实心主按钮会把整页压成一片橙色，
   与主站「卡片只给一处强调」的克制风格不符 */
.detail-btn {
  color: var(--text-secondary);
}

.detail-btn:hover {
  color: var(--text-primary);
}

.equip-btn {
  padding: 0 4px;
  /* global.css 用 !important 把 el-button--primary 的文字统一压成深色，
     这里要「文字链接」观感，必须同等优先级地改回强调色 */
  color: var(--accent) !important;
}

.equip-btn:hover {
  color: var(--accent-hover) !important;
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
}

.detail-fallback {
  font-family: var(--font-display);
  font-size: 56px;
  color: var(--text-secondary);
}

.detail-hint {
  font-size: 12px;
  color: var(--text-secondary);
  margin: 0;
}

.detail-meta {
  margin-top: var(--space-2);
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
}

@media (prefers-reduced-motion: reduce) {
  .store-item { transition: none; }
  .store-item:hover { transform: none; }
}
</style>
