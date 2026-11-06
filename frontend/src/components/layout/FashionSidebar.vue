<template>
  <div class="sidebar fashion-sidebar" :class="{ collapsed, 'is-mobile': isMobile }">
    <div class="sidebar-logo">
      <button
        class="collapse-btn"
        @click="emit('toggle')"
        :title="collapseTitle"
        :aria-label="collapseTitle"
        :aria-expanded="isMobile ? undefined : !collapsed"
      >
        <span class="collapse-icon">{{ collapseIcon }}</span>
      </button>
      <span class="logo-text" v-show="!collapsed">时装中心</span>
    </div>

    <!-- 🥭 芒果块余额：所有时装页共用这一个侧边栏实例，只在挂载时读一次 wallet()；
         读失败静默降级显示「—」，不阻塞导航。折叠态（含移动端）仍显示数字。 -->
    <div class="sidebar-wallet">
      <MangoWalletPanel
        :balance="mangoBalance"
        :loading="mangoLoading"
        :compact="!!collapsed"
      />
    </div>

    <!-- 时装中心专属导航：只保留时装相关内容 -->
    <nav class="sidebar-nav" aria-label="时装中心导航">
      <div class="nav-group">
        <div class="nav-group-title-row" v-show="!collapsed">
          <span class="nav-group-title">时装</span>
        </div>
        <div class="group-items">
          <router-link
            v-for="item in visibleItems"
            :key="item.path"
            :to="item.path"
            class="nav-item"
            :class="{ active: isActive(item) }"
            :title="collapsed ? item.label : item.desc"
            :aria-current="isActive(item) ? 'page' : undefined"
            @click="onNavClick"
          >
            <span class="nav-dot"></span>
            <span class="nav-label" v-show="!collapsed">
              <span class="nav-label-main">{{ item.label }}</span>
              <span class="nav-label-desc" v-show="!collapsed">{{ item.desc }}</span>
            </span>
          </router-link>
        </div>
      </div>

      <!-- 绑定页不属于「三类」主入口，但必须可达：做成次要工具项 -->
      <div class="nav-group nav-group-secondary">
        <div class="nav-group-title-row" v-show="!collapsed">
          <span class="nav-group-title">账号</span>
        </div>
        <div class="group-items">
          <router-link
            to="/fashion/bind"
            class="nav-item nav-item-secondary"
            :class="{ active: isActive({ path: '/fashion/bind' }) }"
            :title="collapsed ? '时装绑定' : '把 Minecraft 账号绑定到本站账号'"
            :aria-current="isActive({ path: '/fashion/bind' }) ? 'page' : undefined"
            @click="onNavClick"
          >
            <span class="nav-dot"></span>
            <span class="nav-label" v-show="!collapsed">
              <span class="nav-label-main">时装绑定</span>
            </span>
          </router-link>
        </div>
      </div>
    </nav>

    <div class="sidebar-status">
      <router-link
        to="/dashboard"
        class="back-link"
        :title="collapsed ? '返回主站' : undefined"
        aria-label="返回主站"
      >
        <span class="back-icon">←</span>
        <span class="back-text" v-show="!collapsed">返回主站</span>
      </router-link>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import MangoWalletPanel from '@/components/fashion/MangoWalletPanel.vue'
import { useMangoWallet } from '@/components/fashion/useMangoWallet'

/** 时装中心导航项（顺序即展示顺序） */
interface FashionNavItem {
  path: string
  label: string
  /** 悬停提示 / 副标题 */
  desc: string
  /** 仅站点管理员可见 */
  siteAdminOnly?: boolean
}

const props = defineProps<{
  collapsed?: boolean
  isMobile?: boolean
}>()
const emit = defineEmits<{
  toggle: []
}>()

const route = useRoute()
const authStore = useAuthStore()

/**
 * 芒果块余额（任务 A4）。
 *
 * 只在**本组件挂载时**读一次：FashionSidebar 挂在 AppLayout 上，切换时装路由时
 * 组件实例不变（不会重新 onMounted），所以不会「每次路由切换都重复并发请求」。
 * `useMangoWallet()` 内部还有「已读就不重复请求 + 在飞请求复用 + seq 竞态 +
 * 会话切换重读 + 卸载保护」；这里再留一个 isUnmounted 标记，保证卸载后即使
 * 请求回来也不写任何状态、不弹提示。
 */
const { mangoBalance, mangoLoading, ensureWallet } = useMangoWallet()
let isUnmounted = false
onMounted(() => {
  if (isUnmounted) return
  void ensureWallet()
})
onUnmounted(() => {
  isUnmounted = true
})

const collapseIcon = computed(() => {
  if (props.isMobile) return '✕'
  return props.collapsed ? '▶' : '◀'
})

const collapseTitle = computed(() => {
  if (props.isMobile) return '关闭菜单'
  return props.collapsed ? '展开' : '收起'
})

function onNavClick() {
  if (props.isMobile) emit('toggle')
}

/**
 * 时装中心三类入口（用户需求）：
 * - 时装商店：浏览全部时装目录
 * - 我的衣柜：挑选 / 保存自己的穿戴
 * - 时装管理：目录 / 资产 / 时装包维护，**仅站点管理员**（后端 require_site_admin）
 */
