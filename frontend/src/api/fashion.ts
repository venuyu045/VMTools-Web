import { ElMessage } from 'element-plus'
import client from './client'
import type { AxiosProgressEvent } from 'axios'

/** 时装系统 API（mod + 网站共用后端，字段 camelCase 与后端一致） */

export type FashionCategory = 'cape' | 'wings' | 'hat' | 'back' | 'pet' | 'aura' | 'other'

export const FASHION_SLOTS: { value: FashionCategory; label: string }[] = [
  { value: 'cape', label: '披风' },
  { value: 'wings', label: '翅膀' },
  { value: 'hat', label: '头饰' },
  { value: 'back', label: '背饰' },
  { value: 'pet', label: '宠物' },
  { value: 'aura', label: '光环' },
  { value: 'other', label: '其它' },
]

export const CATEGORY_LABELS: Record<string, string> = {
  cape: '披风',
  wings: '翅膀',
  hat: '头饰',
  back: '背饰',
  pet: '宠物',
  aura: '光环',
  other: '其它',
}

export interface FashionIdentity {
  id: string
  mcUuid: string
  mcName: string
  authType: 'mojang' | 'yggdrasil' | string
  authServer: string
  verified: boolean
  verifiedAt: string | null
  userId: string | null
  createdAt: string | null
  lastSeenAt: string | null
}

export interface CatalogItem {
  cosmeticId: string
  name: string
  category: string
  assetKey: string
  sha256: string
  bytes: number
  packId: string
  sortOrder: number
  /**
   * 芒果块定价（v1.1）。**可选**：`GET /catalog` 是 mod 契约、带全站缓存，
   * 不返回这两个用户态字段；商店页改用 `GET /store/items`（见 StoreItem）。
   * 这里标可选只是为了兼容老调用点，不要在 /catalog 的数据上依赖它们。
   */
  priceMango?: number
  purchasable?: boolean
}

/**
 * 衣柜收藏条目（`GET /fashion/wardrobe`）。
 *
 * 和 `CatalogItem` 字段几乎一致，但它是**玩家收藏**而不是站点目录：
 * 只有玩家在商店里主动「加入衣柜」的时装才会出现在这里。
 *
 * 两个额外字段：
 * - `addedAt`：加入衣柜的时间（后端 naive UTC 字符串，展示走 `fmtApiTime`）；
 * - `missing`：该 cosmeticId 在站点目录里已下架 / 停用 —— 仍要显示、仍要能删，
 *   只是**不能穿戴**（提交上去也只会被后端 skip 掉）。
 */
export interface WardrobeItem {
  cosmeticId: string
  name: string
  category: string
  packId: string
  assetKey: string
  sha256: string
  bytes: number
  sortOrder: number
  addedAt: string | null
  missing: boolean
}

/** 「加入衣柜」结果：`added=false` 表示本来就在衣柜里（幂等重复点击，不算失败） */
export interface WardrobeAddResult {
  ok: boolean
  added: boolean
  item: WardrobeItem
}

export interface LoadoutItem {
  slot: string
  cosmeticId: string
  scale: number
  assetKey: string
  sha256: string
  bytes: number
  name: string
  category: string
  missing: boolean
  /** 包级下发（mod 契约 §3）：这件时装属于哪个包 / 包的当前版本；无包时为空串 */
  packId: string
  packVersion: string
}

export interface DeviceApproveResult {
  ok: boolean
  identityId: string
  mcUuid: string
  mcName: string
}

/** 保存穿戴时被后端跳过的条目（未知 / 已下架的时装，不会再让整条请求失败） */
export interface LoadoutWarning {
  slot: string
  cosmeticId: string
  code: string
}

export interface LoadoutPutResult {
  ok: boolean
  updatedAt: string
  warnings?: LoadoutWarning[]
}

// ── 管理端类型（site_admin；契约见 docs/fashion-api.md §10）─────────────

