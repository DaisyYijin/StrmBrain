<script setup lang="ts">
/**
 * 媒体库列表（V2，MULTI-ACCOUNT-V2-PLAN.md §10.1）：一个库一张卡片，用户建几个就有几个。
 * 卡片只读，改在弹窗里（MediaLibDialog），存一次只动一个库（后端 libapiv2.go）。
 * 本地媒体根固定（MEDIA_ROOT，默认 /media），这里只显示。
 */
import { computed, onMounted, ref } from 'vue'
import { ArrowDown, ArrowUp, FolderInput, Pencil, Plus, Trash2, TriangleAlert } from '@lucide/vue'
import SectionCard from '@/components/ui/SectionCard.vue'
import HAlert from '@/components/hero/HAlert.vue'
import HButton from '@/components/hero/HButton.vue'
import MediaLibDialog from './MediaLibDialog.vue'
import { libraryApi } from '@/api'
import type { LibSyncStat, MediaLib, MediaLibsView } from '@/api/library'
import { fullTime, relTime } from '@/utils/time'
import { toastError, useFeedback } from '@/composables/useFeedback'

const emit = defineEmits<{ changed: [MediaLibsView] }>()
const { message, dialog } = useFeedback()

const view = ref<MediaLibsView | null>(null)
const loading = ref(true)
const editing = ref<MediaLib | null>(null)
const dialogShow = ref(false)

async function load() {
  try {
    view.value = await libraryApi.getMediaLibs()
    emit('changed', view.value)
  } catch (e) {
    toastError(e, '读取媒体库失败')
  } finally {
    loading.value = false
  }
}
onMounted(load)
defineExpose({ reload: load, view })

function apply(v: MediaLibsView) {
  view.value = v
  emit('changed', v)
}

function openNew() {
  editing.value = null
  dialogShow.value = true
}
function openEdit(l: MediaLib) {
  editing.value = l
  dialogShow.value = true
}

async function remove(l: MediaLib) {
  const ok = await dialog.confirm({
    title: `删除媒体库「${l.name}」`,
    content: `只删这个库的配置：115 上的文件不动，本地 ${l.local_abs} 里的 STRM 也先留着（之后在上面「本地残留」里决定清不清）。删了之后它不再同步、不再整理。`,
    actions: [
      { label: '取消', value: false, variant: 'tertiary' },
      { label: '删除', value: true, variant: 'danger' },
    ],
  })
  if (!ok) return
  try {
    apply(await libraryApi.deleteMediaLib(l.id))
    message.success(`已删除「${l.name}」`)
  } catch (e) {
    toastError(e, '删除失败')
  }
}

async function move(l: MediaLib, dir: -1 | 1) {
  try {
    apply(await libraryApi.moveMediaLib(l.id, dir))
  } catch (e) {
    toastError(e, '调整顺序失败')
  }
}

async function dismissNotice() {
  try {
    await libraryApi.dismissMediaLibNotice()
    if (view.value) view.value.notice = []
  } catch (e) {
    toastError(e, '操作失败')
  }
}

const multiAcct = computed(() => (view.value?.accounts.length ?? 0) > 1 || view.value?.libs.some((l) => l.account !== 0))
const noOrganize = computed(() => !!view.value?.libs.length && !view.value.libs.some((l) => l.organize))