const navItems: FashionNavItem[] = [
  { path: '/fashion/store', label: '时装商店', desc: '浏览全部时装' },
  { path: '/fashion/wardrobe', label: '我的衣柜', desc: '挑选并保存穿戴' },
  { path: '/fashion/admin', label: '时装管理', desc: '目录 / 资产 / 时装包', siteAdminOnly: true },
]

/**
 * 可见项：时装管理只对站点管理员开放。
 *
 * 后端是 `require_site_admin`（admin 调接口会 403），所以这里必须用
 * `isSiteAdmin` 而不是 `isAdmin` —— 否则普通管理员会看到入口却处处 403。
 */
const visibleItems = computed(() =>
  navItems.filter((item) => !item.siteAdminOnly || authStore.isSiteAdmin)
)

/** 高亮判定：/fashion/wardrobe 的详情/查询串仍归衣柜；其余按前缀匹配 */
function isActive(item: { path: string }): boolean {
  if (item.path === '/fashion/store') {
    return route.path === '/fashion/store' || route.path === '/fashion'
  }
  return route.path === item.path || route.path.startsWith(item.path + '/')
}
</script>

<style scoped>
/* 与主站侧边栏（AppSidebar）共用同一套布局与 Token，保证视觉一致 */
.sidebar {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--bg-card);
  border-right: 1px solid var(--border-subtle);
}

.sidebar-logo {
  padding: 16px 14px;
  border-bottom: 1px solid var(--border-subtle);
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 56px;
}

/* 芒果块余额卡片：折叠态仍显示数字；窄栏内不横向溢出 */
.sidebar-wallet {
  padding: 10px 10px;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}

.sidebar-wallet :deep(.mango-wallet-panel) {
  width: 100%;
}

.sidebar.collapsed .sidebar-wallet {
  padding: 8px 6px;
}

.sidebar.collapsed .sidebar-wallet :deep(.mango-balance) {
  font-size: 11px;
}

.collapse-btn {
  background: none;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  color: var(--text-secondary);
  width: 30px;
  height: 30px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  font-size: 12px;
  flex-shrink: 0;
  transition: background-color 150ms var(--ease-out), border-color 150ms var(--ease-out), color 150ms var(--ease-out);
}

.collapse-btn:hover {
  border-color: var(--accent);
  background: var(--bg-hover);
  color: var(--text-primary);
}

.is-mobile .collapse-btn {
  width: 36px;
  height: 36px;
  font-size: 16px;
}

.logo-text {
  font-family: var(--font-display);
  font-size: 18px;
  font-weight: 400;
  letter-spacing: -0.5px;
  color: var(--text-primary);
  white-space: nowrap;
  transition: opacity 0.2s ease;
}

.sidebar-nav {
  flex: 1;
  padding: 12px 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
}

/* ---- 分组 ---- */
.nav-group {
  display: flex;
  flex-direction: column;
  padding-top: 6px;
}

.nav-group:first-child {
  padding-top: 0;
}

.nav-group-title-row {
  padding: 8px 14px;
  min-height: 34px;
  display: flex;
  align-items: center;
}

.nav-group-title {
  font-family: var(--font-sans);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  color: var(--text-muted);
  opacity: 0.9;
  white-space: nowrap;
}

/* 次要分组（账号）与主入口之间留一条分隔线，视觉上弱化 */
.nav-group-secondary {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--border-subtle);
}

.group-items {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 12px;
  text-decoration: none;
  color: var(--text-secondary);
  font-family: var(--font-sans);
  font-size: 14px;
  font-weight: 500;
  border-radius: var(--radius-md);
  position: relative;
  transition: background-color 150ms var(--ease-out), color 150ms var(--ease-out);
  cursor: pointer;
  min-height: 44px;
}

.sidebar.collapsed .nav-item {
  padding: 0;
  justify-content: center;
}

.nav-item:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.nav-item.active {
  background: var(--accent-soft);
  color: var(--accent);
}

.nav-item.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 12px;
  bottom: 12px;
  width: 3px;
  border-radius: 2px;
  background: var(--accent);
}

.nav-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
  flex-shrink: 0;
}

.nav-item:not(.active) .nav-dot {
  opacity: 0.4;
}

.nav-item.active .nav-dot {
  opacity: 1;
}

.nav-label {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.nav-label-main {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 副标题：给「时装商店 / 我的衣柜」这类入口一句说明，避免三个词干巴巴 */
.nav-label-desc {
  font-size: 11px;
  font-weight: 400;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.nav-item.active .nav-label-desc {
  color: var(--accent);
  opacity: 0.75;
}

.nav-item-secondary {
  font-size: 13px;
  min-height: 40px;
}

/* ---- 底部：返回主站 ---- */
.sidebar-status {
  padding: 12px 14px;
  border-top: 1px solid var(--border-subtle);
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 50px;
}

.sidebar.collapsed .sidebar-status {
  padding: 12px 0;
  justify-content: center;
}

.back-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--text-secondary);
  text-decoration: none;
  font-family: var(--font-sans);
  font-size: 13px;
  padding: 6px 10px;
  border-radius: var(--radius-md);
  transition: background-color 150ms var(--ease-out), color 150ms var(--ease-out);
  white-space: nowrap;
}

.back-link:hover {
  background: var(--bg-hover);
  color: var(--accent);
}

.back-icon {
  font-size: 14px;
  line-height: 1;
}

.is-mobile {
  border-right: 1px solid var(--border-subtle);
}
</style>
