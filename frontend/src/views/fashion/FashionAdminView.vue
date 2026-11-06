<template>
  <div class="fashion-admin-page">
    <div class="page-head">
      <div>
        <h2 class="page-title">时装管理</h2>
        <p class="page-subtitle mono">
          目录 / 资产 / 时装包 · 仅站点管理员可操作（后端 require_site_admin）
        </p>
      </div>
      <div class="head-actions">
        <el-button size="small" :loading="refreshing" :disabled="denied" @click="refreshActive">
          刷新
        </el-button>
      </div>
    </div>

    <!--
      403 兜底：路由守卫在 authStore.user 未加载时会放行（router/index.ts:124 的
      `authStore.user &&`），所以直接刷新 /fashion/admin 会先渲染页面再吃 403。
      这里在渲染前就拦一道，并把后端那句英文 detail 换成中文说明（审计 A3）。
    -->
    <el-alert
      v-if="denied"
      class="forbidden-alert"
      type="error"
      show-icon
      :closable="false"
      title="仅站点管理员可操作"
    >
      <template #default>
        当前账号{{ authStore.user ? `（${roleLabel(authStore.user.role)}）` : '' }}没有时装管理权限。
        请用站点管理员账号登录，或
        <router-link class="link" to="/dashboard">返回仪表盘</router-link>。
      </template>
    </el-alert>

    <el-card v-else shadow="never" class="admin-card">
      <el-tabs v-model="activeTab" class="admin-tabs">
        <!-- ============ 1. 目录管理 ============ -->
        <el-tab-pane label="目录管理" name="catalog">
          <div class="pane">
            <div class="pane-toolbar">
              <div class="toolbar-left">
                <el-input
                  v-model="catalogKeyword"
                  size="small"
                  clearable
                  placeholder="搜索 cosmeticId / 名称"
                  aria-label="搜索时装"
                  class="search-input"
                />
                <el-select
                  v-model="catalogCategory"
                  size="small"
                  clearable
                  placeholder="全部槽位"
                  aria-label="按槽位筛选"
                  class="filter-select"
                >
                  <el-option
                    v-for="slot in FASHION_SLOTS"
                    :key="slot.value"
                    :label="slot.label"
                    :value="slot.value"
                  />
                </el-select>
                <el-select
                  v-model="catalogPack"
                  size="small"
                  clearable
                  filterable
                  placeholder="全部时装包"
                  aria-label="按时装包筛选"
                  class="filter-select"
                >
                  <el-option v-for="pack in packOptions" :key="pack" :label="pack" :value="pack" />
                </el-select>
              </div>
              <div class="toolbar-right">
                <span class="count-hint mono">
                  {{ filteredCatalogRows.length }} / {{ catalogRows.length }} 条
                </span>
                <el-button size="small" type="primary" @click="openCatalogDialog(null)">
                  新建时装
                </el-button>
              </div>
            </div>

            <el-table
              v-loading="catalogLoading"
              :data="pagedCatalogRows"
              size="small"
              stripe
              class="admin-table"
              empty-text="目录里还没有时装"
              :header-cell-style="headerCellStyle"
            >
              <el-table-column label="预览" width="72">
                <template #default="{ row }">
                  <div class="thumb-cell">
                    <!--
                      P2-4：停用条目（!row.enabled）的资产必然 404，不渲染 img；
                      P2-1：破图按 URL 记录；P1-1：viewer 可点遮罩关闭 + 返回键/滚动锁。
                    -->
                    <el-image
                      v-if="row.enabled && catalogThumbUrl(row) && !brokenThumbUrls.has(catalogThumbUrl(row))"
                      :src="catalogThumbUrl(row)"
                      :preview-src-list="catalogPreviewList(row)"
                      preview-teleported
                      hide-on-click-modal
                      fit="contain"
                      loading="lazy"
                      fetchpriority="low"
                      class="cell-thumb"
                      :alt="displayName(row)"
                      @show="onViewerShow"
                      @close="onViewerClose"
                      @error="markThumbBroken"
                    />
                    <span v-else-if="!row.enabled" class="thumb-fallback off-fallback">停用</span>
                    <span v-else class="thumb-fallback">{{ (displayName(row) || '?').slice(0, 1) }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="cosmeticId" min-width="170">
                <template #default="{ row }">
                  <span class="mono">{{ row.cosmeticId }}</span>
                </template>
              </el-table-column>
              <el-table-column label="名称" min-width="120">
                <template #default="{ row }">
                  <span v-if="row.name">{{ row.name }}</span>
                  <span v-else class="muted">未命名（衣柜显示 ID）</span>
                </template>
              </el-table-column>
              <el-table-column label="槽位" width="86">
                <template #default="{ row }">
                  {{ CATEGORY_LABELS[row.category] || row.category || '—' }}
                </template>
              </el-table-column>
              <el-table-column label="时装包" width="120">
                <template #default="{ row }">
                  <span class="mono">{{ row.packId || '—' }}</span>
                </template>
              </el-table-column>
              <!-- 任务 A3：目录定价 + 是否可购买 -->
              <el-table-column label="价格（芒果块）" width="128">
                <template #default="{ row }">
                  <span class="mono" :class="Number(row.priceMango) > 0 ? 'price-paid' : 'price-free'">
                    {{ Number(row.priceMango) > 0 ? '🥭 ' + row.priceMango : '免费' }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column label="可购买" width="86">
                <template #default="{ row }">
                  <el-tag :type="row.purchasable === false ? 'info' : 'success'" size="small" effect="plain">
                    {{ row.purchasable === false ? '否' : '是' }}
                  </el-tag>
                </template>
              </el-table-column>
              <!-- A5：资产缺失必须在表格里直接可见，否则「目录有、衣柜破图」无从发现 -->
              <el-table-column label="资产" width="96">
                <template #default="{ row }">
                  <el-tag v-if="!row.assetKey" size="small" type="danger" effect="plain">缺资产</el-tag>
                  <el-tag v-else-if="catalogThumbUrl(row) && brokenThumbUrls.has(catalogThumbUrl(row))" size="small" type="danger" effect="plain">
                    文件丢失
                  </el-tag>
                  <el-tag v-else-if="!isImageKey(row.assetKey)" size="small" type="warning" effect="plain">
                    非图片
                  </el-tag>
                  <el-tag v-else size="small" type="success" effect="plain">正常</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="排序" width="70">
                <template #default="{ row }">
                  <span class="mono">{{ row.sortOrder }}</span>
                </template>
              </el-table-column>
              <el-table-column label="启用" width="72">
                <template #default="{ row }">
                  <el-switch
                    :model-value="row.enabled"
                    size="small"
                    :loading="togglingId === row.cosmeticId"
                    :aria-label="`${row.enabled ? '下架' : '上架'} ${row.cosmeticId}`"
                    @change="(value: string | number | boolean) => toggleEnabled(row, value)"
                  />
                </template>
              </el-table-column>
              <el-table-column label="操作" width="132" fixed="right">
                <template #default="{ row }">
                  <el-button size="small" text type="primary" @click="openCatalogDialog(row)">
                    编辑
                  </el-button>
                  <el-button size="small" text type="danger" @click="removeCatalog(row)">
                    删除
                  </el-button>
                </template>
              </el-table-column>
            </el-table>

            <div class="pane-footer">
              <el-pagination
                layout="prev, pager, next"
                :total="filteredCatalogRows.length"
                :page-size="pageSize"
                :current-page="catalogPage"
                @current-change="catalogPage = $event"
              />
            </div>
          </div>
        </el-tab-pane>

        <!-- ============ 2. 资产管理 ============ -->
        <el-tab-pane label="资产管理" name="assets">
          <div class="pane">
            <div class="pane-toolbar">
              <div class="toolbar-left">
                <el-input
                  v-model="assetPackFilter"
                  size="small"
                  clearable
                  placeholder="按 packId 过滤"
                  aria-label="按 packId 过滤资产"
                  class="search-input"
                  @change="onAssetFilterChange"
                />
              </div>
              <div class="toolbar-right">
                <span class="count-hint mono">{{ assets.length }} 个资产（按 50 条/页）</span>
                <el-button size="small" type="primary" @click="openAssetDialog">上传资产</el-button>
              </div>
            </div>

            <el-table
              v-loading="assetLoading"
              :data="pagedAssets"
              size="small"
              stripe
              class="admin-table"
              empty-text="还没有上传过资产"
              :header-cell-style="headerCellStyle"
            >
              <el-table-column label="预览" width="72">
                <template #default="{ row }">
                  <div class="thumb-cell">
                    <!--
                      P2-3：URL 带 sha256 版本戳；P1-1：viewer 可点遮罩关闭 + 返回键/滚动锁；
                      P3：补 @error / alt / 失败兜底，坏图不再能点开 viewer；
                      P1-2：lazy 加载，移动端少解码全尺寸大图。
                    -->
                    <el-image
                      v-if="assetThumbUrl(row) && !brokenThumbUrls.has(assetThumbUrl(row))"
                      :src="assetThumbUrl(row)"
                      :preview-src-list="[assetThumbUrl(row)]"
                      preview-teleported
                      hide-on-click-modal
                      fit="contain"
                      loading="lazy"
                      fetchpriority="low"
                      class="cell-thumb"
                      :alt="row.filename"
                      @show="onViewerShow"
                      @close="onViewerClose"
                      @error="markThumbBroken"
                    >
                      <template #error>
                        <span class="thumb-fallback mono">失败</span>
                      </template>
                    </el-image>
                    <span v-else-if="isImageKey(row.filename)" class="thumb-fallback mono">失败</span>
                    <span v-else class="thumb-fallback mono">{{ assetKind(row.filename) }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="文件名" min-width="160">
                <template #default="{ row }">
                  <span class="mono">{{ row.filename }}</span>
                </template>
              </el-table-column>
              <el-table-column label="包 / 版本" width="150">
                <template #default="{ row }">
                  <span class="mono">{{ row.packId }} · v{{ row.version }}</span>
                </template>
              </el-table-column>
              <el-table-column label="sha256" width="110">
                <template #default="{ row }">
                  <span class="mono" :title="row.sha256">{{ (row.sha256 || '').slice(0, 8) }}…</span>
                </template>
              </el-table-column>
              <el-table-column label="大小" width="86">
                <template #default="{ row }">
                  <span class="mono">{{ fmtBytes(row.bytes) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="上传时间" width="160">
                <template #default="{ row }">
                  <span class="mono time-text">{{ fmtApiTime(row.createdAt) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="104" fixed="right">
                <template #default="{ row }">
                  <el-button size="small" text type="primary" @click="fillCatalogFromAsset(row)">
                    填入目录
                  </el-button>
                </template>
              </el-table-column>
            </el-table>

            <div v-if="assets.length > assetPageSize" class="pane-footer">
              <el-pagination
                layout="prev, pager, next"
                :total="assets.length"
                :page-size="assetPageSize"
                :current-page="assetPage"
                @current-change="assetPage = $event"
              />
            </div>

            <p class="pane-hint mono">
              后端目前没有删除资产的接口（只有 GET / POST /admin/assets），文件只能由运维在资产目录里清理。
            </p>
          </div>
        </el-tab-pane>

        <!-- ============ 3. 时装包 ============ -->
        <el-tab-pane label="时装包" name="packs">
          <div class="pane">
            <div class="pane-toolbar">
              <div class="toolbar-left">
                <span class="count-hint mono">
                  {{ packs.length }} 个已发布包（公开清单 GET /api/fashion/packs）
                </span>
              </div>
              <div class="toolbar-right">
                <el-button size="small" type="primary" @click="openPackDialog">发布时装包</el-button>
              </div>
            </div>

            <el-table
              v-loading="packLoading"
              :data="packs"
              size="small"
              stripe
              class="admin-table"
              empty-text="还没有发布时装包"
              :header-cell-style="headerCellStyle"
            >
              <el-table-column label="包 ID" width="150">
                <template #default="{ row }">
                  <span class="mono">{{ row.id }}</span>
                </template>
              </el-table-column>
              <el-table-column label="版本" width="110">
                <template #default="{ row }">
                  <span class="mono">{{ row.version }}</span>
                </template>
              </el-table-column>
              <el-table-column label="大小" width="96">
                <template #default="{ row }">
                  <span class="mono">{{ fmtBytes(row.bytes) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="sha256" width="110">
                <template #default="{ row }">
                  <span class="mono" :title="row.sha256">{{ (row.sha256 || '').slice(0, 8) }}…</span>
                </template>
              </el-table-column>
              <el-table-column label="发布时间" width="170">
                <template #default="{ row }">
                  <span class="mono time-text">{{ fmtApiTime(row.publishedAt) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="签名" width="88">
                <template #default="{ row }">
                  <el-tag v-if="row.signature" size="small" type="success" effect="plain">已签名</el-tag>
                  <el-tag v-else size="small" type="info" effect="plain">未签名</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="140" fixed="right">
                <template #default="{ row }">
                  <a class="download-link mono" :href="packDownloadUrl(row)" target="_blank" rel="noopener">
                    下载
                  </a>
                  <el-button size="small" text type="danger" @click="removePack(row)">下架</el-button>
                </template>
              </el-table-column>
            </el-table>

            <p class="pane-hint mono">
              下架只从 /packs 移除清单项：已下载到玩家本地的包不会被远程删除，需要玩家自行
              <code>/vmfashion cache clear</code>。
            </p>
          </div>
        </el-tab-pane>
        <!-- ============ 4. 芒果块管理（任务 A3）============ -->
        <el-tab-pane label="芒果块管理" name="wallet">
          <div class="pane">
            <MangoWalletAdmin :cosmetics="catalogRows" />
          </div>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <!-- ============ 目录编辑弹窗 ============ -->
    <el-dialog
      v-model="catalogDialog.visible"
      :title="catalogDialog.isEdit ? '编辑时装' : '新建时装'"
      width="min(560px, 92vw)"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      :before-close="catalogBeforeClose"
      destroy-on-close
    >
      <el-form :model="catalogForm" label-width="96px" class="dialog-form">
        <el-form-item label="cosmeticId" required>
          <el-input
            v-model="catalogForm.cosmeticId"
            :disabled="catalogDialog.isEdit"
            placeholder="全局 ID，如 example:backboard"
          />
          <p class="field-hint mono">
            与 mod 对齐请用 <code>&lt;packId&gt;:&lt;localId&gt;</code>；仅字母/数字/._:-，长度 ≤128。
            <strong>冒号前缀必须与下方「时装包」字段一致</strong>，否则后端 400。
            编辑时不可改（主键）。
          </p>
        </el-form-item>
        <el-form-item label="名称">
          <el-input v-model="catalogForm.name" maxlength="64" placeholder="衣柜里显示的名称；留空则显示 cosmeticId" />
        </el-form-item>
        <el-form-item label="槽位">
          <el-select v-model="catalogForm.category" style="width: 100%">
            <el-option
              v-for="slot in FASHION_SLOTS"
              :key="slot.value"
              :label="`${slot.label}（${slot.value}）`"
              :value="slot.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="时装包">
          <el-input v-model="catalogForm.packId" maxlength="64" placeholder="所属包 ID（可留空）" />
        </el-form-item>
        <el-form-item label="资产键">
          <el-input v-model="catalogForm.assetKey" placeholder="assetKey：资产目录内的相对路径" />
          <p class="field-hint mono">
            留空则衣柜里显示首字占位。可先去「资产管理」上传，再点「填入目录」自动带过来。
          </p>
        </el-form-item>
        <!-- 任务 A3：目录定价（0 = 免费，上限 1,000,000）与是否可购买 -->
        <el-form-item label="价格（芒果块）">
          <el-input-number v-model="catalogForm.priceMango" :min="0" :max="1000000" :step="1" />
          <span class="field-hint mono inline">0 = 免费；范围 0 ~ 1,000,000</span>
        </el-form-item>
        <el-form-item label="可购买">
          <el-switch v-model="catalogForm.purchasable" />
          <span class="field-hint mono inline">关闭后商店不售卖（仅管理端发放），已拥有者仍可穿戴</span>
        </el-form-item>
        <el-form-item label="排序">
          <el-input-number v-model="catalogForm.sortOrder" :min="0" :max="9999" />
          <span class="field-hint mono inline">数字越小越靠前</span>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="catalogForm.enabled" />
          <span class="field-hint mono inline">关闭后玩家衣柜里看不到（已穿戴的配置会保留）</span>
        </el-form-item>
        <!-- 编辑时展示落库的 sha256/大小；「填入目录」新建时也展示（否则带过来的哈希不可见） -->
        <el-form-item v-if="catalogDialog.isEdit || catalogForm.sha256 || catalogForm.bytes" label="sha256 / 大小">
          <span class="mono muted">
            {{ catalogForm.sha256 ? `${catalogForm.sha256.slice(0, 12)}…` : '（无）' }}
            <template v-if="catalogForm.bytes"> · {{ fmtBytes(catalogForm.bytes) }}</template>
          </span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="catalogDialog.visible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="catalogDialog.saving"
          :disabled="!catalogForm.cosmeticId.trim()"
          @click="saveCatalog"
        >
          保存
        </el-button>
      </template>
    </el-dialog>

    <!-- ============ 资产上传弹窗 ============ -->
    <el-dialog
      v-model="assetDialog.visible"
      title="上传资产"
      width="540px"
      :close-on-click-modal="false"
      destroy-on-close
      @close="resetUpload"
    >
      <el-form :model="assetForm" label-width="96px" class="dialog-form">
        <el-form-item label="packId">
          <el-input v-model="assetForm.packId" placeholder="默认 default" />
        </el-form-item>
        <el-form-item label="版本">
          <el-input v-model="assetForm.version" placeholder="默认 1" />
        </el-form-item>
        <el-form-item label="关联时装">
          <el-select
            v-model="assetForm.cosmeticId"
            clearable
            filterable
            allow-create
            default-first-option
            placeholder="可选：选中后自动回填该条目的 sha256/bytes/assetKey"
            style="width: 100%"
          >
            <el-option
              v-for="row in catalogRows"
              :key="row.cosmeticId"
              :label="`${row.cosmeticId}${row.name ? ` · ${row.name}` : ''}`"
              :value="row.cosmeticId"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="文件">
          <el-upload
            :show-file-list="false"
            :before-upload="beforeAssetUpload"
            :http-request="uploadAsset"
            accept=".png,.jpg,.jpeg,.webp,.gif,.glb,.gltf,.bin,.ogg,.wav,.json,.zip,.bbmodel"
          >
            <el-button type="primary" plain :disabled="upload.active">
              {{ upload.active ? '上传中…' : '选择文件并上传' }}
            </el-button>
          </el-upload>
          <div v-if="upload.active" class="upload-progress">
            <el-progress :percentage="upload.percent" :stroke-width="6" :show-text="false" />
            <div class="upload-meta mono">
              <span>{{ fmtBytes(upload.loaded) }} / {{ fmtBytes(upload.total) }}</span>
              <span v-if="upload.speedKBps > 0">· {{ fmtSpeed(upload.speedKBps) }}</span>
              <span v-if="upload.etaSec !== null">· 剩余 {{ fmtEta(upload.etaSec) }}</span>
              <el-button size="small" text type="danger" class="upload-cancel" @click="cancelUpload">
                取消上传
              </el-button>
            </div>
          </div>
          <p v-if="upload.error" class="upload-error">{{ upload.error }}</p>
          <p class="field-hint mono">
            单文件 ≤ 64MB。扩展名白名单：png/jpg/jpeg/webp/gif/glb/gltf/bin/ogg/wav/json/zip/bbmodel。
            .zip 会按时装包规则（根目录 pack.json 且 id 与 packId 一致）校验。
          </p>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="assetDialog.visible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- ============ 时装包上传弹窗 ============ -->
    <el-dialog
      v-model="packDialog.visible"
      title="发布时装包"
      width="520px"
      :close-on-click-modal="false"
      destroy-on-close
      @close="resetUpload"
    >
      <el-form :model="packForm" label-width="96px" class="dialog-form">
        <el-form-item label="packId" required>
          <el-input v-model="packForm.packId" placeholder="如 wings（须与 zip 内 pack.json 的 id 一致）" />
        </el-form-item>
        <el-form-item label="版本" required>
          <el-input v-model="packForm.version" placeholder="如 1.2.0" />
        </el-form-item>
        <el-form-item label="zip 文件">
          <el-upload
            :show-file-list="false"
            :before-upload="beforePackUpload"
            :http-request="uploadPack"
            accept=".zip"
          >
            <el-button type="primary" plain :disabled="upload.active || !packFormValid">
              {{ upload.active ? '上传中…' : '选择 zip 并发布' }}
            </el-button>
          </el-upload>
          <div v-if="upload.active" class="upload-progress">
            <el-progress :percentage="upload.percent" :stroke-width="6" :show-text="false" />
            <div class="upload-meta mono">
              <span>{{ fmtBytes(upload.loaded) }} / {{ fmtBytes(upload.total) }}</span>
              <span v-if="upload.speedKBps > 0">· {{ fmtSpeed(upload.speedKBps) }}</span>
              <span v-if="upload.etaSec !== null">· 剩余 {{ fmtEta(upload.etaSec) }}</span>
              <el-button size="small" text type="danger" class="upload-cancel" @click="cancelUpload">
                取消上传
              </el-button>
            </div>
          </div>
          <p v-if="upload.error" class="upload-error">{{ upload.error }}</p>
          <p class="field-hint mono">
            发布后立即出现在 GET /api/fashion/packs。zip 根目录必须是 pack.json + cosmetics/，
            单包 ≤ 64MB；坏包（路径穿越 / 可执行文件 / 缺 pack.json）会被后端拒绝。
          </p>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="packDialog.visible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadRawFile, UploadRequestOptions } from 'element-plus'
import { roleLabel, useAuthStore } from '@/stores/auth'
import {
  CATEGORY_LABELS,
  FASHION_SLOTS,
  fashionApi,
  fashionErrorCode,
  fashionErrorMessage,
  fashionErrorIsCanceled,
  fashionErrorStatus,
  isSilentFashionError,
  type AdminCatalogItem,
  type CatalogUpsertPayload,
  type FashionAsset,
  type FashionPack,
} from '@/api/fashion'
import { fmtApiTime } from '@/utils/time'
import MangoWalletAdmin from '@/components/fashion/MangoWalletAdmin.vue'
import { useResponsivePageSize } from '@/composables/useResponsivePageSize'
import { useFashionImageViewer } from '@/composables/useFashionImageViewer'

const authStore = useAuthStore()

// P1-1 + P2-9：两处 el-image 全屏大图统一返回键 / 滚动锁 / 横竖屏重置
const { onShow: onViewerShow, onClose: onViewerClose } = useFashionImageViewer()
// P1-3：路由切走后不再写状态 / 弹 toast；上传在卸载时 abort
let isUnmounted = false
// P2-10：目录保存响应竞态（旧响应不得关掉刚打开的新弹窗）
let catalogSaveSeq = 0

const activeTab = ref<'catalog' | 'assets' | 'packs' | 'wallet'>('catalog')
const refreshing = ref(false)
const forbidden = ref(false)

/**
 * 无权限：本地角色判定 + 服务端 403 兜底。
 * 后端是 `require_site_admin`（不是 require_admin），所以 admin 也进不来 ——
 * 侧边栏放「管理」组（minRole 2）会让 admin 看到入口却每次 403。
 */
const denied = computed(() => forbidden.value || (!!authStore.user && !authStore.isSiteAdmin))

// ────────────────────── 目录 ──────────────────────
const catalogRows = ref<AdminCatalogItem[]>([])
const catalogLoading = ref(false)
const catalogKeyword = ref('')
const catalogCategory = ref('')
const catalogPack = ref('')
const catalogPage = ref(1)
// P1-2：移动端少渲染几行（每行一个 44×44 大图请求），避免解码 OOM
const pageSize = useResponsivePageSize(20, 10)
const togglingId = ref('')
// P2-1：按**失败的 URL**记录破图（旧请求 error 不会误杀换了 sha256 的新预览）
const brokenThumbUrls = ref<Set<string>>(new Set())

const catalogDialog = reactive({ visible: false, isEdit: false, saving: false })
const catalogForm = reactive({
  cosmeticId: '',
  name: '',
  category: 'other',
  packId: '',
  assetKey: '',
  sha256: '',
  bytes: 0,
  sortOrder: 0,
  enabled: true,
  // 任务 A3：定价 / 是否可购买
  priceMango: 0,
  purchasable: true,
})

const filteredCatalogRows = computed(() => {
  const keyword = catalogKeyword.value.trim().toLowerCase()
  return catalogRows.value.filter((row) => {
    if (catalogCategory.value && row.category !== catalogCategory.value) return false
    if (catalogPack.value && row.packId !== catalogPack.value) return false
    if (!keyword) return true
    return (
      row.cosmeticId.toLowerCase().includes(keyword) ||
      (row.name || '').toLowerCase().includes(keyword)
    )
  })
})

const pagedCatalogRows = computed(() => {
  const start = (catalogPage.value - 1) * pageSize.value
  return filteredCatalogRows.value.slice(start, start + pageSize.value)
})

// 搜索 / 筛选变化后回到第一页，否则可能停在一个空页上
watch([catalogKeyword, catalogCategory, catalogPack], () => {
  catalogPage.value = 1
})
// P1-2：视口变化让分页大小改变时把页码夹回有效范围
watch(pageSize, () => {
  const maxPage = Math.max(1, Math.ceil(filteredCatalogRows.value.length / pageSize.value))
  if (catalogPage.value > maxPage) catalogPage.value = maxPage
})

// ────────────────────── 资产 ──────────────────────
const assets = ref<FashionAsset[]>([])
const assetLoading = ref(false)
const assetPackFilter = ref('')
const assetDialog = reactive({ visible: false })
const assetForm = reactive({ packId: 'default', version: '1', cosmeticId: '' })

/**
 * 资产表分页。
 *
 * `GET /admin/assets` 没有分页参数、一次返回全量，而生产资产表可以到 5000+ 行
 * （每件时装的贴图 / 模型 / 音频各一行）。`el-table` 不做虚拟滚动，且挂载时
 * 三个 tab 的内容都会渲染 —— 一次性铺 5000 行会直接把管理页卡死（每行还有一个
 * 缩略图请求）。这里按 50 条前端分页，和「目录管理」同一套页码夹紧规则。
 */
const assetPage = ref(1)
// P1-2：资产表同理
const assetPageSize = useResponsivePageSize(50, 20)
// 分页大小随视口变化时把资产表页码夹回有效范围
watch(assetPageSize, () => {
  const maxPage = Math.max(1, Math.ceil(assets.value.length / assetPageSize.value))
  if (assetPage.value > maxPage) assetPage.value = maxPage
})
const pagedAssets = computed(() => {
  const start = (assetPage.value - 1) * assetPageSize.value
  return assets.value.slice(start, start + assetPageSize.value)
})

// ────────────────────── 时装包 ──────────────────────
const packs = ref<FashionPack[]>([])
const packLoading = ref(false)
const packDialog = reactive({ visible: false })
const packForm = reactive({ packId: '', version: '' })
const packFormValid = computed(
  () => /^[A-Za-z0-9._-]{1,64}$/.test(packForm.packId.trim()) && /^[A-Za-z0-9._-]{1,64}$/.test(packForm.version.trim())
)

/** 目录筛选里的「时装包」下拉：目录里出现过的 packId ∪ 已发布包 */
const packOptions = computed(() => {
  const set = new Set<string>()
  for (const row of catalogRows.value) if (row.packId) set.add(row.packId)
  for (const pack of packs.value) set.add(pack.id)
  return [...set].sort()
})

// ────────────────────── 上传（进度 / 网速 / ETA） ──────────────────────
interface UploadState {
  active: boolean
  percent: number
  loaded: number
  total: number
  speedKBps: number
  etaSec: number | null
  error: string
}

/**
 * 上传状态必须用 `reactive()` 直接创建：包在别的 reactive 容器里时，
 * 后续对 `upload.percent` 的赋值会绕过代理 set trap，进度条不刷新
 * （LixiangxiangAdmin.vue:387-452 记过同一个坑）。
 */
const upload = reactive<UploadState>({
  active: false,
  percent: 0,
  loaded: 0,
  total: 0,
  speedKBps: 0,
  etaSec: null,
  error: '',
})
let lastLoaded = 0
let lastTime = 0
/**
 * 每次上传的序号 + 取消句柄。
 *
 * 两个上传弹窗（资产 / 时装包）**共用同一份 `upload` 状态**，而且关闭弹窗时请求
 * 并不会自己停下 —— 没有这两个东西时，旧请求的 `onUploadProgress` 会继续往同一份
 * 状态里写：进度在两个请求之间来回跳、错误文案出现在另一个弹窗里，用户关掉弹窗
 * 后请求还要挂到 120s 超时才结束。现在：
 * - `uploadSeq`：只有「当前这次上传」的回调才允许改 `upload` / 弹结果提示；
 * - `uploadAbort`：取消按钮 / 关闭弹窗时真正 abort 请求（axios `signal`）。
 */
let uploadSeq = 0
let uploadAbort: AbortController | null = null

/** nginx `client_max_body_size 64m` 与后端 MAX_ASSET_BYTES 相等 → 恰好 64MB 会被 nginx 用 HTML 413 拒掉 */
const MAX_UPLOAD_BYTES = 64 * 1024 * 1024
/** multipart 头部/分隔符开销，留 8KB 余量，避免边界文件被 nginx 拒绝（A2） */
const MULTIPART_OVERHEAD = 8 * 1024

const IMAGE_KEY_RE = /\.(png|jpe?g|webp|gif)$/i

function isImageKey(key: string): boolean {
  return IMAGE_KEY_RE.test(key || '')
}

function displayName(row: { name?: string; cosmeticId: string }): string {
  return (row.name || '').trim() || row.cosmeticId
}

/**
 * 目录缩略图 URL（P2-3 带 sha256 版本戳，破 immutable 缓存；P2-4 停用条目
 * 返回空串，模板据此不渲染 img —— 后端对停用 catalog 行 resolve_asset_file 返回 None）。
 */
function catalogThumbUrl(row: AdminCatalogItem): string {
  if (!row.enabled || !row.assetKey || !isImageKey(row.assetKey)) return ''
  return fashionApi.assetUrl(row.assetKey, row.sha256)
}

/** 资产表缩略图 URL（同样带版本戳；非图片返回空串） */
function assetThumbUrl(row: FashionAsset): string {
  if (!isImageKey(row.filename)) return ''
  return fashionApi.assetUrl(row.storagePath, row.sha256)
}

function catalogPreviewList(row: AdminCatalogItem): string[] {
  const url = catalogThumbUrl(row)
  return url ? [url] : []
}

function assetKind(filename: string): string {
  const ext = (filename.split('.').pop() || '').toLowerCase()
  if (['glb', 'gltf', 'bbmodel'].includes(ext)) return '3D'
  if (['zip'].includes(ext)) return 'ZIP'
  if (['ogg', 'wav'].includes(ext)) return '音频'
  if (['json'].includes(ext)) return 'JSON'
  return (ext || '?').toUpperCase().slice(0, 4)
}

/**
 * 预览图 404 / 解码失败：按真正失败的 src（含 ?v=sha256 版本戳）记录。
 * P2-1：按 URL 而非 cosmeticId，assetKey/sha256 变化后旧标记自然失效；
 * el-image 的 @error 收到的是原生 Event，取 e.target.getAttribute('src')。
 */
function markThumbBroken(event: Event): void {
  const el = event.target as HTMLImageElement | null
  const url = el?.getAttribute('src') || el?.currentSrc || ''
  if (!url || brokenThumbUrls.value.has(url)) return
  brokenThumbUrls.value = new Set(brokenThumbUrls.value).add(url)
}

function fmtBytes(bytes: number | null | undefined): string {
  const value = Number(bytes) || 0
  if (value >= 1024 * 1024) return (value / 1024 / 1024).toFixed(1) + ' MB'
  if (value >= 1024) return (value / 1024).toFixed(0) + ' KB'
  return value + ' B'
}

function fmtSpeed(kb: number): string {
  if (kb >= 1024) return (kb / 1024).toFixed(2) + ' MB/s'
  return kb.toFixed(0) + ' KB/s'
}

function fmtEta(sec: number): string {
  if (sec < 60) return Math.ceil(sec) + ' 秒'
  const m = Math.floor(sec / 60)
  const s = Math.ceil(sec % 60)
  return `${m} 分 ${s} 秒`
}

function headerCellStyle() {
  return {
    background: 'var(--bg-card)',
    color: 'var(--text-secondary)',
    fontWeight: 600,
    fontFamily: 'var(--font-sans)',
    fontSize: '12.5px',
    letterSpacing: '0.5px',
    borderBottom: '1px solid var(--border-subtle)',
  }
}

// ────────────────────── 错误文案 ──────────────────────
const FORBIDDEN_TEXT = '仅站点管理员可操作（后端 require_site_admin 拒绝）'

/**
 * 通用错误文案。
 * - 403：后端 `detail` 是英文字符串 "Only site admin can access this resource"，
 *   直接展示会看到英文（A3）→ 统一映射成中文；
 * - 413：nginx 直接返回 HTML，拿不到 detail → 给出可操作的中文提示（A2）。
 */
function adminErrorMessage(error: unknown, fallback: string): string {
  const status = fashionErrorStatus(error)
  if (status === 403) return FORBIDDEN_TEXT
  if (status === 413) return '文件超过 64MB 上限（nginx 直接拒绝），请压缩或拆分后再传'
  const code = fashionErrorCode(error)
  if (code === 'invalid_pack_zip') return 'zip 结构不合规：需根目录 pack.json、不能含可执行/隐藏文件、不能有路径穿越'
  if (code === 'pack_id_mismatch') return 'pack.json 里的 id 与 packId 不一致'
  if (code === 'pack_too_large' || code === 'asset_too_large') return '文件超过 64MB 上限'
  if (code === 'invalid_category') return '槽位（category）不合法'
  // ⚠️ 不要把 `invalid_cosmetic_id` 压成一句固定的「只允许字母/数字/._:-，长度 ≤128」：
  // 后端 validate_cosmetic_id 有 5 种更具体的原因（空 / 冒号不是恰好一个 / packId 前缀
  // 不合规 / localId 字符集 / **前缀与 packId 字段不一致**），文案各不相同。
  // 最常见的一种恰恰是最后那条（编辑时改了「时装包」、或「填入目录」自动带进来的
  // packId 与手填前缀冲突），压成字符集提示会把管理员引到错误的修改方向。
  // 这里直接透传后端的中文 message（fashionErrorMessage 会取 detail.message）。
  const axiosCode = (error as { code?: string })?.code
  if (axiosCode === 'ECONNABORTED') return '请求超时，请检查网络后重试'
  return fashionErrorMessage(error, fallback)
}

/**
 * 统一的错误 toast（本页所有 catch 都走它）。
 *
 * 与直接 `ElMessage.error(adminErrorMessage(...))` 的区别：401（登录过期）由
 * `api/client.ts` 拦截器统一提示并跳登录、主动取消（`ERR_CANCELED`）不是错误，
 * 这两类不再重复弹。
 */
function notifyAdminError(error: unknown, fallback: string): void {
  if (isUnmounted || isSilentFashionError(error)) return
  ElMessage.error(adminErrorMessage(error, fallback))
}

/** 列表加载失败：403 时切到「无权限」页，其余给 toast（避免一次挂载弹三条重复错误） */
function handleLoadError(error: unknown, fallback: string): void {
  if (isUnmounted) return
  if (fashionErrorStatus(error) === 403) {
    forbidden.value = true
    return
  }
  notifyAdminError(error, fallback)
}

// ────────────────────── 加载 ──────────────────────
// 请求竞态保护：连点「刷新」、改筛选、上传成功后回读，都可能并发同一个 loader；
// 没有序号时旧响应会覆盖新响应（例如刚按 packId 过滤完，又被一次全量响应刷回全部）。
let catalogSeq = 0
let assetSeq = 0
let packSeq = 0

async function loadCatalog(): Promise<void> {
  const seq = ++catalogSeq
  catalogLoading.value = true
  try {
    const { data } = await fashionApi.adminCatalog()
    if (isUnmounted || seq !== catalogSeq) return
    catalogRows.value = data.items || []
    // 与衣柜页同一条规则：目录重新拉成功 = 资产可能已补齐，缩略图要重试一次
    brokenThumbUrls.value = new Set()
    // 条目变少（删除 / 筛选）时把页码夹回有效范围，避免停在空页
    const maxPage = Math.max(1, Math.ceil(filteredCatalogRows.value.length / pageSize.value))
    if (catalogPage.value > maxPage) catalogPage.value = maxPage
  } catch (error) {
    if (isUnmounted || seq !== catalogSeq) return
    handleLoadError(error, '读取时装目录失败')
  } finally {
    if (!isUnmounted && seq === catalogSeq) catalogLoading.value = false
  }
}

async function loadAssets(): Promise<void> {
  const seq = ++assetSeq
  assetLoading.value = true
  try {
    const { data } = await fashionApi.adminAssets(
      assetPackFilter.value.trim() ? { pack_id: assetPackFilter.value.trim() } : undefined
    )
    if (isUnmounted || seq !== assetSeq) return
    assets.value = data.items || []
    // 过滤 / 回读后条数变少时夹紧页码，避免停在空页（与目录管理同一规则）
    const maxPage = Math.max(1, Math.ceil(assets.value.length / assetPageSize.value))
    if (assetPage.value > maxPage) assetPage.value = maxPage
  } catch (error) {
    if (isUnmounted || seq !== assetSeq) return
    handleLoadError(error, '读取资产列表失败')
  } finally {
    if (!isUnmounted && seq === assetSeq) assetLoading.value = false
  }
}

async function loadPacks(): Promise<void> {
  const seq = ++packSeq
  packLoading.value = true
  try {
    const { data } = await fashionApi.packs()
    if (isUnmounted || seq !== packSeq) return
    packs.value = data.packs || []
  } catch (error) {
    if (isUnmounted || seq !== packSeq) return
    handleLoadError(error, '读取时装包清单失败')
  } finally {
    if (!isUnmounted && seq === packSeq) packLoading.value = false
  }
}

/** 资产页按 packId 过滤：输入框 change（回车 / 失焦）才发请求，并回到第 1 页 */
async function onAssetFilterChange(): Promise<void> {
  assetPage.value = 1
  await loadAssets()
}

async function refreshActive(): Promise<void> {
  if (refreshing.value || denied.value) return
  refreshing.value = true
  try {
    if (activeTab.value === 'assets') await loadAssets()
    else if (activeTab.value === 'packs') await loadPacks()
    else if (activeTab.value === 'wallet') return // 芒果块管理自带刷新按钮
    else await loadCatalog()
  } finally {
    refreshing.value = false
  }
}

// ────────────────────── 目录：编辑 / 上下架 / 删除 ──────────────────────
function openCatalogDialog(row: AdminCatalogItem | null): void {
  // P2-10：每次打开都作废旧在飞的保存响应，避免它回来时关掉刚打开的新弹窗
  ++catalogSaveSeq
  catalogDialog.isEdit = !!row
  catalogForm.cosmeticId = row?.cosmeticId || ''
  catalogForm.name = row?.name || ''
  catalogForm.category = row?.category || 'other'
  catalogForm.packId = row?.packId || ''
  catalogForm.assetKey = row?.assetKey || ''
  catalogForm.sha256 = row?.sha256 || ''
  catalogForm.bytes = row?.bytes || 0
  catalogForm.sortOrder = row?.sortOrder ?? 0
  catalogForm.enabled = row?.enabled ?? true
  // 任务 A3：定价 / 可购买（老数据可能缺字段，给默认值）
  catalogForm.priceMango = Number(row?.priceMango) || 0
  catalogForm.purchasable = row?.purchasable !== false
  catalogDialog.visible = true
}

/** 资产表「填入目录」：把已上传资产直接变成一条目录条目（packId/assetKey/sha256/bytes 全带上） */
function fillCatalogFromAsset(asset: FashionAsset): void {
  openCatalogDialog(null)
  catalogForm.packId = asset.packId || ''
  catalogForm.assetKey = asset.storagePath || ''
  catalogForm.sha256 = asset.sha256 || ''
  catalogForm.bytes = asset.bytes || 0
  catalogForm.name = (asset.filename || '').replace(/\.[^.]+$/, '')
  // 「时装包」已被自动填成资产的 packId：新建的 cosmeticId 若用全局形式，
  // 冒号前缀必须与它一致（后端 validate_cosmetic_id 强制），这里提前说清楚，
  // 否则保存时只会看到一句 invalid_cosmetic_id。
  ElMessage.info(
    `已带入资产信息（时装包 ${asset.packId || '—'}）。cosmeticId 若用 <packId>:<localId> 形式，前缀必须是 ${asset.packId || '上面的时装包'}`
  )
}

function buildCatalogPayload(): CatalogUpsertPayload {
  const payload: CatalogUpsertPayload = {
    category: catalogForm.category,
    packId: catalogForm.packId.trim(),
    assetKey: catalogForm.assetKey.trim(),
    sortOrder: catalogForm.sortOrder,
    enabled: catalogForm.enabled,
    // 任务 A3：定价 / 是否可购买（POST/PUT 支持部分更新，这里总是带上）
    priceMango: Math.max(0, Math.min(1000000, Math.trunc(Number(catalogForm.priceMango) || 0))),
    purchasable: catalogForm.purchasable,
  }
  const name = catalogForm.name.trim()
  // 新建时留空不传：后端会把 name 默认成 cosmeticId（比存空串好）
  if (name || catalogDialog.isEdit) payload.name = name
  // v1.2 修复：「资产管理 → 填入目录」会把资产的 sha256/bytes 填进表单，但这里原来
  // 没带上它们 → 后端 upsert 落库成 sha256="" / bytes=0，目录条目与真实资产脱钩
  // （mod 端按 sha256 判重/校验、后端按 sha256 判 pack_in_use 都依赖这两个字段）。
  // 空值不传，保持 PUT 的部分更新语义。
  const sha256 = catalogForm.sha256.trim()
  if (sha256) payload.sha256 = sha256
  if (catalogForm.bytes > 0) payload.bytes = catalogForm.bytes
  return payload
}

/**
 * 「时装包」字段本身的校验：与后端 `is_safe_pack_id` 对齐（空值合法 = 不指定包）。
 *
 * 为什么必须单独校验：`validate_cosmetic_id` 只在**全局 ID**（带冒号）时才检查
 * 前缀，而 `packId` 字段无论 ID 形态都会过一遍 `is_safe_pack_id`。只在冒号分支里
 * 校验前缀，会漏掉「简单 ID + 非法 packId」这一路（例如填了 `server` 或 `.tmp`），
 * 管理员仍要吃一次 400 `invalid_pack_id` 才知道。
 */
function validatePackIdField(packId: string): string {
  if (!packId) return ''
  if (!/^[A-Za-z0-9._-]{1,64}$/.test(packId)) {
    return '「时装包」只允许字母/数字/._-，长度 1-64'
  }
  if (packId === '.' || packId === '..' || packId.startsWith('.')) {
    return '「时装包」不能是 . / .. 或以点开头（客户端会把点开头的包当临时目录清理）'
  }
  // 后端是 value.lower() in {"server"}：大小写不敏感，Server / SERVER 同样被拒
  if (packId.toLowerCase() === 'server') {
    return '「时装包」不能叫 server（客户端保留名）'
  }
  return ''
}

/**
 * 目录表单的前端预校验：与后端 `core/fashion_service.py:validate_cosmetic_id` 对齐。
 *
 * 为什么不能只靠后端：`invalid_cosmetic_id` 的 5 种原因里，管理员最常踩的是
 * **全局 ID 的冒号前缀必须等于「时装包」字段**（编辑时改了 packId、或「填入目录」
 * 自动带进来的 packId 与手填前缀不一致）—— 只发一次 400 往返，用户还得自己猜
 * 该改哪一边。这里逐条给出「改哪边」的提示。
 *
 * @returns 错误文案；空串表示通过
 */
function validateCatalogCosmeticId(cosmeticId: string, packId: string): string {
  if (!cosmeticId) return 'cosmeticId 不能为空'
  if (!/^[A-Za-z0-9._:-]{1,128}$/.test(cosmeticId)) {
    return 'cosmeticId 只允许字母/数字/._:-，长度 ≤128'
  }
  // 先校验「时装包」字段（后端顺序也是先 cid 格式、再 is_safe_pack_id(pid)）
  const packIdError = validatePackIdField(packId)
  if (packIdError) return packIdError
  const colons = (cosmeticId.match(/:/g) || []).length
  if (colons === 0) return '' // 历史简单 ID：接受（legacy），packId 独立生效
  if (colons > 1) return 'cosmeticId 只能有一个冒号（形如 <packId>:<localId>）'
  const [rawPrefix, rawLocal] = cosmeticId.split(':')
  const prefix = rawPrefix.trim()
  const local = rawLocal.trim()
  if (!prefix || !local) return 'cosmeticId 形如 <packId>:<localId>，冒号两段都不能为空'
  if (
    !/^[A-Za-z0-9._-]{1,64}$/.test(prefix) ||
    prefix === '.' ||
    prefix === '..' ||
    prefix.toLowerCase() === 'server' ||
    prefix.startsWith('.')
  ) {
    return `cosmeticId 的包前缀「${prefix}」不能当目录名：只允许字母/数字/._-，长度 1-64，且不能是 . / .. / server / 点开头`
  }
  if (!/^[A-Za-z0-9._-]{1,96}$/.test(local)) {
    return 'cosmeticId 的 localId 只允许字母/数字/._-，长度 ≤96'
  }
  if (packId && packId !== prefix) {
    return `cosmeticId 的包前缀「${prefix}」必须与「时装包」字段「${packId}」一致：改前缀，或把「时装包」改成 ${prefix}`
  }
  return ''
}

/**
 * P2-10：保存期间禁止关闭弹窗（before-close + ESC），并给每次保存一个序号。
 * 「保存中 ESC / 点 ✕ → 立刻重开新弹窗」时旧响应回来会执行 visible=false 关掉
 * 新弹窗、还把旧 row 塞回表格；用 seq 比对后旧响应直接作废。
 */
function catalogBeforeClose(done: () => void): void {
  if (catalogDialog.saving) {
    ElMessage.warning('正在保存，请稍候')
    return
  }
  done()
}

async function saveCatalog(): Promise<void> {
  const cosmeticId = catalogForm.cosmeticId.trim()
  const packId = catalogForm.packId.trim()
  const invalid = validateCatalogCosmeticId(cosmeticId, packId)
  if (invalid) {
    ElMessage.warning(invalid)
    return
  }
  const seq = ++catalogSaveSeq
  catalogDialog.saving = true
  try {
    if (catalogDialog.isEdit) {
      const { data } = await fashionApi.adminUpdateCatalog(cosmeticId, buildCatalogPayload())
      if (isUnmounted || seq !== catalogSaveSeq) return
      const index = catalogRows.value.findIndex((row) => row.cosmeticId === cosmeticId)
      if (index >= 0) catalogRows.value.splice(index, 1, data)
      // P2-1：局部替换 row 后可能换了 assetKey/sha256，清掉旧破图标记
      brokenThumbUrls.value = new Set()
      ElMessage.success('已保存')
    } else {
      const { data } = await fashionApi.adminCreateCatalog({
        ...buildCatalogPayload(),
        cosmeticId,
      })
      if (isUnmounted || seq !== catalogSaveSeq) return
      catalogRows.value = [data, ...catalogRows.value.filter((row) => row.cosmeticId !== data.cosmeticId)]
      catalogPage.value = 1
      brokenThumbUrls.value = new Set()
      ElMessage.success('已创建')
    }
    catalogDialog.visible = false
  } catch (error) {
    if (isUnmounted || seq !== catalogSaveSeq) return
    notifyAdminError(error, '保存失败')
  } finally {
    if (seq === catalogSaveSeq) catalogDialog.saving = false
  }
}

async function toggleEnabled(row: AdminCatalogItem, value: string | number | boolean): Promise<void> {
  const next = Boolean(value)
  if (togglingId.value) return
  togglingId.value = row.cosmeticId
  try {
    const { data } = await fashionApi.adminUpdateCatalog(row.cosmeticId, { enabled: next })
    if (isUnmounted) return
    Object.assign(row, data)
    ElMessage.success(next ? '已上架' : '已下架')
  } catch (error) {
    notifyAdminError(error, '切换上下架失败')
  } finally {
    togglingId.value = ''
  }
}

async function removeCatalog(row: AdminCatalogItem): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除「${displayName(row)}」（${row.cosmeticId}）？删除后无法恢复；` +
        '已经穿戴这件时装的玩家，衣柜里会显示「已下架」，需要他们重新选择。',
      '删除时装',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    await fashionApi.adminDeleteCatalog(row.cosmeticId)
    if (isUnmounted) return
    catalogRows.value = catalogRows.value.filter((item) => item.cosmeticId !== row.cosmeticId)
    // 删掉「最后一页的最后一条」后页码会越界：表格拿到空数组 → 显示 empty-text
    // 「目录里还没有时装」，而分页器还停在已经不存在的第 N 页（用户以为目录被清空了）。
    // 与 loadCatalog 同一条夹紧规则。
    const maxPage = Math.max(1, Math.ceil(filteredCatalogRows.value.length / pageSize.value))
    if (catalogPage.value > maxPage) catalogPage.value = maxPage
    ElMessage.success('已删除')
  } catch (error) {
    notifyAdminError(error, '删除失败')
  }
}

// ────────────────────── 上传 ──────────────────────
/**
 * 清空上传状态，并**作废进行中的请求**。
 *
 * `uploadSeq++` 是关键：调用它的地方（关闭弹窗 / 打开另一个上传弹窗 / 取消）
 * 之后，仍在飞的那次上传的回调会被 `seq !== uploadSeq` 挡掉，不会把进度和结果
 * 写进已经属于「下一次上传」的状态里。
 */
function resetUpload(): void {
  uploadSeq++
  uploadAbort?.abort()
  uploadAbort = null
  upload.active = false
  upload.percent = 0
  upload.loaded = 0
  upload.total = 0
  upload.speedKBps = 0
  upload.etaSec = null
  upload.error = ''
  lastLoaded = 0
  lastTime = 0
}

/** 开始一次上传，返回本次上传的序号（用于判定回调是否仍属当前上传） */
function startUpload(total: number): number {
  resetUpload()
  const seq = uploadSeq
  upload.active = true
  upload.total = total
  lastTime = performance.now()
  uploadAbort = new AbortController()
  return seq
}

/** 用户点「取消上传」：中止请求，状态由 catch 分支统一收尾 */
function cancelUpload(): void {
  if (!upload.active) return
  uploadAbort?.abort()
}

function onUploadTick(seq: number) {
  return (event: { loaded: number; total: number }): void => {
    if (seq !== uploadSeq) return // 旧上传的回调：丢弃，别写坏新上传的进度
    const now = performance.now()
    const dt = (now - lastTime) / 1000
    upload.loaded = event.loaded
    if (event.total) upload.total = event.total
    if (dt > 0.15) {
      const delta = event.loaded - lastLoaded
      upload.speedKBps = delta > 0 ? delta / 1024 / dt : upload.speedKBps * 0.9
      lastLoaded = event.loaded
      lastTime = now
    }
    upload.percent =
      upload.total > 0 ? Math.min(99, Math.round((upload.loaded / upload.total) * 100)) : 0
    upload.etaSec =
      upload.speedKBps > 0.05 && upload.total > upload.loaded
        ? (upload.total - upload.loaded) / 1024 / upload.speedKBps
        : null
  }
}

/**
 * 选文件时就拦（A2）：nginx 的 64m 与后端 64MB 相等，multipart 还有开销，
 * 恰好 64MB 的文件会被 nginx 用 HTML 413 拒掉，前端连 detail 都拿不到。
 */
function checkUploadSize(file: File): boolean {
  if (!file.size) {
    ElMessage.error('文件是空的，无法上传')
    return false
  }
  if (file.size > MAX_UPLOAD_BYTES - MULTIPART_OVERHEAD) {
    ElMessage.error(
      `文件 ${fmtBytes(file.size)}，超过 64MB 上限（nginx 与后端上限相等，multipart 还有头部开销，` +
        '恰好 64MB 也会被拒）。请压缩或拆分后再传。'
    )
    return false
  }
  return true
}

function beforeAssetUpload(rawFile: UploadRawFile): boolean {
  return checkUploadSize(rawFile)
}

function beforePackUpload(rawFile: UploadRawFile): boolean {
  if (!rawFile.name.toLowerCase().endsWith('.zip')) {
    ElMessage.error('时装包必须是 .zip')
    return false
  }
  return checkUploadSize(rawFile)
}

function openAssetDialog(): void {
  resetUpload()
  assetForm.packId = 'default'
  assetForm.version = '1'
  assetForm.cosmeticId = ''
  assetDialog.visible = true
}

function openPackDialog(): void {
  resetUpload()
  packForm.packId = ''
  packForm.version = ''
  packDialog.visible = true
}

async function uploadAsset(options: UploadRequestOptions): Promise<void> {
  const file = options.file as File
  const seq = startUpload(file.size)
  try {
    const result = await fashionApi.adminUploadAsset(
      {
        file,
        packId: assetForm.packId.trim() || 'default',
        version: assetForm.version.trim() || '1',
        cosmeticId: assetForm.cosmeticId,
      },
      onUploadTick(seq),
      uploadAbort?.signal
    )
    if (seq !== uploadSeq) return // 上传期间弹窗被关闭 / 发起了新上传：结果作废
    upload.percent = 100
    upload.active = false
    ElMessage.success(
      result.cosmeticId
        ? `上传成功，已回填 ${result.cosmeticId} 的 sha256/bytes/assetKey`
        : `上传成功：${result.asset.filename}`
    )
    assetDialog.visible = false
    await Promise.all([loadAssets(), loadCatalog()])
  } catch (error) {
    if (seq !== uploadSeq) return
    upload.active = false
    if (fashionErrorIsCanceled(error)) {
      // 用户自己点的「取消上传」/关掉了弹窗：不是失败，不弹红条
      resetUpload()
      ElMessage.info('已取消上传')
      return
    }
    upload.error = adminErrorMessage(error, '上传失败')
    // 401 已由拦截器统一提示（弹窗内仍保留一行说明），不再重复弹红条
    if (!isSilentFashionError(error)) ElMessage.error(upload.error)
  } finally {
    if (seq === uploadSeq) uploadAbort = null
  }
}

async function uploadPack(options: UploadRequestOptions): Promise<void> {
  const file = options.file as File
  const seq = startUpload(file.size)
  try {
    const result = await fashionApi.adminUploadPack(
      { file, packId: packForm.packId.trim(), version: packForm.version.trim() },
      onUploadTick(seq),
      uploadAbort?.signal
    )
    if (seq !== uploadSeq) return
    upload.percent = 100
    upload.active = false
    ElMessage.success(`已发布 ${result.pack.id} v${result.pack.version}（${fmtBytes(result.pack.bytes)}）`)
    packDialog.visible = false
    await loadPacks()
  } catch (error) {
    if (seq !== uploadSeq) return
    upload.active = false
    if (fashionErrorIsCanceled(error)) {
      resetUpload()
      ElMessage.info('已取消上传')
      return
    }
    upload.error = adminErrorMessage(error, '发布失败')
    if (!isSilentFashionError(error)) ElMessage.error(upload.error)
  } finally {
    if (seq === uploadSeq) uploadAbort = null
  }
}

// ────────────────────── 时装包：下载 / 下架 ──────────────────────
function packDownloadUrl(pack: FashionPack): string {
  return `/api/fashion/packs/${encodeURIComponent(pack.id)}/${encodeURIComponent(pack.version)}/download`
}

async function removePack(pack: FashionPack): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认下架「${pack.id} v${pack.version}」？下架后新玩家不会再下载到它；` +
        '已经下载到本地的玩家不受影响（需要他们自行 /vmfashion cache clear）。',
      '下架时装包',
      { type: 'warning', confirmButtonText: '下架', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    await fashionApi.adminDeletePack(pack.id, pack.version)
    if (isUnmounted) return
    packs.value = packs.value.filter(
      (item) => !(item.id === pack.id && item.version === pack.version)
    )
    ElMessage.success('已下架')
  } catch (error) {
    // A6：409 pack_in_use 需要专门文案 + 引导，否则管理员只看到一句通用失败
    if (fashionErrorCode(error) === 'pack_in_use') {
      await ElMessageBox.alert(
        `这个包的 sha256 仍被「目录管理」里的条目引用，后端拒绝下架（409 pack_in_use）。` +
          '请先在「目录管理」里把引用它的条目改到别的包或删除，再回来下架。',
        '无法下架：包仍在使用中',
        { type: 'warning', confirmButtonText: '知道了' }
      ).catch(() => undefined)
      return
    }
    notifyAdminError(error, '下架失败')
  }
}

onMounted(async () => {
  if (denied.value) return
  await loadCatalog()
  if (isUnmounted || forbidden.value) return
  await Promise.all([loadAssets(), loadPacks()])
})

/**
 * P1-3：路由切换销毁组件时 EP 不会触发上传弹窗的 @closed，64MB 上传会继续
 * 跑到 120s 超时、进度回调写已卸载组件、toast 弹在下一个页面。这里真正 abort，
 * 并作废所有在飞请求（seq++ 让它们的回调全部 return），不再写 reactive 状态。
 */
onUnmounted(() => {
  isUnmounted = true
  ++catalogSeq
  ++assetSeq
  ++packSeq
  ++catalogSaveSeq
  resetUpload()
})
</script>

<style scoped>
.fashion-admin-page {
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

.forbidden-alert {
  margin-bottom: var(--space-4);
}

.admin-card {
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
}

.admin-tabs :deep(.el-tabs__nav-wrap::after) {
  height: 1px;
  background-color: var(--border-subtle);
}

.admin-tabs :deep(.el-tabs__item) {
  font-family: var(--font-sans);
  font-size: 14.5px;
  color: var(--text-muted);
}

.admin-tabs :deep(.el-tabs__item.is-active) {
  color: var(--text-primary);
}

.admin-tabs :deep(.el-tabs__active-bar) {
  background-color: var(--accent);
}

.pane {
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

.filter-select {
  width: 150px;
  max-width: 100%;
}

.count-hint {
  font-size: 12px;
  color: var(--text-secondary);
}

.admin-table {
  width: 100%;
}

.thumb-cell {
  width: 44px;
  height: 44px;
  border-radius: var(--radius-md);
  background: var(--bg-hover);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}

.cell-thumb {
  width: 44px;
  height: 44px;
}

.thumb-fallback {
  font-family: var(--font-display);
  font-size: 18px;
  color: var(--text-secondary);
}

.mono {
  font-family: var(--font-mono);
  font-size: 12px;
}

.muted {
  color: var(--text-muted);
}

.time-text {
  color: var(--text-secondary);
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

.pane-hint code {
  font-family: var(--font-mono);
  background: var(--bg-hover);
  padding: 0 4px;
  border-radius: 4px;
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

.field-hint code {
  font-family: var(--font-mono);
}

.upload-progress {
  width: 100%;
  margin-top: var(--space-2);
}

.upload-meta {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
  font-size: 11.5px;
  color: var(--text-secondary);
  margin-top: var(--space-1);
}

/* 「取消上传」：贴在进度信息行右侧，64MB 慢速上行时给用户一条退路 */
.upload-cancel {
  margin-left: auto;
  padding: 0 4px;
}

.upload-error {
  margin: var(--space-2) 0 0;
  font-size: 12px;
  color: var(--danger);
  line-height: 1.7;
}

.download-link {
  color: var(--accent);
  text-decoration: underline;
  text-underline-offset: 2px;
  margin-right: var(--space-2);
}

.link {
  color: var(--text-primary);
  text-decoration: underline;
  text-decoration-color: var(--accent);
  text-underline-offset: 2px;
}

/* 任务 A3：定价列语义色、停用预览占位 */
.price-paid {
  color: var(--accent);
  font-weight: 600;
}

.price-free {
  color: var(--success);
}

.off-fallback {
  font-family: var(--font-sans);
  font-size: 11px;
  color: var(--text-muted);
  white-space: nowrap;
}

@media (max-width: 900px) {
  .fashion-admin-page {
    padding: var(--space-3);
  }

  .search-input,
  .filter-select {
    width: 100%;
  }

  .pane-toolbar {
    flex-direction: column;
    align-items: flex-start;
  }
}

/* 目录/资产表格列很多：小屏靠横向滚动查看，不让整页横向溢出 */
@media (max-width: 768px) {
  .admin-table {
    min-width: 640px;
  }
  .pane {
    overflow-x: auto;
  }
}
</style>
