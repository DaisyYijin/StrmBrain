<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import HAlert from '@/components/hero/HAlert.vue'
import HButton from '@/components/hero/HButton.vue'
import HModal from '@/components/hero/HModal.vue'
import HSpinner from '@/components/hero/HSpinner.vue'
import { libraryApi } from '@/api'
import type { LayoutChange, LayoutPreview } from '@/api/library'
import { toastError, useFeedback } from '@/composables/useFeedback'
import { layoutDirLabel } from './layout'

/**
 * 改媒体库布局之前先看后果（后端 previewLayout，LIBRARY-LAYOUT-PLAN.md §3.4），确认了才写。
 * 布局页的手动增删、采纳建议、对账里的「加入」都走这一个弹窗，别另开一条直接写的路。
 *
 * 要用户看清的三件事：新认出多少片目、其中多少没有任何佐证（多半是花絮、空目录，登记后会变成「片目」）、
 * 已有片目的 key 会不会变（按片目记的补刮记账、背景图缺口会作废，会重刮一次）。
 */
const props = defineProps<{ show: boolean; change: LayoutChange | null }>()
const emit = defineEmits<{ 'update:show': [boolean]; done: [] }>()
const { message } = useFeedback()

const loading = ref(false)
const failed = ref('')
const preview = ref<LayoutPreview | null>(null)
const skipped = ref<string[]>([])
const saving = ref(false)

watch(
  () => [props.show, props.change] as const,
  async ([show, change]) => {
    if (!show || !change) return
    loading.value = true
    failed.value = ''
    preview.value = null
    skipped.value = []
    try {
      const d = await libraryApi.previewLayout(change)
      preview.value = d.preview
      skipped.value = d.skipped ?? []
    } catch (e) {
      failed.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  },
  { immediate: true },
)

const adds = computed(() => props.change?.add ?? [])
const removes = computed(() => props.change?.remove ?? [])
const risky = computed(() => !!preview.value && (preview.value.changed > 0 || preview.value.lost > 0 || preview.value.uncorroborated_n > 0))

async function confirm() {
  if (!props.change) return
  saving.value = true
  try {
    const r = await libraryApi.adoptLayout(props.change)
    if (r.skipped?.length) message.warning(r.skipped.join('；'))
    else message.success('已保存，海报墙按新布局重新识别')
    emit('update:show', false)
    emit('done')
  } catch (e) {
    toastError(e, '保存失败')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <HModal :show="show" title="确认媒体库布局改动" width="620px" @update:show="emit('update:show', $event)">
    <div class="lc">
      <ul class="changes">
        <li v-for="d in adds" :key="`a${d.lib}/${d.dir}`">
          <span class="op add">登记</span><code>{{ layoutDirLabel(d.lib ?? '', d.dir) }}</code>
          <span class="muted">（{{ d.type === 'tv' ? '剧集' : d.type === 'movie' ? '电影' : '自动' }}）</span>
        </li>
        <li v-for="d in removes" :key="`r${d.lib}/${d.dir}`">
          <span class="op rm">移除</span><code>{{ layoutDirLabel(d.lib ?? '', d.dir) }}</code>
        </li>
      </ul>

      <div v-if="loading" class="loading"><HSpinner size="sm" />正在按新布局把台账认一遍…</div>
      <HAlert v-else-if="failed" status="danger">{{ failed }}</HAlert>
      <template v-else-if="preview">
        <HAlert v-if="skipped.length" status="warning">{{ skipped.join('；') }}</HAlert>
        <dl class="facts">
          <div>
            <dt>新认出的片目</dt>
            <dd>
              <b>{{ preview.new_titles }}</b> 部<template v-if="preview.new_titles">，其中 {{ preview.corroborated }} 部有 Emby 条目或 NFO 对得上</template>
            </dd>
          </div>
          <div v-if="preview.new_samples?.length" class="samples">
            <code v-for="k in preview.new_samples" :key="k">{{ k }}</code>
            <span v-if="preview.new_titles > preview.new_samples.length" class="muted">…</span>
          </div>
          <div v-if="preview.uncorroborated_n" class="warn">
            <dt>没有任何佐证的</dt>
            <dd>
              {{ preview.uncorroborated_n }} 个：Emby 里没有、目录里也没有 NFO，多半是花絮、空目录或杂物。登记后它们也会成为「片目」，出现在海报墙上。
            </dd>
          </div>
          <div v-if="preview.uncorroborated?.length" class="samples">
            <code v-for="k in preview.uncorroborated" :key="k">{{ k }}</code>
          </div>
          <div v-if="preview.changed" class="warn">
            <dt>已有片目会换 key</dt>
            <dd>
              {{ preview.changed }} 部：新分类比现有的更深，原来认成一部的目录会拆开。按片目记的补刮记账、背景图缺口随之作废，媒体信息补全会把它们再刮一次。
            </dd>
          </div>
          <div v-if="preview.changed_samples?.length" class="samples">
            <code v-for="k in preview.changed_samples" :key="k">{{ k }}</code>
          </div>
          <div v-if="preview.lost" class="warn">
            <dt>不再认得的片目</dt>
            <dd>{{ preview.lost }} 部会从海报墙上消失（文件与台账不动）。</dd>
          </div>
          <div v-if="preview.lost_samples?.length" class="samples">
            <code v-for="k in preview.lost_samples" :key="k">{{ k }}</code>
          </div>
          <div v-if="preview.queued_scrapes && (preview.changed || preview.lost)">
            <dt>排着的刮削任务</dt>
            <dd>{{ preview.queued_scrapes }} 个：里面点名的片目若换了 key，会报「台账里已经没有这个片目」。</dd>
          </div>
        </dl>
        <p v-if="!risky && preview.new_titles" class="ok">没有发现风险：新认出的片目都有佐证，已有片目不受影响。</p>
        <p class="muted note">只影响「哪些算片目」：整理不读这份登记，不会把新片放进这些目录；网盘与本地文件都不动。</p>
      </template>
    </div>
    <template #footer>
      <HButton variant="tertiary" @click="emit('update:show', false)">取消</HButton>
      <HButton variant="primary" :loading="saving" :disabled="loading || !!failed" @click="confirm">确认保存</HButton>
    </template>
  </HModal>
</template>

<style scoped>
.lc {
  display: flex;
  flex-direction: column;
  gap: 12px;
  font-size: 13px;
}
.changes {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.op {
  display: inline-block;
  margin-right: 6px;
  padding: 0 6px;
  border-radius: var(--r-sm);
  font-size: 12px;
}
.op.add {
  color: var(--success);
  background: color-mix(in oklab, var(--success) 12%, transparent);
}
.op.rm {
  color: var(--danger);
  background: color-mix(in oklab, var(--danger) 12%, transparent);
}
.loading {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--muted);
}
.facts {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.facts dt {
  display: inline;
  font-weight: 600;
}
.facts dt::after {
  content: '：';
}
.facts dd {
  display: inline;
  margin: 0;
}
.facts .warn dt {
  color: var(--warning);
}
.samples {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 8px;
  padding-left: 12px;
}
.samples code {
  font-size: 12px;
  word-break: break-all;
}
.muted {
  color: var(--muted);
}
.ok {
  margin: 0;
  color: var(--success);
}
.note {
  margin: 0;
  font-size: 12px;
}
</style>