function durText(sec: number) {
  if (sec < 60) return `${sec} 秒`
  if (sec < 3600) return `${Math.round(sec / 60)} 分钟`
  return `${(sec / 3600).toFixed(1)} 小时`
}
function syncLine(s: LibSyncStat) {
  const when = relTime(new Date(s.at * 1000).toISOString())
  if (!s.ok) return `上次全量 ${when} 失败：${s.error || '原因未知'}`
  const parts = [`视频 ${s.videos}`, `新增 STRM ${s.created}`]
  if (s.failed) parts.push(`写失败 ${s.failed}`)
  if (s.orphans) parts.push(`失效 STRM ${s.orphans} 待清理`)
  return `上次全量 ${when}（${durText(s.seconds)}）：${parts.join('，')}`
}
const syncTitle = (s: LibSyncStat) => fullTime(new Date(s.at * 1000).toISOString())
// 「还没进 Emby 的目录」按分类建 Emby 库时动辄几十个：去掉共同的上级、只先列几个，其余折起来
const MISS_FOLD = 8
const missOpen = ref(new Set<number>())
function toggleMiss(id: number) {
  const next = new Set(missOpen.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  missOpen.value = next
}
function missingOf(l: MediaLib): { base: string; rest: string[] } {
  const list = l.emby?.missing ?? []
  if (list.length < 2) return { base: '', rest: list }
  const split = list.map((m) => m.split('/'))
  let n = 0
  // 只取到上级目录为止：至少给每一项留最后一段
  const maxN = Math.min(...split.map((p) => p.length - 1))
  while (n < maxN && split.every((p) => p[n] === split[0][n])) n++
  const base = split[0].slice(0, n).join('/')
  if (!base) return { base: '', rest: list }
  return { base, rest: split.map((p) => p.slice(n).join('/')) }
}
function missingShown(l: MediaLib) {
  const rest = missingOf(l).rest
  return missOpen.value.has(l.id) || rest.length <= MISS_FOLD ? rest : rest.slice(0, MISS_FOLD)
}
const SCRAPE_TEXT: Record<string, string> = { auto: '自动刮新片', manual: '只手动刮', off: '不刮（交给 Emby）' }
</script>

<template>
  <SectionCard title="媒体库" hint="每个库：115 目录 → 本地目录，放进它的转存文件夹的内容整理进它">
    <template #extra>
      <HButton size="sm" variant="secondary" @click="openNew"><Plus :size="14" />添加媒体库</HButton>
    </template>

    <template v-if="view">
      <p class="root-line">
        本地媒体根：<code>{{ view.media_root }}</code>
        <span class="muted small">（容器环境变量 MEDIA_ROOT 决定，默认 /media；各库的本地目录都在它下面）</span>
      </p>
      <HAlert v-if="view.root_problem" status="danger" class="mb">{{ view.root_problem }}</HAlert>

      <HAlert v-if="view.notice.length" status="warning" title="媒体库升级了：整理要重新设置" class="mb">
        <ul class="notice">
          <li v-for="(n, i) in view.notice" :key="i">{{ n }}</li>
        </ul>
        <template #actions>
          <HButton size="sm" variant="secondary" @click="dismissNotice">知道了</HButton>
        </template>
      </HAlert>

      <HAlert v-if="noOrganize" status="warning" class="mb">
        还没有库设转存/待整理文件夹：自动整理、转存后整理、订阅都停着，只同步 STRM。点库卡片上的「修改」设一个。
      </HAlert>
      <HAlert v-if="view.emby_error" status="default" class="mb">读不到 Emby 的媒体库（{{ view.emby_error }}），下面不显示 Emby 覆盖情况。</HAlert>

      <p v-if="!view.libs.length" class="muted empty">还没有媒体库。点右上角「添加媒体库」选一个 115 目录。</p>

      <div class="cards">
        <article v-for="(l, i) in view.libs" :key="l.id" class="lib">
          <header class="lib-head">
            <h3>{{ l.name }}</h3>
            <span v-if="multiAcct" class="tag">{{ l.account_name }}</span>
            <span v-if="!l.organize" class="tag">只同步</span>
            <span class="grow" />
            <HButton v-if="view.libs.length > 1" size="sm" variant="ghost" :disabled="i === 0" aria-label="上移" @click="move(l, -1)">
              <ArrowUp :size="14" />
            </HButton>
            <HButton v-if="view.libs.length > 1" size="sm" variant="ghost" :disabled="i === view.libs.length - 1" aria-label="下移" @click="move(l, 1)">
              <ArrowDown :size="14" />
            </HButton>
            <HButton size="sm" variant="ghost" @click="openEdit(l)"><Pencil :size="14" />修改</HButton>
            <HButton size="sm" variant="ghost" @click="remove(l)"><Trash2 :size="14" />删除</HButton>
          </header>

          <dl class="facts">
            <dt>115</dt>
            <dd>{{ l.path || `cid ${l.cid}` }}</dd>
            <dt>本地</dt>
            <dd><code>{{ l.local_abs }}</code></dd>
            <dt>转存</dt>
            <dd>
              <span v-if="l.organize" class="with-icon"><FolderInput :size="13" />{{ l.staging_path || `cid ${l.staging_cid}` }}</span>
              <span v-else class="muted">没设：只同步 STRM，本站不往里整理</span>
            </dd>
            <dt>分类</dt>
            <dd>{{ l.scheme_id ? l.scheme_name : '不分类（片目直接放在库目录下）' }}</dd>
            <dt>刮削</dt>
            <dd>{{ SCRAPE_TEXT[l.scrape] || l.scrape }}</dd>
            <template v-if="l.emby">
              <dt>Emby</dt>
              <dd>
                <span v-if="l.emby.covered">已在 Emby 里<template v-if="l.emby.suggest">（<code>{{ l.emby.suggest }}</code>）</template></span>
                <div v-else-if="l.emby.partial && l.emby.missing?.length" class="warn">
                  <p class="warn-title">
                    <TriangleAlert :size="13" />Emby 只覆盖了一部分：{{ l.emby.missing.length }} 个目录还没进 Emby<template v-if="missingOf(l).base">
                      （都在 <code>{{ missingOf(l).base }}</code> 下）</template>
                  </p>
                  <ul class="miss">
                    <li v-for="m in missingShown(l)" :key="m"><code>{{ m }}</code></li>
                    <li v-if="missingOf(l).rest.length > MISS_FOLD">
                      <button type="button" class="more" @click="toggleMiss(l.id)">
                        {{ missOpen.has(l.id) ? '收起' : `还有 ${missingOf(l).rest.length - MISS_FOLD} 个` }}
                      </button>
                    </li>
                  </ul>
                </div>
                <span v-else-if="l.emby.partial">Emby 覆盖了这个库下面的部分目录</span>
                <span v-else-if="l.emby.unmapped" class="warn"><TriangleAlert :size="13" />路径映射换算不出这个库在 Emby 里的路径（设置 → Emby → 路径映射）</span>
                <span v-else class="warn">
                  <TriangleAlert :size="13" />Emby 里还没有这个目录<template v-if="l.emby.suggest">：在 Emby 里添加 <code>{{ l.emby.suggest }}</code></template>
                </span>
              </dd>
            </template>
          </dl>

          <p v-if="l.problem" class="warn"><TriangleAlert :size="13" />{{ l.problem }}</p>
          <p class="sync-line" :class="{ warn: l.last_sync && !l.last_sync.ok }">
            <span v-if="l.last_sync" :title="syncTitle(l.last_sync)">{{ syncLine(l.last_sync) }}</span>
            <span v-else class="muted">还没跑过全量同步（Strm 管理 → 全量同步）</span>
          </p>
        </article>
      </div>

      <p class="ws-line muted small">
        已存在：<code>{{ view.existing || '未设置' }}</code> · 冗余：<code>{{ view.redundant || '未设置' }}</code>
        （所有库共用这一套路径；别的账号在自己的网盘里同一路径下各建一份，整理时自动创建）
      </p>
    </template>
    <p v-else-if="loading" class="muted">正在读取…</p>

    <MediaLibDialog v-if="view" v-model:show="dialogShow" :view="view" :lib="editing" @saved="apply" />
  </SectionCard>
</template>

<style scoped>
.root-line {
  margin: 0 0 12px;
  font-size: 13px;
}
.mb {
  margin-bottom: 12px;
}
.notice {
  margin: 6px 0 0;
  padding-left: 18px;
  line-height: 1.7;
}
.empty {
  padding: 16px 0;
}
.cards {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.lib {
  padding: 12px 14px;
  border-radius: var(--r-lg);
  background: var(--surface-secondary, var(--default));
}
.lib-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.lib-head h3 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}
.tag {
  font-size: 12px;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--default);
  color: var(--muted);
}
.grow {
  flex: 1;
}
.facts {
  display: grid;
  grid-template-columns: 48px minmax(0, 1fr);
  gap: 4px 10px;
  margin: 0;
  font-size: 13px;
}
.facts dt {
  color: var(--muted);
}
.facts dd {
  margin: 0;
  min-width: 0;
  overflow-wrap: anywhere;
}
.facts :deep(svg) {
  vertical-align: -2px;
}
.warn {
  color: var(--warning);
  font-size: 13px;
}
p.warn,
.warn-title {
  margin: 0;
}
.warn :deep(svg),
.with-icon :deep(svg) {
  display: inline-block;
  margin-right: 4px;
  vertical-align: -2px;
}
.with-icon {
  display: inline;
}
.miss {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 6px;
  margin: 6px 0 0;
  padding: 0;
  list-style: none;
}
.miss code {
  padding: 1px 6px;
  border-radius: var(--r-sm);
  background: color-mix(in oklab, var(--warning) 12%, transparent);
  white-space: nowrap;
}
.more {
  all: unset;
  cursor: pointer;
  font-size: 12.5px;
  color: var(--accent);
}
.more:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 1px;
}
.sync-line {
  margin: 8px 0 0;
  font-size: 12.5px;
}
.ws-line {
  margin: 12px 0 0;
}
.muted {
  color: var(--muted);
}
.small {
  font-size: 12.5px;
}
code {
  font-family: var(--font-mono);
  font-size: 12.5px;
}
</style>
