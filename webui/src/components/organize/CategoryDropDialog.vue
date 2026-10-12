<script setup lang="ts">
import { ref, watch } from 'vue'
import HButton from '@/components/hero/HButton.vue'
import HCheckbox from '@/components/hero/HCheckbox.vue'
import HModal from '@/components/hero/HModal.vue'
import type { CategoryDrop } from '@/api/organize'
import type { ExtraDir } from '@/api/library'
import { num } from '@/utils/format'

/**
 * 保存分类规则前：这次去掉（或改了名）的分类目录底下还有片目，问用户要不要留作「其他媒体库目录」。
 * 不留的话这些片会从海报墙上消失、网盘文件页也不能整理（片子本身不动）。
 * 结果经 done 回传：数组 = 保存并保留这些（空数组 = 保存但都不留），null = 取消保存
 */
const props = defineProps<{ drops: CategoryDrop[] | null }>()
const emit = defineEmits<{ done: [ExtraDir[] | null] }>()

const picked = ref<Record<string, boolean>>({})
watch(
  () => props.drops,
  (d) => {
    picked.value = Object.fromEntries((d || []).map((x) => [x.dir, true]))
  },
)

const TYPE_TEXT = { movie: '电影', tv: '剧集', auto: '自动' }

function finish(keep: boolean) {
  const list = keep ? (props.drops || []).filter((d) => picked.value[d.dir]).map((d) => ({ dir: d.dir, type: d.type })) : []
  emit('done', list)
}
</script>

<template>
  <HModal
    :show="!!drops"
    title="这些分类目录里还有片子"
    width="600px"
    persistent
    @update:show="(v: boolean) => !v && emit('done', null)"
  >
    <p class="intro">
      新的分类规则里没有下面这些目录了。不保留的话，里面的片子会从海报墙上消失，网盘文件页也不能再整理它们（网盘和本地文件都不会动）。
      保留后它们记在「其他媒体库目录」里：照常识别，但整理不会再往里放新片。
    </p>
    <ul class="list">
      <li v-for="d in drops || []" :key="d.dir">
        <HCheckbox :checked="picked[d.dir]" @update:checked="picked[d.dir] = $event" />
        <div class="info">
          <div class="head">
            <code>{{ d.dir }}</code>
            <span class="meta">{{ TYPE_TEXT[d.type] }} · {{ num(d.titles) }} 部</span>
          </div>
          <div v-if="d.samples?.length" class="samples">{{ d.samples.join('、') }}{{ d.titles > d.samples.length ? ' …' : '' }}</div>
        </div>
      </li>
    </ul>

    <template #footer>
      <div class="footer">
        <HButton variant="ghost" @click="emit('done', null)">取消</HButton>
        <HButton variant="tertiary" @click="finish(false)">不保留，直接保存</HButton>
        <HButton variant="primary" @click="finish(true)">保留勾选的并保存</HButton>
      </div>
    </template>
  </HModal>
</template>

<style scoped>
.intro {
  margin: 0 0 12px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--muted);
}
.list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.list li {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 8px 12px;
  border-radius: var(--r-lg);
  background: var(--surface-secondary);
}
.info {
  min-width: 0;
  flex: 1;
}
.head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 10px;
}
code {
  font-size: 12.5px;
  color: var(--foreground);
  word-break: break-all;
}
.meta,
.samples {
  font-size: 12px;
  color: var(--muted);
}
.footer {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}
</style>
