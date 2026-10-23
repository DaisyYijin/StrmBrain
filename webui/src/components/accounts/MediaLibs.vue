<script setup lang="ts">
/**
 * 媒体库列表（V2，MULTI-ACCOUNT-V2-PLAN.md §10.1）：一个库一张卡片，用户建几个就有几个。
 * 卡片只读，改在弹窗里（MediaLibDialog），存一次只动一个库（后端 libapiv2.go）。
 *
 * 数据由页面读（账号页签也要知道有没有库、每个账号挂了几个库），这里只显示、改完把新的交回去。
 * 本地媒体根与已存在 / 冗余在上面的 SharedDirs，这里不再重复。
 */
import { computed, ref } from 'vue'
import {
  ArrowDown,
  ArrowRight,
  ArrowUp,
  CircleCheck,
  Cloud,
  FolderInput,
  FolderTree,
  HardDrive,
  ImageDown,
  Pencil,
  Plus,
  Trash2,
  TriangleAlert,
  Tv,
} from '@lucide/vue'
import SectionCard from '@/components/ui/SectionCard.vue'
import HAlert from '@/components/hero/HAlert.vue'
import HButton from '@/components/hero/HButton.vue'
import MediaLibDialog from './MediaLibDialog.vue'
import { libraryApi } from '@/api'
import type { LibSyncStat, MediaLib, MediaLibsView } from '@/api/library'
import { fullTime, relTime } from '@/utils/time'
import { toastError, useFeedback } from '@/composables/useFeedback'

const props = defineProps<{ view: MediaLibsView | null; loading?: boolean }>()
const emit = defineEmits<{ update: [MediaLibsView] }>()
const { message, dialog } = useFeedback()

const editing = ref<MediaLib | null>(null)
const dialogShow = ref(false)

function apply(v: MediaLibsView) {
  emit('update', v)
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
    content: `只删这个库的配置：115 上的文件不动，本地 ${l.local_abs} 里的 STRM 也先留着（之后在本页顶上的「本地残留」里决定清不清）。删了之后它不再同步、不再整理。`,
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
    if (props.view) apply({ ...props.view, notice: [] })
  } catch (e) {
    toastError(e, '操作失败')
  }
}

const multiAcct = computed(() => (props.view?.accounts.length ?? 0) > 1 || !!props.view?.libs.some((l) => l.account !== 0))
const noOrganize = computed(() => !!props.view?.libs.length && !props.view.libs.some((l) => l.organize))

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
  <SectionCard title="媒体库" hint="每个库：115 目录 → 本地目录；放进它的转存文件夹的内容整理进它">
    <template #extra>
      <HButton v-if="view?.libs.length" size="sm" variant="primary" @click="openNew"><Plus :size="14" />添加媒体库</HButton>
    </template>

    <template v-if="view">
      <div v-if="view.root_problem || view.notice.length || noOrganize || view.emby_error" class="alerts">
        <HAlert v-if="view.root_problem" status="danger">{{ view.root_problem }}</HAlert>
        <HAlert v-if="view.notice.length" status="warning" title="媒体库升级了：整理要重新设置">
          <ul class="notice">
            <li v-for="(n, i) in view.notice" :key="i">{{ n }}</li>
          </ul>
          <template #actions>
            <HButton size="sm" variant="secondary" @click="dismissNotice">知道了</HButton>
          </template>
        </HAlert>
        <HAlert v-if="noOrganize" status="warning">
          还没有库设转存/待整理文件夹：自动整理、转存后整理、订阅都停着，只同步 STRM。点库卡片上的「修改」设一个。
        </HAlert>
        <HAlert v-if="view.emby_error" status="default">读不到 Emby 的媒体库（{{ view.emby_error }}），下面不显示 Emby 覆盖情况。</HAlert>
      </div>

      <button v-if="!view.libs.length" type="button" class="empty" @click="openNew">
        <span class="empty-ico"><Plus :size="20" /></span>
        <span class="empty-title">添加第一个媒体库</span>
        <span class="empty-sub">选一个 115 目录同步成本地 STRM；再设个转存文件夹，本站就把新片整理进去</span>
      </button>

      <div v-else class="cards">
        <article v-for="(l, i) in view.libs" :key="l.id" class="lib" :class="{ 'has-problem': l.problem }">
          <header class="lib-head">
            <div class="lib-title">
              <h3 :title="l.name">{{ l.name }}</h3>
              <span v-if="multiAcct" class="tag">{{ l.account_name }}</span>
              <span class="tag" :class="{ 'tag-on': l.organize }">{{ l.organize ? '本站整理' : '只同步' }}</span>
            </div>
            <div class="lib-ops">
              <template v-if="view.libs.length > 1">
                <HButton size="sm" variant="ghost" :disabled="i === 0" aria-label="上移" @click="move(l, -1)"><ArrowUp :size="14" /></HButton>
                <HButton size="sm" variant="ghost" :disabled="i === view.libs.length - 1" aria-label="下移" @click="move(l, 1)">
                  <ArrowDown :size="14" />
                </HButton>
              </template>
              <HButton size="sm" variant="secondary" @click="openEdit(l)"><Pencil :size="14" />修改</HButton>
              <HButton size="sm" variant="ghost" :aria-label="`删除 ${l.name}`" @click="remove(l)"><Trash2 :size="14" /></HButton>
            </div>
          </header>

          <!-- 一个库最要紧的是这条映射：115 哪个目录 → 本地哪个目录 -->
          <div class="route">
            <div class="end">
              <span class="end-label"><Cloud :size="12" />115</span>
              <span class="end-path" :title="l.path || `cid ${l.cid}`">{{ l.path || `cid ${l.cid}` }}</span>
            </div>
            <ArrowRight :size="15" class="route-arrow" />
            <div class="end">
              <span class="end-label"><HardDrive :size="12" />本地</span>
              <code class="end-path" :title="l.local_abs">{{ l.local_abs }}</code>
            </div>
          </div>

          <dl class="facts">
            <dt><FolderInput :size="13" />转存</dt>
            <dd>
              <span v-if="l.organize">{{ l.staging_path || `cid ${l.staging_cid}` }}</span>
              <span v-else class="muted">没设：只同步 STRM，本站不往里整理</span>
            </dd>
            <dt><FolderTree :size="13" />分类</dt>
            <dd>{{ l.scheme_id ? l.scheme_name : '不分类（片目直接放在库目录下）' }}</dd>
            <dt><ImageDown :size="13" />刮削</dt>
            <dd>{{ SCRAPE_TEXT[l.scrape] || l.scrape }}</dd>
            <template v-if="l.emby">
              <dt><Tv :size="13" />Emby</dt>
              <dd>
                <span v-if="l.emby.covered" class="ok">
                  <CircleCheck :size="13" />已在 Emby 里<template v-if="l.emby.suggest">（<code>{{ l.emby.suggest }}</code>）</template>
                </span>
                <div v-else-if="l.emby.partial && l.emby.missing?.length" class="warn">
                  <p class="warn-title">
                    <TriangleAlert :size="13" />只覆盖了一部分：{{ l.emby.missing.length }} 个目录还没进 Emby<template v-if="missingOf(l).base">
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

          <footer class="lib-foot">
            <p v-if="l.problem" class="warn"><TriangleAlert :size="13" />{{ l.problem }}</p>
            <p class="sync-line" :class="{ warn: l.last_sync && !l.last_sync.ok }">
              <span v-if="l.last_sync" :title="syncTitle(l.last_sync)">{{ syncLine(l.last_sync) }}</span>
              <span v-else>还没跑过全量同步（Strm 管理 → 全量同步）</span>
            </p>
          </footer>
        </article>
      </div>
    </template>
    <p v-else-if="loading" class="muted">正在读取…</p>

    <MediaLibDialog v-if="view" v-model:show="dialogShow" :view="view" :lib="editing" @saved="apply" />
  </SectionCard>
