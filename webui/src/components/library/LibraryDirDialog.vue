<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import HAlert from '@/components/hero/HAlert.vue'
import HButton from '@/components/hero/HButton.vue'
import HInput from '@/components/hero/HInput.vue'
import HModal from '@/components/hero/HModal.vue'
import HMultiSelect, { type MultiGroup, type MultiOption } from '@/components/hero/HMultiSelect.vue'
import HSegmented from '@/components/hero/HSegmented.vue'
import HSwitch from '@/components/hero/HSwitch.vue'
import FieldRow from '@/components/ui/FieldRow.vue'
import Cid115Input, { type CidValue } from '@/components/Cid115Input.vue'
import type { LibDirLib, LibDirType, LibraryDir } from '@/api/library'
import { CATEGORY_PRESETS, COUNTRIES, LANGUAGES, MOVIE_GENRES, TV_GENRES, type CategoryPreset } from '@/pages/organize/categoryRef'
import { emptyLibDir } from './libDirs'

/**
 * 编辑媒体库目录表的一行。目录用 115 选择器选（也可以直接填路径：要整理进去的新目录可以还不存在，整理时自动建），
 * 按它落在哪个媒体库下面换成「库 + 库内路径」；不在任何媒体库下的不收。本地 STRM 目录跟着 115 结构，只显示。
 */
const props = defineProps<{ show: boolean; row: LibraryDir | null; libs: LibDirLib[]; localRoot?: string }>()
const emit = defineEmits<{ 'update:show': [boolean]; save: [LibraryDir] }>()

const form = ref<LibraryDir>(emptyLibDir())
const target = ref<CidValue>({ cid: '', path: '' })

const trim = (p: string) => p.replace(/\/+$/, '')
watch(
  () => props.show,
  (v) => {
    if (!v) return
    const main = props.libs.find((l) => l.main) ?? props.libs[0]
    form.value = props.row ? { ...props.row } : emptyLibDir(main?.cid ?? '', main?.name ?? '')
    const lib = props.libs.find((l) => l.cid === form.value.lib_cid)
    const abs = lib ? [trim(lib.abs), form.value.path].filter(Boolean).join('/') : form.value.path
    target.value = { cid: form.value.cid ?? '', path: abs }
  },
)

/** 选择器里的 115 绝对路径 → 落在哪个库（取最长的库路径前缀）、库内路径 */
const located = computed(() => {
  const p = trim(target.value.path.trim())
  let best: LibDirLib | null = null
  for (const l of props.libs) {
    const a = trim(l.abs)
    if (a && (p === a || p.startsWith(`${a}/`)) && (!best || a.length > trim(best.abs).length)) best = l
  }
  if (!best) return null
  return { lib: best, rel: p.slice(trim(best.abs).length).replace(/^\/+/, '') }
})
// 老行的库不在配置里（迁移来的）：选择器认不出，保留原值
const keepsMissingLib = computed(() => !!props.row && !props.libs.some((l) => l.cid === props.row!.lib_cid))
const pathError = computed(() => {
  if (!target.value.path.trim()) return '选一个 115 目录'
  if (!located.value && !keepsMissingLib.value) return '这个目录不在任何已配置的 115 媒体库里（先到上面「媒体库位置」添加附加媒体库）'
  return ''
})
const localPath = computed(() => {
  const l = located.value
  if (!l || !props.localRoot) return ''
  return [trim(props.localRoot), l.lib.name, l.rel].filter(Boolean).join('/')
})

const TYPE_OPTIONS: { label: string; value: LibDirType }[] = [
  { label: '电影', value: 'movie' },
  { label: '剧集', value: 'tv' },
  { label: '电影和剧集', value: 'all' },
]

// ---- 条件：逗号串 ⇄ 多选 ----
const split = (s: string) => s.split(',').map((t) => t.trim()).filter(Boolean)
function csv(key: 'genre_ids' | 'origin_country' | 'production_countries' | 'original_language') {
  return computed<string[]>({
    get: () => split(form.value[key]),
    set: (v) => (form.value[key] = v.join(',')),
  })
}
const genres = csv('genre_ids')
const countries = csv('origin_country')
const prodCountries = csv('production_countries')
const langs = csv('original_language')

const genreOptions = computed<MultiOption<string>[]>(() =>
  (form.value.media_type === 'tv' ? TV_GENRES : MOVIE_GENRES).map(([v, l]) => ({ label: `${l}（${v}）`, value: v })),
)
const countryOptions: MultiGroup<string>[] = COUNTRIES.map((c) => ({
  type: 'group' as const,
  key: c.group,
  label: c.group,
  children: c.items.map(([v, l]) => ({ label: `${l}（${v}）`, value: v })),
}))
const langOptions: MultiOption<string>[] = LANGUAGES.map(([v, l]) => ({ label: `${l}（${v}）`, value: v }))

const presets = computed(() =>
  CATEGORY_PRESETS.filter((p) => form.value.media_type === 'all' || p.media === form.value.media_type),
)
/** 配方只填条件；目录还没选时顺手按配方的名字在当前库下填一个 */
function applyPreset(p: CategoryPreset) {
  form.value.genre_ids = (p.genres ?? []).join(',')
  form.value.origin_country = (p.countries ?? []).join(',')
  form.value.production_countries = ''
  form.value.original_language = (p.langs ?? []).join(',')
  form.value.release_year = ''
  form.value.custom_regex = p.regex ?? ''
  if (!target.value.path.trim()) {
    const lib = props.libs.find((l) => l.cid === form.value.lib_cid) ?? props.libs[0]
    if (lib) target.value = { cid: '', path: `${trim(lib.abs)}/${p.name}` }
  }
}