/**
 * 管理端目录条目：比公开 `CatalogItem` 多 `packId` / `enabled`，
 * 且 `GET /admin/catalog` 会把**停用**条目也返回（公开目录不会）。
 */
export interface AdminCatalogItem {
  cosmeticId: string
  packId: string
  name: string
  category: string
  assetKey: string
  sha256: string
  bytes: number
  enabled: boolean
  sortOrder: number
  /** 价格（芒果块），0 = 免费；范围 0..1_000_000（v1.1） */
  priceMango: number
  /** false = 商店不售卖（仅管理端发放），仍可穿戴（v1.1） */
  purchasable: boolean
}

/**
 * 新建 / 更新目录条目。`PUT` 是部分更新：只传要改的字段即可
 * （后端 `exclude_unset`，没传的字段保持原值）。
 */
export interface CatalogUpsertPayload {
  cosmeticId?: string
  packId?: string
  name?: string
  category?: string
  assetKey?: string
  sha256?: string
  bytes?: number | null
  enabled?: boolean
  sortOrder?: number
  /** 芒果块定价：`POST/PUT /admin/catalog` 支持部分更新，只传这一个也能改价（v1.1） */
  priceMango?: number
  /** 是否可购买（商店是否售卖）；不传 = 不改（v1.1） */
  purchasable?: boolean
}

/** 已上传资产（`GET /admin/assets`） */
export interface FashionAsset {
  id: string
  packId: string
  version: string
  filename: string
  sha256: string
  bytes: number
  /** 资产存储路径：可直接当 catalog 的 `assetKey` 用（后端两段解析都认） */
  storagePath: string
  createdAt: string | null
}

/**
 * 时装包清单项（`GET /packs`，公开读）。
 *
 * ⚠️ `publishedAt` 是**毫秒时间戳 number**（mod 的 `PackCatalog` 用 `getAsLong()`
 * 读，给 ISO 字符串会静默变 0）—— 前端格式化时走 `parseApiDate`/`fmtApiTime`。
 */
export interface FashionPack {
  id: string
  version: string
  sha256: string
  bytes: number
  /** 相对 `/api/fashion` 的下载路径（不是 `/api/fashion/packs/...` 绝对地址） */
  url: string
  publishedAt: number | null
  /** 未配置签名私钥时后端不输出这两个字段 */
  signature?: string | null
  signingKeyId?: string | null
}

export interface AssetUploadResult {
  ok: boolean
  asset: FashionAsset
  /** 带 cosmeticId 上传且命中目录时回填的条目 ID（否则空串） */
  cosmeticId: string
}

export interface PackUploadResult {
  ok: boolean
  pack: FashionPack
}

// ── 芒果块钱包 / 代币购买（v1.1；契约见 docs/fashion-mango-wallet-api.md）──────
// 所有字段 camelCase，与后端 schemas/fashion.py 的 alias 一致。货币统一叫「芒果块」。

/** 我的钱包（GET /wallet、GET /store/items 内联返回） */
export interface WalletOut {
  balance: number
  currency: string
  totalEarned: number
  totalSpent: number
  updatedAt: string | null
}

/** 一条收支流水（GET /wallet/transactions）；delta 正入负出，按 createdAt 倒序 */
export interface WalletTransactionItem {
  id: string
  /** purchase | refund | admin_adjust | recharge | grant */
  type: string
  delta: number
  balanceAfter: number
  amountAbs: number
  cosmeticId: string
  cosmeticName: string
  note: string
  createdAt: string | null
}

export interface WalletTransactionListResponse {
  items: WalletTransactionItem[]
  total: number
  limit: number
  offset: number
}

/** 充值渠道（本期恒为空数组；接入支付后由后端填充） */
export interface RechargeChannel {
  id: string
  name: string
  description: string
  enabled: boolean
}

/** GET /wallet/recharge/options：充值入口全部展示信息（本期 available=false） */
export interface RechargeOptionsResponse {
  available: boolean
  currency: string
  channels: RechargeChannel[]
  notice: string
}

