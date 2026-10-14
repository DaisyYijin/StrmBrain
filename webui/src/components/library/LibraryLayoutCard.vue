<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Plus, RefreshCw, Sparkles, Trash2 } from '@lucide/vue'
import HAlert from '@/components/hero/HAlert.vue'
import HButton from '@/components/hero/HButton.vue'
import HChip from '@/components/hero/HChip.vue'
import HInput from '@/components/hero/HInput.vue'
import HSelect from '@/components/hero/HSelect.vue'
import HSpinner from '@/components/hero/HSpinner.vue'
import SectionCard from '@/components/ui/SectionCard.vue'
import LayoutChangeDialog from './LayoutChangeDialog.vue'
import { libraryApi } from '@/api'
import type { ExtraDirType, LayoutChange, LayoutOverview, LayoutRow, LayoutSuggestReply, LayoutSuggestion } from '@/api/library'
import { toastError, useFeedback } from '@/composables/useFeedback'
import { LAYOUT_SOURCE_LABEL, LAYOUT_TYPE_LABEL, layoutDirLabel } from './layout'

/**
 * 媒体库布局（后端 liblayout*.go，设计见 docs/115-station-notes/LIBRARY-LAYOUT-PLAN.md）。
 *
 * 海报墙、刮削、对账、网盘文件页都按这张表认片目：登记过的分类目录，下一层是片目；
 * 电影分类目录里直接放的视频一个算一部。分类规则来的只读（改规则去自动整理），
 * 其余（手动、从 Emby 建议采纳的）在这里增删 —— 这是唯一的写入口，自动整理页的卡片只是摘要，
 * 两处都能改就是两份整存整取、必然互相覆盖。任何改动都先过 LayoutChangeDialog 的预览。
 */
const { dialog } = useFeedback()
const route = useRoute()
const router = useRouter()

const loading = ref(true)
const data = ref<LayoutOverview | null>(null)

async function load(refresh = false) {
  loading.value = true
  try {
    data.value = await libraryApi.getLayout(refresh)
  } catch (e) {
    toastError(e, '读取媒体库布局失败')
  } finally {
    loading.value = false
  }
}

const rows = computed(() => data.value?.rows ?? [])
/** 台账里一部片都没有的规则目录：多数是默认分类，折起来 */
const idleRules = computed(() => rows.value.filter((r) => r.hint === 'empty'))
const activeRows = computed(() => rows.value.filter((r) => r.hint !== 'empty'))
const showIdle = ref(false)

function rowLabel(r: LayoutRow) {
  return r.lib ? layoutDirLabel(r.lib, r.dir) : r.dir
}

// ---- 改动：一律经确认弹窗 ----
const change = ref<LayoutChange | null>(null)
const showChange = ref(false)
function openChange(c: LayoutChange) {
  change.value = c
  showChange.value = true
}
async function onDone() {
  await load()
  if (suggest.value) await loadSuggest(true)
}

const TYPE_OPTIONS: { label: string; value: ExtraDirType }[] = [
  { label: '电影', value: 'movie' },
  { label: '剧集', value: 'tv' },
  { label: '自动（有集号算剧集）', value: 'auto' },
]

function setType(r: LayoutRow, type: ExtraDirType) {
  if (type === r.type) return
  const old = { lib: r.lib, dir: r.dir, type: r.type }
  openChange({ remove: [old], add: [{ ...old, type }], source: r.source === 'rule' ? 'manual' : r.source })
}

function removeRow(r: LayoutRow) {
  openChange({ remove: [{ lib: r.lib, dir: r.dir, type: r.type }] })
}

