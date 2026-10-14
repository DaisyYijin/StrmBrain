<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import HAlert from '@/components/hero/HAlert.vue'
import HButton from '@/components/hero/HButton.vue'
import type { LocalUnclaimed } from '@/api/local'

/**
 * 台账里有、却不属于任何片目的视频（后端 ledger.go 的 ledgerUnclaimed）：所在目录没登记成分类目录，
 * 海报墙、刮削、对账都认不出它们。只用刮削 / 海报墙、不用自动整理的用户最常遇到 —— 默认分类规则和自己的目录名对不上。
 *
 * 「不再提示」记在本浏览器：有的目录是故意不要的（花絮、演唱会）。记的是当时的视频数，
 * 之后认不出的视频变多了再提示（新同步进来的可能又是没登记的目录）。
 * 布局页（LIBRARY-LAYOUT-PLAN.md 阶段 4）做好之后「去登记」改指那里。
 */
const props = defineProps<{ unclaimed: LocalUnclaimed; embyConfigured?: boolean }>()
const emit = defineEmits<{ reconcile: [] }>()
const router = useRouter()

const DISMISS_KEY = 'local.unclaimed.dismissed'
function readDismissed(): number {
  try {
    return Number(localStorage.getItem(DISMISS_KEY)) || 0
  } catch {
    return 0 // 隐私模式 / 禁用站点数据：照常提示
  }
}
const dismissed = ref(readDismissed())
const visible = computed(() => props.unclaimed.videos > dismissed.value)

function dismiss() {
  dismissed.value = props.unclaimed.videos
  try {
    localStorage.setItem(DISMISS_KEY, String(props.unclaimed.videos))
  } catch {
    // 存不下就只在这次生效
  }
}

/** 列几个视频最多的目录：够看出是哪里，长了就收进「等 N 个目录」 */
const SHOW = 3
const shown = computed(() => props.unclaimed.dirs.slice(0, SHOW))
const restDirs = computed(() => props.unclaimed.dirs.length - shown.value.length + (props.unclaimed.more_dirs || 0))

function goRegister() {
  void router.push({ name: 'organize', query: { tab: 'category' }, hash: '#extra-dirs' })
}
</script>

<template>
  <HAlert v-if="visible" status="default" class="unclaimed">
    台账里还有 <b>{{ unclaimed.videos }}</b> 个视频不属于任何片目：所在目录没有登记成分类目录，海报墙、刮削和对账都认不出它们。
    <span class="dirs">
      <code v-for="d in shown" :key="d.path" :title="`${d.videos} 个视频`">{{ d.path || '（本地媒体库根目录）' }}</code>
      <span v-if="restDirs > 0">等 {{ restDirs + shown.length }} 个目录</span>
    </span>
    <template #actions>
      <HButton size="sm" variant="primary" @click="goRegister">去登记目录</HButton>
      <HButton v-if="embyConfigured" size="sm" variant="tertiary" @click="emit('reconcile')">按 Emby 对账找出来</HButton>
      <HButton size="sm" variant="ghost" @click="dismiss">不再提示</HButton>
    </template>
  </HAlert>
</template>

<style scoped>
.unclaimed {
  margin: 0;
}
.dirs {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 6px;
  margin-left: 2px;
}
.dirs code {
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--foreground);
}
</style>