/** POST /wallet/recharge 请求（本期固定返回 501，形状先定死） */
export interface RechargePayload {
  amount: number
  channel?: string
  clientRequestId?: string
}

/** 充值成功响应（本期不会返回，供后续接入支付时复用） */
export interface RechargeResponse {
  ok: boolean
  balance: number
  transactionId: string
  orderNo: string
}

/** 商店条目：目录元数据 + 定价 + 当前用户的拥有/衣柜状态（GET /store/items） */
export interface StoreItem {
  cosmeticId: string
  name: string
  category: string
  assetKey: string
  sha256: string
  bytes: number
  packId: string
  sortOrder: number
  /** 价格（芒果块），0 = 免费 */
  priceMango: number
  /** false = 商店不售卖（仅管理端发放），仍可穿戴 */
  purchasable: boolean
  owned: boolean
  inWardrobe: boolean
}

/** GET /store/items 聚合响应：一次拿全余额 / 价格 / 拥有 / 衣柜状态 / 充值开关 */
export interface StoreItemsResponse {
  currency: string
  wallet: WalletOut
  rechargeAvailable: boolean
  items: StoreItem[]
}

/** POST /store/purchase 请求 */
export interface PurchasePayload {
  cosmeticId: string
  /** 可选幂等键（<=64 字符）：网络重试 / 双击不会重复扣款 */
  clientRequestId?: string
}

/** POST /store/purchase 响应（幂等：alreadyOwned=true 表示之前已拥有，不扣款） */
export interface PurchaseResponse {
  ok: boolean
  purchased: boolean
  alreadyOwned: boolean
  free: boolean
  priceMango: number
  balance: number
  cosmeticId: string
  item: StoreItem | null
  transactionId: string
}

/** 购买/获得记录条目（GET /store/orders，所有权维度，删衣柜不会消失） */
export interface PurchaseOrderItem {
  orderNo: string
  cosmeticId: string
  name: string
  category: string
  packId: string
  pricePaid: number
  /** purchase | free_claim | legacy_wardrobe | admin_grant | migration */
  source: string
  note: string
  acquiredAt: string | null
  /** 目录已删除 / 已停用 */
  missing: boolean
}

export interface PurchaseOrderListResponse {
  items: PurchaseOrderItem[]
  total: number
  limit: number
  offset: number
}

/** 分页参数（limit 1..100，默认 20；offset >= 0） */
export interface FashionListParams {
  limit?: number
  offset?: number
}

/** POST /admin/wallet/adjust 请求：delta > 0 发放、< 0 扣回，不能为 0 */
export interface AdminWalletAdjustPayload {
  userId: string
  delta: number
  note?: string
}

export interface AdminWalletAdjustResponse {
  ok: boolean
  userId: string
  balance: number
  delta: number
  transactionId: string
}

/** 管理端用户钱包条目（GET /admin/wallet/users） */
export interface AdminWalletUser {
  userId: string
  gameId: string
  displayName: string
  role: string
  balance: number
  totalEarned: number
  totalSpent: number
}

export interface AdminWalletUserListResponse {
  items: AdminWalletUser[]
  total: number
  limit: number
  offset: number
}

export interface AdminWalletUsersParams extends FashionListParams {
  keyword?: string
}

/** POST /admin/wallet/grant-cosmetic：直接发放一件时装（不扣币，自动加入衣柜） */
export interface AdminGrantCosmeticPayload {
  userId: string
  cosmeticId: string
  note?: string
}

export interface AdminGrantCosmeticResponse {
  ok: boolean
  userId: string
  cosmeticId: string
  /** false = 该用户之前已拥有（幂等，不重复写入） */
  granted: boolean
  item: StoreItem | null
}

export type UploadProgressHandler = (e: { loaded: number; total: number }) => void