const hasCond = computed(() =>
  [form.value.genre_ids, form.value.origin_country, form.value.production_countries, form.value.original_language, form.value.release_year, form.value.custom_regex].some((v) => v.trim()),
)

function submit() {
  if (pathError.value) return
  const r = { ...form.value }
  const l = located.value
  if (l) {
    r.lib_cid = l.lib.cid
    r.lib_name = l.lib.name
    r.path = l.rel
  }
  r.cid = target.value.cid || ''
  if (!r.organize) r.fallback = false
  emit('save', r)
  emit('update:show', false)
}
</script>

<template>
  <HModal :show="show" :title="row ? '修改目录' : '添加目录'" width="760px" persistent @update:show="emit('update:show', $event)">
    <div class="form">
      <FieldRow label="115 目录" required :error="pathError" wide
        tip="在已配置的 115 媒体库里选；要整理进去的新目录可以直接填路径，整理时自动创建">
        <Cid115Input v-model="target" placeholder="选择或填写 115 目录，如 /影视/电影/华语电影" />
      </FieldRow>
      <p v-if="located" class="where">
        媒体库「{{ located.lib.name }}」{{ located.rel ? ` / ${located.rel}` : '的库根本身' }}
        <template v-if="localPath"> · 本地 STRM 目录 <code>{{ localPath }}</code></template>
      </p>
      <HAlert v-if="located && !located.rel" status="warning">
        选的是媒体库根本身：库根下每个含视频的子目录都会当成一部片。
      </HAlert>

      <FieldRow label="类型">
        <HSegmented v-model="form.media_type" size="sm" :options="TYPE_OPTIONS" />
      </FieldRow>
      <FieldRow label="参与整理" tip="关掉 = 仅识别：海报墙、刮削认得里面的片，但整理不会往这里放新片（自己手动放片的目录用这个）">
        <HSwitch v-model="form.organize" />
      </FieldRow>

      <template v-if="form.organize">
        <FieldRow label="兜底" tip="条件都没匹配上时，按顺序第一个兜底目录接住。每种类型至少要有一个">
          <HSwitch v-model="form.fallback" />
        </FieldRow>
        <FieldRow label="常见配方" tip="点一下填好条件">
          <div class="presets">
            <HButton v-for="p in presets" :key="`${p.media}${p.label}`" variant="tertiary" size="sm" @click="applyPreset(p)">{{ p.label }}</HButton>
          </div>
        </FieldRow>
        <FieldRow label="影视类型" tip="TMDB genre_ids，多选之间是「或」；可直接输入数字 ID，前面加 ! 表示排除" wide>
          <HMultiSelect v-model="genres" creatable :options="genreOptions" placeholder="不限" />
        </FieldRow>
        <FieldRow label="国家 / 地区" tip="origin_country，多选之间是「或」；前面加 ! 表示排除" wide>
          <HMultiSelect v-model="countries" creatable :options="countryOptions" placeholder="不限" />
        </FieldRow>
        <FieldRow label="制片国家" tip="production_countries；老电影常缺 origin_country，按国家分电影时用它更稳" wide>
          <HMultiSelect v-model="prodCountries" creatable :options="countryOptions" placeholder="不限" />
        </FieldRow>
        <FieldRow label="语言" tip="original_language，多选之间是「或」" wide>
          <HMultiSelect v-model="langs" creatable :options="langOptions" placeholder="不限" />
        </FieldRow>
        <FieldRow label="发行年份" tip="剧集取首播年份；可写范围 2000-2009，多个用逗号分隔" wide>
          <HInput v-model="form.release_year" placeholder="不限" />
        </FieldRow>
        <FieldRow label="片名正则" tip="命中片名或原名即归这里，不要求其他条件同时成立" wide>
          <HInput v-model="form.custom_regex" placeholder="不限" />
        </FieldRow>
        <HAlert v-if="form.unsupported" status="warning">
          这一行带着认不出的条件「{{ form.unsupported }}」（从老规则迁来的），永远不会匹配。不需要的话删掉这一行重建。
        </HAlert>
        <p v-if="!hasCond && !form.fallback" class="hint">没写条件：这一行会接住所有{{ form.media_type === 'tv' ? '剧集' : form.media_type === 'movie' ? '电影' : '电影和剧集' }}，排在它后面的同类型目录永远轮不到。</p>
      </template>

      <FieldRow label="备注" wide>
        <HInput v-model="form.note" placeholder="选填" />
      </FieldRow>
    </div>
    <template #footer>
      <HButton variant="tertiary" @click="emit('update:show', false)">取消</HButton>
      <HButton variant="primary" :disabled="!!pathError" @click="submit">确定</HButton>
    </template>
  </HModal>
</template>

<style scoped>
.form {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.where {
  margin: -2px 0 6px;
  font-size: 12.5px;
  color: var(--muted);
}
.where code {
  font-size: 12px;
  color: var(--foreground);
}
.presets {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.hint {
  margin: 4px 0 0;
  font-size: 12.5px;
  color: var(--warning);
}
</style>