// ---- 手动登记 ----
const formEl = ref<HTMLElement | null>(null)
const form = ref<{ lib: string; dir: string; type: ExtraDirType }>({ lib: '', dir: '', type: 'movie' })
const libOptions = computed(() => [
  { label: '所有库', value: '' },
  ...(data.value?.libs ?? []).map((l) => ({ label: `只对「${l}」`, value: l })),
])
const formError = computed(() => {
  if (!form.value.lib && !form.value.dir.trim()) return '不限定库时要写目录；要登记库根本身，先选一个库'
  return ''
})
function submitForm() {
  if (formError.value) return
  openChange({ add: [{ lib: form.value.lib, dir: form.value.dir.trim(), type: form.value.type }], source: 'manual' })
}

/** 认不出的目录 → 填进登记表（第一段是库名），让用户确认是哪一层再提交 */
async function fillFrom(p: string) {
  const [lib, ...rest] = p.split('/')
  form.value = { lib: lib ?? '', dir: rest.join('/'), type: form.value.type }
  await nextTick()
  formEl.value?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

// ---- Emby 建议 ----
const suggest = ref<LayoutSuggestReply | null>(null)
const suggesting = ref(false)
async function loadSuggest(refresh = false) {
  suggesting.value = true
  try {
    suggest.value = await libraryApi.getLayoutSuggest(refresh)
  } catch (e) {
    toastError(e, '读取 Emby 建议失败')
  } finally {
    suggesting.value = false
  }
}
const sres = computed(() => suggest.value?.result)

function adoptSuggestion(s: LayoutSuggestion) {
  openChange({ add: [{ lib: s.lib, dir: s.dir, type: s.type }], source: 'emby' })
}
async function dismissSuggestion(s: LayoutSuggestion) {
  const ok = await dialog.confirm({
    title: '忽略这条建议？',
    content: `「${layoutDirLabel(s.lib, s.dir)}」以后不再建议。要登记的话仍可在上面手动添加。`,
    actions: [
      { label: '取消', value: false, variant: 'tertiary' },
      { label: '忽略', value: true, variant: 'primary' },
    ],
  })
  if (!ok) return
  try {
    await libraryApi.dismissLayout(s.lib, s.dir)
    await loadSuggest(true)
  } catch (e) {
    toastError(e, '忽略失败')
  }
}

function goRules() {
  void router.push({ name: 'organize', query: { tab: 'category' } })
}

onMounted(async () => {
  await load()
  if (route.hash === '#library-layout') {
    await nextTick()
    document.getElementById('library-layout')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
})
</script>

<template>
  <SectionCard id="library-layout" title="媒体库布局" hint="哪些目录是分类目录：片目在它的下一层">
    <template #extra>
      <HButton variant="ghost" size="sm" icon-only aria-label="刷新" title="刷新" :loading="loading" @click="load(true)">
        <RefreshCw :size="15" />
      </HButton>
    </template>
    <p class="lead">
      海报墙、刮削、对账和网盘文件页都按这张表认片目。分类规则里的目录自动在列（改规则去「自动整理 → 二级分类策略」）；
      自己建的目录在这里登记，<b>只用于识别</b>，整理不会往里放新片。电影目录里直接放的视频一个算一部。
    </p>

    <div v-if="loading && !data" class="muted"><HSpinner size="sm" /> 读取中…</div>
    <template v-else-if="data">
      <p class="summary">
        认出 <b>{{ data.titles }}</b> 部片目
        <template v-if="data.unclaimed.videos">，另有 <b class="warn">{{ data.unclaimed.videos }}</b> 个视频不属于任何片目</template>
      </p>

      <!-- 认不出的目录：点一下填进登记表 -->
      <div v-if="data.unclaimed.dirs.length" class="unclaimed">
        <span class="muted">认不出的视频在：</span>
        <button v-for="d in data.unclaimed.dirs" :key="d.path" type="button" class="udir" :disabled="!d.path.includes('/')"
          :title="d.path.includes('/') ? '填进下面的登记表' : '本地媒体库根目录下直接放的视频：不属于任何库，登记不了'" @click="fillFrom(d.path)">
          {{ d.path || '（本地根目录）' }} <span class="muted">{{ d.videos }}</span>
        </button>
        <span v-if="data.unclaimed.more_dirs" class="muted">等另外 {{ data.unclaimed.more_dirs }} 个目录</span>
      </div>

      <ul class="rows">
        <li v-for="r in activeRows" :key="`${r.lib}/${r.dir}`" class="row">
          <code class="dir" :title="rowLabel(r)">{{ rowLabel(r) }}</code>
          <HChip size="sm" :color="r.source === 'rule' ? 'default' : 'accent'">{{ LAYOUT_SOURCE_LABEL[r.source] ?? r.source }}</HChip>
          <span v-if="!r.lib && r.source !== 'rule'" class="muted scope">所有库</span>
          <span class="cnt">{{ r.titles }} 部</span>
          <template v-if="r.source === 'rule'">
            <span class="type muted">{{ LAYOUT_TYPE_LABEL[r.type] }}</span>
            <HButton variant="ghost" size="sm" @click="goRules">去改规则</HButton>
          </template>
          <template v-else>
            <HSelect :model-value="r.type" :options="TYPE_OPTIONS" aria-label="类型" class="type-sel" @update:model-value="setType(r, $event as ExtraDirType)" />
            <HButton variant="ghost" size="sm" icon-only aria-label="移除" @click="removeRow(r)"><Trash2 :size="15" /></HButton>
          </template>
          <p v-if="r.hint === 'never_organized'" class="hint">
            按分类规则认的片目，但从没有整理进来过（片是自己放的）：若这个目录的结构和「分类 / 片目」不一样，请调整分类规则或改用手动登记。
          </p>
        </li>
      </ul>
      <button v-if="idleRules.length" type="button" class="idle-toggle" @click="showIdle = !showIdle">
        {{ showIdle ? '收起' : '还有' }} {{ idleRules.length }} 个分类规则目录台账里没有片{{ showIdle ? '' : '（多半是默认分类）' }}
      </button>
      <ul v-if="showIdle" class="rows idle">
        <li v-for="r in idleRules" :key="`${r.lib}/${r.dir}`" class="row">
          <code class="dir">{{ rowLabel(r) }}</code>
          <span class="type muted">{{ LAYOUT_TYPE_LABEL[r.type] }}</span>
        </li>
      </ul>

      <!-- 手动登记 -->
      <div ref="formEl" class="form">
        <HSelect v-model="form.lib" :options="libOptions" aria-label="作用范围" class="lib-sel" />
        <HInput v-model="form.dir" mono :placeholder="form.lib ? '库内路径，留空 = 库根本身' : '库内路径，如 纪录片 或 LGNB电影/港片'" class="form-dir" />
        <HSelect v-model="form.type" :options="TYPE_OPTIONS" aria-label="类型" class="type-sel" />
        <HButton variant="secondary" size="sm" :disabled="!!formError" :title="formError" @click="submitForm"><Plus :size="14" />预览并登记</HButton>
      </div>

      <!-- Emby 建议 -->
      <div class="suggest">
        <div class="suggest-head">
          <b>按 Emby 找建议</b>
          <span class="muted">按 Emby 里电影 / 剧集的实际路径推出该登记哪些目录，看过预览再采纳。</span>
          <HButton size="sm" variant="tertiary" :loading="suggesting" @click="loadSuggest(!!suggest)">
            <Sparkles :size="14" />{{ suggest ? '重新查' : '查一下' }}
          </HButton>
        </div>
        <template v-if="suggest">
          <p v-if="!suggest.configured" class="muted">没有配置 Emby。</p>
          <HAlert v-else-if="sres && !sres.mapping_ok" status="warning">
            Emby 条目换到本地一个都对不上台账：多半是「系统配置 → Emby」里的路径映射不对，先改好映射再来。
          </HAlert>
          <template v-else-if="sres">
            <p v-if="!sres.suggestions.length && !sres.unsure.length" class="muted">
              没有可建议的：Emby 里的片目都已经认得了{{ sres.dismissed ? `（另有 ${sres.dismissed} 条被忽略的建议）` : '' }}。
            </p>
            <ul class="rows">
              <li v-for="s in sres.suggestions" :key="`${s.lib}/${s.dir}`" class="row">
                <code class="dir">{{ layoutDirLabel(s.lib, s.dir) }}</code>
                <span class="type muted">{{ LAYOUT_TYPE_LABEL[s.type] }}</span>
                <span class="cnt">{{ s.titles }} 部</span>
                <HButton size="sm" variant="primary" @click="adoptSuggestion(s)">预览并采纳</HButton>
                <HButton size="sm" variant="ghost" @click="dismissSuggestion(s)">忽略</HButton>
                <p class="hint muted">例如：{{ s.samples.join('、') }}</p>
              </li>
            </ul>
            <template v-if="sres.unsure.length">
              <p class="muted">下面这些认不准，没有给建议，请自己判断后手动登记：</p>
              <ul class="rows">
                <li v-for="u in sres.unsure" :key="`u${u.lib}/${u.dir}`" class="row">
                  <code class="dir">{{ layoutDirLabel(u.lib, u.dir) }}</code>
                  <span class="muted">{{ u.reason }}</span>
                  <p v-if="u.samples.length" class="hint muted">例如：{{ u.samples.join('、') }}</p>
                </li>
              </ul>
            </template>
            <p v-if="sres.dismissed && (sres.suggestions.length || sres.unsure.length)" class="muted">另有 {{ sres.dismissed }} 条被忽略的建议没有列出。</p>
          </template>
        </template>
      </div>
    </template>

    <LayoutChangeDialog v-model:show="showChange" :change="change" @done="onDone" />
  </SectionCard>
</template>

<style scoped>
.lead {
  margin: 0 0 12px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--muted);
}
.summary {
  margin: 0 0 8px;
  font-size: 13px;
}
.warn {
  color: var(--warning);
}
.muted {
  color: var(--muted);
}
.unclaimed {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-bottom: 12px;
  font-size: 12.5px;
}
.udir {
  padding: 2px 8px;
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  background: transparent;
  color: var(--foreground);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.udir:disabled {
  cursor: default;
  opacity: 0.6;
}
.udir:not(:disabled):hover {
  border-color: var(--accent);
}
.rows {
  margin: 0 0 8px;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
}
.row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 10px;
  padding: 6px 0;
  border-bottom: 1px solid var(--separator);
  font-size: 13px;
}
.row:last-child {
  border-bottom: none;
}
.dir {
  flex: 1;
  min-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12.5px;
}
.cnt {
  min-width: 48px;
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.scope,
.type {
  font-size: 12px;
}
.type-sel {
  width: 180px;
}
.hint {
  flex-basis: 100%;
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--warning);
}
.hint.muted {
  color: var(--muted);
}
.idle-toggle {
  margin: 0 0 8px;
  padding: 0;
  border: none;
  background: none;
  color: var(--muted);
  font: inherit;
  font-size: 12.5px;
  cursor: pointer;
}
.idle-toggle:hover {
  color: var(--foreground);
}
.idle .dir {
  color: var(--muted);
}
.form {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin: 12px 0 16px;
}
.lib-sel {
  width: 180px;
}
.form-dir {
  flex: 1;
  min-width: 200px;
}
.suggest {
  padding-top: 12px;
  border-top: 1px solid var(--separator);
}
.suggest-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
  margin-bottom: 8px;
  font-size: 13px;
}
.suggest-head .muted {
  flex: 1;
  min-width: 200px;
  font-size: 12.5px;
}
@media (max-width: 720px) {
  .type-sel,
  .lib-sel {
    width: auto;
    flex: 1;
  }
}
</style>