/**
 * 上传类请求必须**逐请求覆盖 timeout**（A2 坑）。
 *
 * 为什么：`client` 全局 `timeout: 15000`（api/client.ts:7），而 64MB 的资产/包在
 * 普通家宽上行要 13~60 秒 → 全局超时会在传输中途 abort（axios `ECONNABORTED`），
 * 用户只看到「上传失败」。这里沿用理想乡上传的既有写法
 * （api/lixiangxiang.ts:110-125）放宽到 120s，并把进度回调透出去。
 */
const UPLOAD_TIMEOUT_MS = 120_000

/**
 * @param signal 取消句柄：管理端「取消上传」与「关闭上传弹窗」都靠它中止请求。
 *   没有它时 64MB 的包在慢速上行下要挂到 120s 超时才结束，用户关掉弹窗后
 *   请求仍在后台跑（进度/错误回调会串到下一次上传）。
 */
function uploadConfig(
  file: File,
  onProgress?: UploadProgressHandler,
  signal?: AbortSignal
) {
  return {
    timeout: UPLOAD_TIMEOUT_MS,
    ...(signal ? { signal } : {}),
    onUploadProgress: (e: AxiosProgressEvent) => {
      // e.total 在个别环境可能为 undefined/0 → 用 file.size 兜底，保证进度条始终刷新
      if (onProgress) onProgress({ loaded: e.loaded, total: e.total || file.size })
    },
  }
}

