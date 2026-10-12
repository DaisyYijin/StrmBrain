<script setup lang="ts">
import type { Component } from 'vue'
import { RouterLink, type RouteLocationRaw } from 'vue-router'
import { Archive, ChevronRight, Copy, FolderInput, FolderPlus, Library } from '@lucide/vue'
import HAlert from '@/components/hero/HAlert.vue'
import HButton from '@/components/hero/HButton.vue'
import SectionCard from '@/components/ui/SectionCard.vue'

/**
 * 四个工作目录一览。原来这一页只在「缺了」的时候冒一条黄色提示，
 * 配好了反而看不到它们各在哪、各管什么 —— 而它们互不包含这条约束（workspace.go）恰恰要一眼看全才好判断。
 * 目录本身在各自的页面改（转存/待整理、已存在、冗余都在自动整理；转存/待整理在影视转存里也能改），这里只负责看和跳过去；
 * 媒体库目录在本页「媒体库位置」卡里改。
 */
export interface WsSlot {
  key: 'library' | 'share' | 'existing' | 'redundant'
  /** 已配置时的显示路径（没存路径的老配置退回 cid） */
  path: string
  configured: boolean
  /** 附加媒体库（full.extra_libs）：key 也是 library，一个库一行 */
  extra?: boolean
}

/** legacy：合并时被弃用的旧待整理目录（后端 stagingdir.go 记的），用户点「知道了」之前一直提示 */
defineProps<{ slots: WsSlot[]; canCreate: boolean; creating?: boolean; legacy?: string }>()
const emit = defineEmits<{ create: []; dismissLegacy: [] }>()

const META: Record<WsSlot['key'], { label: string; desc: string; icon: Component; to?: RouteLocationRaw }> = {
  library: { label: '媒体库', desc: '整理入库的目的地，同步与洗版都以它为准', icon: Library },
  share: {
    label: '转存/待整理',
    desc: '转存、离线下载落地，手动要整理的也放这里，守望者每分钟接管',
    icon: FolderInput,
    to: { name: 'organize', query: { tab: 'basic' } },
  },
  existing: { label: '已存在', desc: '洗版判输或重复的文件搬到这里', icon: Copy, to: { name: 'organize', query: { tab: 'basic' } } },
  redundant: { label: '冗余', desc: '被新版本替换下来的旧文件', icon: Archive, to: { name: 'organize', query: { tab: 'basic' } } },
}
/** 附加媒体库那几行的文案（图标、去向同主库） */
const EXTRA_LIB = { label: '附加媒体库', desc: '同步、海报墙与主库一样管；整理目前只往主库放' }
const labelOf = (s: WsSlot) => (s.extra ? EXTRA_LIB.label : META[s.key].label)
const descOf = (s: WsSlot) => (s.extra ? EXTRA_LIB.desc : META[s.key].desc)
</script>

<template>
  <SectionCard title="工作目录" hint="这些目录必须互不包含；整理在它们之间搬运文件">
    <template v-if="canCreate" #extra>
      <HButton variant="secondary" size="sm" :loading="creating" @click="emit('create')">
        <template #icon><FolderPlus :size="15" /></template>
        一键创建缺少的
      </HButton>
    </template>

    <HAlert v-if="legacy" status="warning" title="待整理已并进转存目录" class="ws-legacy">
      原来的待整理目录（{{ legacy }}）不再自动整理。里面如果还有要整理的，请移到上面的转存/待整理目录。
      <template #actions>
        <HButton variant="tertiary" size="sm" @click="emit('dismissLegacy')">知道了</HButton>
      </template>
    </HAlert>

    <ul class="ws">
      <li v-for="(s, i) in slots" :key="`${s.key}-${i}`" class="ws-item" :class="{ 'is-missing': !s.configured }">
        <span class="ws-icon"><component :is="META[s.key].icon" :size="17" :stroke-width="1.8" /></span>
        <div class="ws-body">
          <div class="ws-head">
            <span class="ws-label">{{ labelOf(s) }}</span>
            <span v-if="!s.configured" class="ws-missing">未配置</span>
          </div>
          <div v-if="s.configured" class="ws-path" :title="s.path">{{ s.path }}</div>
          <div class="ws-desc">{{ descOf(s) }}</div>
        </div>
        <RouterLink v-if="META[s.key].to" :to="META[s.key].to!" class="ws-go" :aria-label="`去设置${labelOf(s)}目录`">
          {{ s.configured ? '修改' : '去设置' }}<ChevronRight :size="14" />
        </RouterLink>
        <span v-else class="ws-here">见「媒体库位置」</span>
      </li>
    </ul>
  </SectionCard>
</template>

<style scoped>
.ws-legacy {
  margin-bottom: 12px;
}
.ws {
  margin: -4px 0;
  padding: 0;
  list-style: none;
}
.ws-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 0;
  border-top: 1px solid var(--separator);
}
.ws-item:first-child {
  border-top: 0;
}
.ws-icon {
  width: 36px;
  height: 36px;
  flex-shrink: 0;
  border-radius: 11px;
  display: grid;
  place-items: center;
  background: var(--accent-soft);
  color: var(--accent);
}
.is-missing .ws-icon {
  background: var(--default);
  color: var(--muted);
}
.ws-body {
  flex: 1;
  min-width: 0;
}
.ws-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.ws-label {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--foreground);
}
.ws-missing {
  padding: 0 7px;
  border-radius: 999px;
  background: var(--warning-soft);
  color: var(--warning-soft-foreground);
  font-size: 11px;
  font-weight: 500;
  line-height: 18px;
}
.ws-path {
  margin-top: 2px;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--foreground);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ws-desc {
  margin-top: 2px;
  font-size: 12px;
  color: var(--muted);
}
.ws-go {
  display: inline-flex;
  align-items: center;
  flex-shrink: 0;
  font-size: 12.5px;
  color: var(--muted);
  text-decoration: none;
  transition: color 150ms ease;
}
.ws-go:hover {
  color: var(--accent);
}
.is-missing .ws-go {
  color: var(--accent);
  font-weight: 500;
}
.ws-here {
  flex-shrink: 0;
  font-size: 12px;
  color: var(--muted);
}
</style>