</template>

<style scoped>
.alerts {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-bottom: 14px;
}
.notice {
  margin: 6px 0 0;
  padding-left: 18px;
  line-height: 1.7;
}

.empty {
  all: unset;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 32px 16px;
  border: 1.5px dashed var(--border);
  border-radius: var(--r-lg);
  text-align: center;
  cursor: pointer;
  transition:
    border-color 150ms ease,
    background-color 150ms ease;
}
.empty:hover,
.empty:focus-visible {
  border-color: var(--accent);
}
.empty-ico {
  width: 40px;
  height: 40px;
  border-radius: 999px;
  display: grid;
  place-items: center;
  background: var(--accent-soft);
  color: var(--accent);
}
.empty-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--foreground);
}
.empty-sub {
  font-size: 12.5px;
  color: var(--muted);
}

/* 宽屏两三列并排：每张卡片自己装得下，不再被右栏挤成半宽 */
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 420px), 1fr));
  gap: 12px;
}
.lib {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
  padding: 14px 16px;
  border-radius: var(--r-lg);
  background: var(--surface-secondary, var(--default));
}
.lib.has-problem {
  box-shadow: inset 3px 0 0 var(--warning);
}
.lib-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.lib-title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  flex: 1;
  min-width: 0;
}
.lib-title h3 {
  margin: 0;
  max-width: 100%;
  font-size: 15.5px;
  font-weight: 700;
  color: var(--foreground);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.lib-ops {
  display: flex;
  align-items: center;
  gap: 2px;
  flex-shrink: 0;
}
.tag {
  font-size: 11.5px;
  line-height: 19px;
  padding: 0 8px;
  border-radius: 999px;
  background: var(--default);
  color: var(--muted);
  white-space: nowrap;
}
.tag-on {
  background: var(--success-soft);
  color: var(--success-soft-foreground);
}

.route {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border-radius: var(--r-sm);
  background: var(--surface);
}
.end {
  min-width: 0;
}
.end-label {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11.5px;
  color: var(--muted);
}
.end-path {
  display: block;
  margin-top: 1px;
  font-size: 13px;
  font-weight: 600;
  color: var(--foreground);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
code.end-path {
  font-family: var(--font-mono);
  font-size: 12.5px;
}
.route-arrow {
  color: var(--muted);
}

.facts {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr);
  gap: 5px 10px;
  margin: 0;
  font-size: 13px;
}
.facts dt {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 20px;
  color: var(--muted);
}
.facts dd {
  margin: 0;
  min-width: 0;
  line-height: 20px;
  overflow-wrap: anywhere;
}
.ok {
  color: var(--success);
}
.warn {
  color: var(--warning);
  font-size: 13px;
}
p.warn,
.warn-title {
  margin: 0;
}
.ok :deep(svg),
.warn :deep(svg) {
  display: inline-block;
  margin-right: 4px;
  vertical-align: -2px;
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
.lib-foot {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: auto;
  padding-top: 8px;
  border-top: 1px solid var(--separator);
}
.sync-line {
  margin: 0;
  font-size: 12.5px;
  color: var(--muted);
}
.sync-line.warn {
  color: var(--warning);
}

.muted {
  color: var(--muted);
}
code {
  font-family: var(--font-mono);
  font-size: 12.5px;
}

@media (max-width: 720px) {
  .route {
    grid-template-columns: minmax(0, 1fr);
    gap: 4px;
  }
  .route-arrow {
    transform: rotate(90deg);
  }
}
</style>