export const fashionApi = {
  /** 时装目录（登录用户可读；mod 契约，带缓存，**不含**用户态价格/拥有字段） */
  catalog(params?: { category?: string; pack_id?: string }) {
    return client.get<{ items: CatalogItem[] }>('/fashion/catalog', { params })
  },

  // ── 芒果块钱包 / 商店购买（v1.1；契约见 docs/fashion-mango-wallet-api.md §3）──

  /**
   * 商店页聚合数据：目录 + 定价 + 拥有/衣柜状态 + 钱包余额 + 充值开关，**一次拿全**。
   * 与 `catalog()` 的区别：它按当前登录用户计算 owned/inWardrobe，不能复用全站缓存。
   */
  storeItems(params?: { category?: string; pack_id?: string }) {
    return client.get<StoreItemsResponse>('/fashion/store/items', { params })
  },
  /** 用芒果块购买 / 免费领取一件时装（成功自动加入衣柜；幂等） */
  purchase(payload: PurchasePayload) {
    return client.post<PurchaseResponse>('/fashion/store/purchase', payload)
  },
  /** 我的购买/获得记录（所有权维度，删衣柜不会消失） */
  orders(params?: FashionListParams) {
    return client.get<PurchaseOrderListResponse>('/fashion/store/orders', { params })
  },
  /** 我的钱包（首次访问自动创建余额 0 的钱包） */
  wallet() {
    return client.get<WalletOut>('/fashion/wallet')
  },
  /** 芒果块收支明细（按时间倒序，分页） */
  walletTransactions(params?: FashionListParams) {
    return client.get<WalletTransactionListResponse>('/fashion/wallet/transactions', { params })
  },
  /** 充值入口信息（本期恒 available=false、channels=[]） */
  rechargeOptions() {
    return client.get<RechargeOptionsResponse>('/fashion/wallet/recharge/options')
  },
  /**
   * 充值占位：本期后端固定 501 `recharge_not_available`，接口形状先定死。
   * 调用方必须**友好处理 501**（ElMessage.info + 弹窗内说明），不要当崩溃弹红条。
   */
  recharge(payload: RechargePayload) {
    return client.post<RechargeResponse>('/fashion/wallet/recharge', payload)
  },

  // ── 我的衣柜（收藏制：只有主动「加入衣柜」的时装才在这里）───────────────
  /**
   * 我的衣柜。
   *
   * 注意它**不是**目录的子集查询，而是玩家收藏：商店页拿它算「已在衣柜」集合，
   * 衣柜页拿它当唯一数据源（不再展示全站目录）。
   */
  wardrobe() {
    return client.get<{ items: WardrobeItem[] }>('/fashion/wardrobe')
  },
  /** 加入衣柜（幂等：已在衣柜时后端返回 `added: false`，不报错） */
  addToWardrobe(cosmeticId: string) {
    return client.post<WardrobeAddResult>('/fashion/wardrobe', { cosmeticId })
  },
  /** 从衣柜移除单件（`removed: false` = 本来就不在衣柜里） */
  removeFromWardrobe(cosmeticId: string) {
    return client.delete<{ ok: boolean; removed: boolean }>(
      `/fashion/wardrobe/${encodeURIComponent(cosmeticId)}`
    )
  },
  /**
   * 批量从衣柜移除。
   *
   * 后端对空数组 / 超量分别返回 400 `empty_selection` / `too_many_items`，
   * 调用方用 `fashionErrorMessage()` 取中文提示即可。
   */
  batchRemoveFromWardrobe(cosmeticIds: string[]) {
    return client.post<{ ok: boolean; removed: number }>('/fashion/wardrobe/batch-delete', {
      cosmeticIds,
    })
  },
  /** 当前网页账号绑定的 MC 身份 */
  myIdentities() {
    return client.get<{ items: FashionIdentity[] }>('/fashion/identities/me')
  },
  /** 解绑自己的 MC 身份 */
  unbind(identityId: string) {
    return client.delete(`/fashion/identities/${encodeURIComponent(identityId)}`)
  },
  /** 输入 6 位绑定码（mod 端生成）完成绑定 */
  approveDevice(userCode: string) {
    return client.post<DeviceApproveResult>('/fashion/device/approve', { userCode })
  },
  /** 读取穿戴配置；identityId 用于一个账号绑了多个 MC 身份时指定 */
  getLoadout(identityId?: string) {
    return client.get<{ items: LoadoutItem[] }>('/fashion/loadout/me', {
      params: identityId ? { identityId } : {},
    })
  },
  /** 全量覆盖穿戴配置（后端会跳过未知/已下架的条目，并在 warnings 里列出） */
  putLoadout(items: { slot: string; cosmeticId: string; scale: number }[], identityId?: string) {
    return client.put<LoadoutPutResult>(
      '/fashion/loadout',
      { items },
      { params: identityId ? { identityId } : {} }
    )
  },
  /**
   * 资产 URL（公开可读、带 immutable 长缓存；带 /api 前缀，img 标签直接可用）。
   *
   * P2-3：后端对资产回 `Cache-Control: public,max-age=31536000,immutable`，
   * 只用 assetKey 拼 URL 时管理员替换资产（sha256 变了）用户一年都看到旧图。
   * 追加 `?v=${sha256.slice(0,12)}` 作版本戳即可破缓存；后端 `{asset_key:path}`
   * 路由按路径取文件、忽略 query，安全。**第二参数可选**：老调用点不传也能用，
   * 但缩略图/大图/viewer 应尽量把当前条目的 sha256 传进来。
   *
   * P3 防御：
   * - 空 assetKey（或全是 "."/".."）返回 ''，调用方据此不渲染 img（避免必然 404）；
   * - 反斜杠归一为 '/'，避免 Windows 路径拼出畸形 URL；
   * - 含字面 '%' 的段先 decodeURIComponent 再重新编码，避免 `%20 → %2520` 二次编码。
   */
  assetUrl(assetKey: string, sha256?: string): string {
    if (!assetKey) return ''
    const safe = assetKey
      .replace(/\\/g, '/')
      .split('/')
      // 丢弃空段与 "."/".."：避免拼出会被浏览器归一化的穿越路径
      .filter((seg) => seg && seg !== '.' && seg !== '..')
      .map((seg) => {
        let raw = seg
        // 段里已经有百分号转义（如历史 assetKey "icon%20a.png"）时先解码，
        // 否则 encodeURIComponent 会把 '%' 再编一次变成 '%25'
        if (raw.includes('%')) {
          try {
            raw = decodeURIComponent(raw)
          } catch {
            // 非法百分号序列（如 "100%.png"）：保持原样，下面统一编码成 %25
          }
        }
        return encodeURIComponent(raw)
      })
      .join('/')
    if (!safe) return ''
    const url = `/api/fashion/assets/${safe}`
    // sha256 通常已是十六进制；仍走 encodeURIComponent 防注入
    const version = (sha256 || '').trim().slice(0, 12)
    return version ? `${url}?v=${encodeURIComponent(version)}` : url
  },

  // ── 管理端（仅 site_admin；后端是 require_site_admin，admin 调会 403）──

  /** 全部目录条目（含停用） */
  adminCatalog(params?: { category?: string; pack_id?: string }) {
    return client.get<{ items: AdminCatalogItem[] }>('/fashion/admin/catalog', { params })
  },
  /** 新建目录条目 */
  adminCreateCatalog(payload: CatalogUpsertPayload) {
    return client.post<AdminCatalogItem>('/fashion/admin/catalog', payload)
  },
  /** 更新目录条目（部分字段） */
  adminUpdateCatalog(cosmeticId: string, payload: CatalogUpsertPayload) {
    return client.put<AdminCatalogItem>(
      `/fashion/admin/catalog/${encodeURIComponent(cosmeticId)}`,
      payload
    )
  },
  /** 删除目录条目（已穿戴的玩家会在衣柜里看到「已下架」） */
  adminDeleteCatalog(cosmeticId: string) {
    return client.delete<{ ok: boolean }>(
      `/fashion/admin/catalog/${encodeURIComponent(cosmeticId)}`
    )
  },

  // ── 管理端：芒果块钱包（site_admin；v1.1）──────────────────────────────
  /** 发放 / 扣回芒果块（充值开放前唯一入金途径；delta 不能为 0） */
  adminAdjustWallet(payload: AdminWalletAdjustPayload) {
    return client.post<AdminWalletAdjustResponse>('/fashion/admin/wallet/adjust', payload)
  },
  /** 用户钱包列表（按 gameId / displayName 模糊搜索） */
  adminWalletUsers(params?: AdminWalletUsersParams) {
    return client.get<AdminWalletUserListResponse>('/fashion/admin/wallet/users', { params })
  },
  /** 直接给用户发放一件时装（不扣币，自动加入衣柜；补发 / 活动赠送 / 联调） */
  adminGrantCosmetic(payload: AdminGrantCosmeticPayload) {
    return client.post<AdminGrantCosmeticResponse>(
      '/fashion/admin/wallet/grant-cosmetic',
      payload
    )
  },

  /** 资产列表（可按 packId 过滤） */
  adminAssets(params?: { pack_id?: string }) {
    return client.get<{ items: FashionAsset[] }>('/fashion/admin/assets', { params })
  },
  /**
   * 上传资产（multipart）。
   *
   * 带 `cosmeticId` 时后端会自动回填该目录条目的 sha256/bytes（并补 assetKey），
   * 这是「先建目录条目 → 再传图」的推荐顺序；不带则只存文件。
   */
  async adminUploadAsset(
    input: { file: File; packId?: string; version?: string; cosmeticId?: string },
    onProgress?: UploadProgressHandler,
    signal?: AbortSignal
  ): Promise<AssetUploadResult> {
    const form = new FormData()
    form.append('file', input.file)
    form.append('pack_id', input.packId || 'default')
    form.append('version', input.version || '1')
    form.append('cosmetic_id', input.cosmeticId || '')
    // 不手动设置 Content-Type：让浏览器生成带 boundary 的 multipart 头
    const { data } = await client.post<AssetUploadResult>(
      '/fashion/admin/assets',
      form,
      uploadConfig(input.file, onProgress, signal)
    )
    return data
  },
  /** 发布时装包（.zip；后端按契约 §6.3 静态校验后立即出现在 GET /packs） */
  async adminUploadPack(
    input: { file: File; packId: string; version: string },
    onProgress?: UploadProgressHandler,
    signal?: AbortSignal
  ): Promise<PackUploadResult> {
    const form = new FormData()
    form.append('file', input.file)
    form.append('pack_id', input.packId)
    form.append('version', input.version)
    const { data } = await client.post<PackUploadResult>(
      '/fashion/admin/packs',
      form,
      uploadConfig(input.file, onProgress, signal)
    )
    return data
  },
  /** 下架时装包（目录仍在引用其 sha256 时后端返回 409 `pack_in_use`） */
  adminDeletePack(packId: string, version: string) {
    return client.delete<{ ok: boolean }>(
      `/fashion/admin/packs/${encodeURIComponent(packId)}/${encodeURIComponent(version)}`
    )
  },
  /** 时装包清单（公开读；mod 与本页共用同一份契约） */
  packs() {
    return client.get<{ packs: FashionPack[] }>('/fashion/packs')
  },
}

