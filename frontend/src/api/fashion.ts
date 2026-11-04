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
  /** 时装目录（登录用户可读） */
  catalog(params?: { category?: string; pack_id?: string }) {
    return client.get<{ items: CatalogItem[] }>('/fashion/catalog', { params })
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
  /** 资产 URL（公开可读、长缓存；注意带 /api 前缀，img 标签直接可用） */
  assetUrl(assetKey: string) {
    const safe = assetKey
      .split('/')
      // 丢弃空段与 "."/".."：避免拼出会被浏览器归一化的穿越路径
      .filter((seg) => seg && seg !== '.' && seg !== '..')
      .map((seg) => encodeURIComponent(seg))
      .join('/')
    return `/api/fashion/assets/${safe}`
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
 * 时装页统一错误提示（替代到处写的 `ElMessage.error(fashionErrorMessage(...))`）。
 *
 * 与直接调 `ElMessage.error` 的区别只有一处：跳过 401 / 主动取消这两类
 * 「已有别的反馈」的错误（见 {@link isSilentFashionError}）。
 */
export function notifyFashionError(error: unknown, fallback = '操作失败'): void {
  if (isSilentFashionError(error)) return
  ElMessage.error(fashionErrorMessage(error, fallback))
}
