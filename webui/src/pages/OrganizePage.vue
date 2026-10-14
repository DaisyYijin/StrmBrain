<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import HAlert from '@/components/hero/HAlert.vue'
import HButton from '@/components/hero/HButton.vue'
import HSpinner from '@/components/hero/HSpinner.vue'
import HTabs from '@/components/hero/HTabs.vue'
import BasicTab from './organize/BasicTab.vue'
import ScrapeSummary from './organize/ScrapeSummary.vue'
import RecognizeTab from './organize/RecognizeTab.vue'
import AiTab from './organize/AiTab.vue'
import RenameTab from './organize/RenameTab.vue'
import YamlRuleTab from './organize/YamlRuleTab.vue'
import { libraryApi, organizeApi } from '@/api'
import { DEFAULT_CATEGORY_YAML } from './organize/defaultRules'
import ExtraDirsCard from '@/components/organize/ExtraDirsCard.vue'
import CategoryDropDialog from '@/components/organize/CategoryDropDialog.vue'
import type { CategoryDrop } from '@/api/organize'
import type { ExtraDir } from '@/api/library'
import { useTabQuery } from '@/composables/useTabQuery'

const tab = useTabQuery('basic')
const router = useRouter()

// 媒体库目录表启用后（docs/115-station-notes/LIBRARY-DIRS-PLAN.md），分类规则与其他媒体库目录都并进了
// 「账号与媒体库 → 媒体库目录」，这个页签只留一句说明 + 跳转（保留页签：收藏的 ?tab=category 不 404）。
// 没启用（还没配媒体库、115 暂时读不出库名）时照旧显示老的编辑器
const dirsActive = ref<boolean | null>(null)
const dirsReason = ref('')
watch(
  tab,
  async (v) => {
    if (v !== 'category' || dirsActive.value === true) return
    try {
      const d = await libraryApi.getLibraryDirs()
      dirsActive.value = d.active
      dirsReason.value = d.reason ?? ''
    } catch {
      dirsActive.value = false
    }
  },
  { immediate: true },
)
function goDirs() {
  void router.push({ name: 'accounts', hash: '#library-dirs' })
}
// 整理记录已搬到任务中心（/tasks?tab=records）：这一页只剩配置。
// 旧地址 /organize?tab=records 由路由重定向过去，见 router/index.ts

// ---- 保存分类规则：去掉的分类目录里还有片子时，先问要不要留作其他媒体库目录 ----
const drops = ref<CategoryDrop[] | null>(null)
let answer: ((v: ExtraDir[] | null) => void) | null = null
// 其他媒体库目录被这里改过：卡片重读
const extraVer = ref(0)

function askKeep(list: CategoryDrop[]) {
  return new Promise<ExtraDir[] | null>((resolve) => {
    answer = resolve
    drops.value = list
  })
}
function onDropDone(v: ExtraDir[] | null) {
  drops.value = null
  answer?.(v)
  answer = null
}

async function persistCategories(yaml: string) {
  const { dropped } = await organizeApi.previewCategories(yaml)
  let keep: ExtraDir[] = []
  if (dropped?.length) {
    const v = await askKeep(dropped)
    if (v === null) return false
    keep = v
  }
  await organizeApi.saveCategories(yaml, keep)
  if (keep.length) extraVer.value++
}

const tabs = computed(() => [
  { value: 'basic', label: '基础配置' },
  { value: 'scrape', label: '影视刮削' },
  { value: 'recognize', label: '识别规则' },
  { value: 'ai', label: 'AI 增强识别' },
  { value: 'rename', label: '重命名策略' },
  { value: 'category', label: '二级分类策略' },
  { value: 'wash', label: '洗版策略' },
])
</script>

<template>
  <div class="page">
    <HTabs v-model="tab" :items="tabs" />

    <!-- 页签内容按需挂载：没选中的页签不渲染、不发请求（与原 NTabPane 的 display-directive="if" 一致） -->
    <div class="panel">
      <BasicTab v-if="tab === 'basic'" />
      <!-- 刮削配置已搬到「影视刮削」页，这里只读，改要跳过去 -->
      <ScrapeSummary v-else-if="tab === 'scrape'" />
      <RecognizeTab v-else-if="tab === 'recognize'" />
      <AiTab v-else-if="tab === 'ai'" />
      <RenameTab v-else-if="tab === 'rename'" />
      <div v-else-if="tab === 'category' && dirsActive === null" class="loading"><HSpinner size="sm" /> 读取中…</div>
      <HAlert v-else-if="tab === 'category' && dirsActive" status="default" title="二级分类已并进「媒体库目录」">
        分类规则和其他媒体库目录合成了一张表：每个目录写清适用的类型、地区等条件，从上往下的顺序就是优先级。
        原来的规则已经原样迁过去。
        <template #actions><HButton size="sm" variant="primary" @click="goDirs">去「账号与媒体库 → 媒体库目录」</HButton></template>
      </HAlert>
      <template v-else-if="tab === 'category'">
        <HAlert v-if="dirsReason" status="warning" class="extra">媒体库目录表还没启用：{{ dirsReason }}。暂时继续在这里编辑分类规则。</HAlert>
        <YamlRuleTab
          key="category"
          kind="category"
          title="二级分类策略"
          hint="分类名即 115 目录名"
          note="分类名即 115 目录名，整理时目录不存在会自动创建。规则从上往下匹配，先命中的先停；不想手写 YAML 就用右上角的「添加分类规则」。"
          :load="organizeApi.getCategories"
          :persist="persistCategories"
          :fallback="DEFAULT_CATEGORY_YAML"
        />
        <ExtraDirsCard :ver="extraVer" class="extra" />
      </template>
      <YamlRuleTab
        v-else-if="tab === 'wash'"
        key="wash"
        kind="wash"
        title="洗版策略"
        hint="同一影视多版本时的取舍规则"
        note="按优先级字段决定保留哪个版本。mode 决定共存 / 跳过 / 替换，scope 决定是全局只留一个还是按分辨率分组各留一个；字段含义见右上角「规则说明」。"
        :load="organizeApi.getWash"
        :persist="organizeApi.saveWash"
      />
    </div>
    <CategoryDropDialog :drops="drops" @done="onDropDone" />
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}
.panel {
  min-width: 0;
}
.extra {
  margin-top: 16px;
}
</style>