/** 从 axios 错误里抽出后端结构化错误码（detail 可能是对象） */
export function fashionErrorCode(error: unknown): string {
  const err = error as { response?: { data?: { detail?: unknown } } }
  const detail = err?.response?.data?.detail
  if (detail && typeof detail === 'object' && 'code' in detail) {
    return String((detail as { code: unknown }).code)
  }
  return ''
}

/**
 * 后端错误的中文提示。
 *
 * 优先级：结构化 `detail.message`（后端所有业务错误都是中文）→ 字符串 `detail`
 * （400 业务错误是中文）→ **按 HTTP 状态码兜底中文** → 调用方给的 fallback。
 *
 * 为什么最后要按状态码兜底：鉴权类错误（`api/deps.py:get_current_user`）的
 * `detail` 是**英文字符串**（"Invalid or expired token" / "User not approved" /
 * require_site_admin 的 "Only site admin can access this resource"），
 * 直接原样弹给用户就是一句英文；401 还会和 client.ts 拦截器的「登录已过期」
 * 提示重复出现。四个时装页面都走这个函数，所以在这里统一兜底。
 */
export function fashionErrorMessage(error: unknown, fallback = '操作失败'): string {
  const err = error as {
    response?: { data?: { detail?: unknown }; status?: number }
  }
  const detail = err?.response?.data?.detail
  const status = typeof err?.response?.status === 'number' ? err.response.status : 0
  // 401：登录态失效由拦截器统一提示 + 跳登录，这里不再把英文串弹出来
  if (status === 401) return '登录已过期，请重新登录'
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const message = (detail as { message: unknown }).message
    if (typeof message === 'string' && message.trim()) return message.trim()
  }
  if (typeof detail === 'string' && detail.trim()) {
    // 403 的字符串 detail 是英文（require_site_admin / User not approved）→ 中文兜底；
    // 业务错误（fashion_service 的 4xx）是结构化对象，不会走到这里
    if (status === 403) return '没有权限执行该操作（403）'
    return detail.trim()
  }
  if (status === 403) return '没有权限执行该操作（403）'
  if (status === 413) return '文件超过 64MB 上限（nginx 直接拒绝），请压缩或拆分后再传'
  if (status === 422) return '提交的数据格式有误，请检查后重试（422）'
  if (status === 429) {
    const retry = fashionRetryAfter(error)
    return retry ? `操作太频繁，请等待 ${retry} 秒后再试` : '操作太频繁，请稍后再试'
  }
  // 5xx：反向代理 / nginx 的错误页是 HTML，拿不到 detail → 给中文兜底，
  // 否则用户只会看到调用方的通用 fallback（如「保存失败」），无从判断是服务端问题
  if (status >= 500) return '服务器内部错误，请稍后重试（5xx）'
  return fallback
}

