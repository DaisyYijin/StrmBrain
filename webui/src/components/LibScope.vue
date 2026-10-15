<script setup lang="ts">
/**
 * 功能页顶部的「作用于」（LIBRARY-ROLES-PLAN.md §5）：库卡片管「做不做」，这一行告诉用户
 * 这一页的配置管到哪些库。只在多库、或一个本站整理的库都没有时显示（单库时是噪音）。
 * 数据与影视刮削页共用 composables/scrapeProvider.ts（GET /scrape/config 的 libs，零 115 请求）。
 */
import { computed, onMounted } from 'vue'
import { RouterLink } from 'vue-router'
import { Library } from '@lucide/vue'
import HAlert from '@/components/hero/HAlert.vue'
import { useScrapeProvider } from '@/composables/scrapeProvider'

const props = defineProps<{ kind: 'organize' | 'category' | 'wash' }>()
const { libs, load } = useScrapeProvider()
onMounted(() => void load())

const label = (l: { name: string; cid: string }) => `「${l.name || `cid ${l.cid}`}」`
const organize = computed(() => libs.value.filter((l) => l.mode !== 'self'))
const self = computed(() => libs.value.filter((l) => l.mode === 'self'))
const fallback = computed(() => organize.value.find((l) => l.default))
const none = computed(() => libs.value.length > 0 && organize.value.length === 0)
const visible = computed(() => none.value || libs.value.length > 1)

const text = computed(() => {
  if (none.value) {
    return {
      organize: '没有本站整理的库：自动整理、转存后整理、订阅都停着，转存目录里的东西不会自动整理。',
      category: '没有本站整理的库，二级分类现在不生效。',
      wash: '没有本站整理的库，洗版现在不生效。',
    }[props.kind]
  }
  const orgs = organize.value.map(label).join('、')
  const selfTail = self.value.length ? `${self.value.map(label).join('、')}是自己整理的库，不归这里管。` : ''
  switch (props.kind) {
    case 'category':
      return `这些规则把内容整理进${orgs}（没写 library 的进${fallback.value ? label(fallback.value) : '默认库'}）。${selfTail}`
    case 'wash':
      return `作用于本站整理的库${orgs}，只在同一个库里比版本。${selfTail}`
    default:
      return `整理进${orgs}。${selfTail}`
  }
})
</script>

<template>
  <HAlert v-if="visible" :status="none ? 'warning' : 'default'" class="lib-scope">
    <span class="lead"><Library :size="14" />作用于</span>{{ text }}
    <RouterLink :to="{ name: 'accounts', query: { tab: 'library' } }" class="link">
      {{ none ? '去把一个库改成本站整理 →' : '去媒体库修改 →' }}
    </RouterLink>
  </HAlert>
</template>

<style scoped>
.lib-scope {
  font-size: 13px;
}
.lead {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-right: 6px;
  font-weight: 600;
}
.link {
  margin-left: 8px;
  color: var(--accent);
  white-space: nowrap;
  text-decoration: none;
}
</style>