/**
 * 后端错误的 HTTP 状态码（拿不到返回 0）。
 *
 * 需要它的两个场景（审计 A2/A3）：
 * - 403：`require_site_admin` 的 `detail` 是**英文字符串**
 *   `"Only site admin can access this resource"`，`fashionErrorMessage` 会原样返回；
 * - 413：nginx 的 `client_max_body_size` 会直接返回 **HTML**，拿不到 `detail`。
 */
export function fashionErrorStatus(error: unknown): number {
  const err = error as { response?: { status?: number } }
  return typeof err?.response?.status === 'number' ? err.response.status : 0
}

/** 429 响应里的 `Retry-After`（秒）；没有该头返回 null */
export function fashionRetryAfter(error: unknown): number | null {
  const err = error as { response?: { headers?: Record<string, unknown> } }
  const headers = err?.response?.headers
  if (!headers) return null
  const raw = headers['retry-after'] ?? headers['Retry-After']
  const value = Number(raw)
  return Number.isFinite(value) && value > 0 ? Math.ceil(value) : null
}

/**
 * 请求是否被**调用方主动取消**（axios `CanceledError`）。
 *
 * 上传弹窗的「取消上传」/「关闭弹窗」都会 abort 请求，此时不该把
 * 「请求已取消」当成失败弹红条 —— 那是用户自己的操作。
 */
export function fashionErrorIsCanceled(error: unknown): boolean {
  const err = error as { code?: string; name?: string }
  return err?.code === 'ERR_CANCELED' || err?.name === 'CanceledError'
}

/**
 * 这类错误**不需要**再弹第二条提示。
 *
 * - 401：`api/client.ts` 的响应拦截器已经统一「清 token + 提示登录已过期 + 跳登录」，
 *   页面再弹一次就是同一句话叠两条（一个页面并发三个请求时更是一次弹四条）。
 *   拦截器会在错误对象上打 `authErrorHandled` 标记 —— 只有它**真的**处理过
 *   才静默；没有 token 的 401（拦截器按设计静默）仍由页面提示，避免页面一片空白
 *   却什么反馈都没有；
 * - `ERR_CANCELED`：用户主动取消上传，不是错误。
 */
export function isSilentFashionError(error: unknown): boolean {
  if (fashionErrorIsCanceled(error)) return true
  const handled = (error as { authErrorHandled?: boolean })?.authErrorHandled
  return handled === true
}

/**
 * 402 余额不足：购买失败时用它给出「去充值 / 联系管理员发放」的引导。
 * 后端 `insufficient_mango` 的 message 里带还差多少，`notifyFashionError` 会原样展示。
 */
export function fashionErrorIsInsufficientMango(error: unknown): boolean {
  return fashionErrorCode(error) === 'insufficient_mango' || fashionErrorStatus(error) === 402
}

/**
 * 「充值通道未开放」的 501（预留接口本期固定返回）：
 * 这是**预期内**的占位响应，不是服务端崩溃，调用方应弹 info 级友好提示，
 * 不要走 `notifyFashionError` 弹红条、也不要落到「服务器内部错误（5xx）」兜底。
 */
export function fashionErrorIsRechargeUnavailable(error: unknown): boolean {
  return (
    fashionErrorCode(error) === 'recharge_not_available' ||
    fashionErrorStatus(error) === 501
  )
}

/**
 * 时装页统一错误提示（替代到处写的 `ElMessage.error(fashionErrorMessage(...))`）。
 *
 * 与直接调 `ElMessage.error` 的区别只有一处：跳过 401 / 主动取消这两类
 * 「已有别的反馈」的错误（见 {@link isSilentFashionError}）。
 *
 * 说明：402 余额不足 / 403 purchase_required / 501 充值未开放 都走
 * `fashionErrorMessage`，它会优先取后端结构化的中文 `detail.message` 展示；
 * 501 由调用方用 {@link fashionErrorIsRechargeUnavailable} 单独做友好提示。
 */
export function notifyFashionError(error: unknown, fallback = '操作失败'): void {
  if (isSilentFashionError(error)) return
  ElMessage.error(fashionErrorMessage(error, fallback))
}
