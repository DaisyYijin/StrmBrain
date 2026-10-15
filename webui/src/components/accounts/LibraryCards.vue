<script setup lang="ts">
/**
 * 多个媒体库：一库一卡（docs/115-station-notes/MULTI-LIB-SIMPLIFY-PLAN.md）。
 *
 * 卡片只管「库在哪、做什么、从哪进」：115 目录、本地目录、谁来整理（LIBRARY-ROLES-PLAN.md）、
 * 转存入口（绑定的转存子目录）、移除。
 * 「我自己整理」的库本站不往里放东西：没有分类、没有转存入口，只声明片目在第几层（按台账马上给预览）。
 * **分类归属只读**，改去二级分类：卡片上只剩一个分类名，条件、顺序看不到，
 * 「往动漫库添加日番」会被读成在定义「什么是动漫」（§0.5）。
 *
 * 默认库（full.cid，「默认放这里」）的 115 目录仍在上面「媒体库位置」里改，这里只列出来。
 * 只有一个库时整张卡片不出现，只留一行「添加媒体库」。
 *
 * 保存走 PUT /libraries：附加库与入口整份替换；移除库是单独的一步（确认框里说清后果，立刻保存）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { FolderInput, Plus, Trash2, TriangleAlert, X } from '@lucide/vue'
import SectionCard from '@/components/ui/SectionCard.vue'
import FormActions from '@/components/ui/FormActions.vue'
import MediaTypeChip from '@/components/MediaTypeChip.vue'
import Cid115Input, { type CidValue } from '@/components/Cid115Input.vue'
import HAlert from '@/components/hero/HAlert.vue'
import HButton from '@/components/hero/HButton.vue'
import HCheckbox from '@/components/hero/HCheckbox.vue'
import HInput from '@/components/hero/HInput.vue'
import HModal from '@/components/hero/HModal.vue'
import HSegmented from '@/components/hero/HSegmented.vue'
import HSelect from '@/components/hero/HSelect.vue'
import HSwitch from '@/components/hero/HSwitch.vue'
import { configApi, libraryApi, storageApi } from '@/api'
import { ApiError } from '@/api/client'
import type { LibInboxConfirm, LibInboxReq, LibMode, LibOpt, LibPreview, LibrariesView, LibraryView } from '@/api/library'
import type { LibScrape } from '@/api/organize'
import { toastError, useFeedback } from '@/composables/useFeedback'
import { useStagingSubs } from '@/composables/stagingSubs'
import { useQueueStore } from '@/stores/queue'

const emit = defineEmits<{
  /** 保存成功：附加库清单变了，上层同步 full 配置、刷新残留提示 */
  saved: [extraLibs: { cid: string; path: string }[]]
}>()

const { message, dialog } = useFeedback()
const queue = useQueueStore()
const staging = useStagingSubs()

/** 一个库在编辑中的样子 */
interface DraftLib {
  cid: string
  /** 新加的库：还没选目录时 cid 为空，目录在 pick 里 */
  isNew?: boolean
  pick?: CidValue
  view?: LibraryView
  /** 已有的入口（cid）与要新建的入口（只有名字） */
  inboxes: { cid?: string; name: string; problem?: string }[]
  /** 入口开关打开、还没有任何入口时的新入口名 */
  inboxDraft?: string
  /** 谁来整理；新加的库要用户选（空 = 还没选） */
  mode: LibMode | ''
  /** 自己整理的库：片目在第几层 */
  depth: number
  /** 刮不刮（阶段 2）；新加的库默认自动刮新片 */
  scrape: LibScrape
  /** 按 depth 认出来的片目（不参与「有没有改动」的比较） */
  preview?: LibPreview | null
  previewing?: boolean
  expanded?: boolean
}

const view = ref<LibrariesView | null>(null)
const loading = ref(true)
const saving = ref(false)
const drafts = ref<DraftLib[]>([])
const newInputs = ref<Record<number, InstanceType<typeof Cid115Input> | null>>({})
let snapshot = '[]'

function draftsFrom(v: LibrariesView): DraftLib[] {
  return v.libs.map((l) => ({
    cid: l.cid,
    view: l,
    inboxes: l.inboxes.map((b) => ({ cid: b.cid, name: b.name, problem: b.problem })),
    mode: l.mode === 'self' ? 'self' : 'organize',
    depth: l.depth || 2,
    scrape: l.scrape || 'auto',
  }))
}
const serialize = (d: DraftLib[]) =>
  JSON.stringify(
    d.map((x) => ({
      cid: x.cid, pick: x.pick?.cid ?? '', path: x.pick?.path ?? '', inboxes: x.inboxes, draft: x.inboxDraft ?? null,
      mode: x.mode, depth: x.mode === 'self' ? x.depth : 0, scrape: x.scrape,
    })),
  )
const dirty = computed(() => serialize(drafts.value) !== snapshot)

function apply(v: LibrariesView) {
  view.value = v
  drafts.value = draftsFrom(v)
  snapshot = serialize(drafts.value)
  for (const d of drafts.value) if (d.mode === 'self') void loadPreview(d)
}

async function load() {
  loading.value = true
  try {
    apply(await libraryApi.getLibraries())
  } catch (e) {
    toastError(e, '读取媒体库失败')
  } finally {
    loading.value = false
  }
}
onMounted(load)
// 上面「保存媒体库位置」存完会叫这里刷新（默认库换了目录，名字跟着变）。卡片上有没存的改动时不刷：
// 刷了就把用户刚开的入口、刚加的库悄悄丢掉（2026-10-15 实测踩到）
defineExpose({ reload: () => (dirty.value ? Promise.resolve() : load()) })

/** 有附加库、或正在加第一个附加库：显示「默认放这里」、转存入口这些只在多库时有意义的东西 */
const multi = computed(() => drafts.value.length > 1)
const defaultName = computed(() => view.value?.libs[0]?.name || '默认库')
const stagingRoot = computed(() => view.value?.staging_root ?? '')

function libLabel(d: DraftLib) {
  if (d.isNew) return d.pick?.path ? d.pick.path.split('/').filter(Boolean).pop() || '新的媒体库' : '新的媒体库'
  return d.view?.name || `cid ${d.cid}`
}

// ---- 添加 ----

function addLib() {
  drafts.value.push({ cid: '', isNew: true, pick: { cid: '', path: '' }, inboxes: [], mode: '', depth: 2, scrape: 'auto' })
}
function dropNew(i: number) {
  drafts.value.splice(i, 1)
}

// ---- 刮不刮 ----

const SCRAPE_OPTS: { label: string; value: LibScrape }[] = [
  { label: '自动刮新片', value: 'auto' },
  { label: '只手动刮', value: 'manual' },
  { label: '不刮（交给 Emby）', value: 'off' },
]

function scrapeHint(d: DraftLib) {
  if (d.scrape === 'auto')
    return d.mode === 'self'
      ? '同步进来的新片目会刮；目录名对不上 TMDB 的不刮（不猜），定时补全也补它'
      : '整理进来的、同步进来的新片目都会刮，定时补全也补它'
  if (d.scrape === 'manual') return '只在海报墙勾选后刮，自动刮削和定时补全都不碰它'
  return '本站不写 NFO 和图片，交给 Emby 自己刮；海报墙按 Emby 的数据看刮没刮全'
}

// ---- 谁来整理 ----

const MODE_OPTS: { label: string; value: LibMode }[] = [
  { label: '本站整理', value: 'organize' },
  { label: '我自己整理', value: 'self' },
]
const DEPTH_OPTS = [
  { label: '库 / 片目', value: 1 },
  { label: '库 / 分类 / 片目', value: 2 },
  { label: '库 / 分类 / 子分类 / 片目', value: 3 },
]

function modeHint(d: DraftLib) {
  if (d.view?.default) return '首个媒体库暂时只能由本站整理：二级分类里没指定库的规则都放进它'
  if (d.mode === 'organize') return '转存、下载进来的内容由本站识别、改名，按分类放进这个库'
  if (d.mode === 'self') return '本站不往这个库里放东西；同步 STRM、海报墙、刮削、Emby 探测照常'
  return '选一下：内容由本站整理进来，还是目录你自己管'
}

/** 切换整理方式。已有的库要先说清后果（规则回默认库、入口解绑），新加的库直接切 */
async function setMode(d: DraftLib, to: LibMode) {
  if (to === d.mode) return
  if (!d.isNew) {
    const lines =
      to === 'self'
        ? [
            ...(d.view?.rules.length ? [`${d.view.rules.map((r) => r.name).join('、')} 改回「${defaultName.value}」（二级分类里的 library 会被删掉）`] : []),
            ...(inboxOn(d) ? ['转存入口解绑：子目录和里面的东西不删，引用它的订阅改为转存到转存目录本身'] : []),
            '库里已有的片原地不动；之后本站不再往里整理，网盘文件页里也不能整理、移动它下面的片',
          ]
        : [
            '已有的片不搬、不改名',
            '不在分类目录下的片海报墙会认不出（可以在网盘文件页对它们「整理」）',
            '之后在二级分类里给规则选这个库、或打开转存入口，内容才会整理进来',
          ]
    const ok = await dialog.confirm({
      title: `「${libLabel(d)}」改成${to === 'self' ? '我自己整理' : '本站整理'}`,
      content: `${lines.map((s) => `· ${s}`).join('\n')}\n\n保存后生效。`,
      actions: [
        { label: '取消', value: false, variant: 'tertiary' },
        { label: '改', value: true, variant: 'primary' },
      ],
    })
    if (!ok) return
  }
  d.mode = to
  if (to === 'self') {
    d.inboxes = []
    d.inboxDraft = undefined
    void loadPreview(d)
  }
}

/** 按选的层级预览认出来的片目（只读台账，零 115 请求）。新加的库按输入的目录名查 */
async function loadPreview(d: DraftLib) {
  if (d.isNew && !d.pick?.path) {
    d.preview = null
    return
  }
  const depth = d.depth
  d.previewing = true
  try {
    const p = await libraryApi.previewLibrary(d.cid, depth, d.isNew ? libLabel(d) : undefined)
    if (d.depth === depth) d.preview = p // 连着切了几次层级：只留最后一次的
  } catch (e) {
    toastError(e, '读取片目预览失败')
  } finally {
    d.previewing = false
  }
}
// 新加的库先选了「我自己整理」、后选目录：目录定了再预览
watch(
  () => drafts.value.map((d) => (d.isNew && d.mode === 'self' ? (d.pick?.path ?? '') : '')).join('\n'),
  () => {
    for (const d of drafts.value) if (d.isNew && d.mode === 'self') void loadPreview(d)
  },
)
function setDepth(d: DraftLib, v: number) {
  d.depth = v
  void loadPreview(d)
}

/** 本站整理却什么都进不来：不是默认库、没有分类、没有入口 */
const idle = (d: DraftLib) => d.mode === 'organize' && !d.isNew && !d.view?.default && !d.view?.rules.length && !inboxOn(d)

// ---- 转存入口 ----

function inboxOn(d: DraftLib) {
  return d.inboxes.length > 0 || d.inboxDraft !== undefined
}
function toggleInbox(d: DraftLib, on: boolean) {
  if (on) {
    d.inboxDraft = libLabel(d)
  } else {
    d.inboxes = []
    d.inboxDraft = undefined
  }
}

// ---- 保存 ----

/** 新加的库：把输入框里的路径换成 cid 与可读路径；认不出的拦下来，别静默丢掉 */
async function resolveNew(): Promise<boolean> {
  for (let i = 0; i < drafts.value.length; i++) {
    const d = drafts.value[i]
    if (!d.isNew) continue
    if (!d.mode) {
      message.error('新加的媒体库还没选谁来整理：本站整理，还是我自己整理')
      return false
    }
    const cid = (await newInputs.value[i]?.ensureCid()) ?? d.pick?.cid ?? ''
    if (!cid || cid === '0') {
      message.error('新加的媒体库目录无法识别：请点「选择目录」重新选择，或输入纯数字 cid')
      return false
    }
    let p = d.pick?.path.trim() ?? ''
    if (!p || /^\d+$/.test(p)) {
      try {
        p = (await storageApi.path115(cid)).path
      } catch {
        p = ''
      }
    }
    d.cid = cid
    d.pick = { cid, path: p }
  }
  return true
}

function buildReq(list: DraftLib[], confirmed: Set<string>) {
  const extra_libs = list.slice(1).map((d) => ({ cid: d.cid, path: d.isNew ? (d.pick?.path ?? '') : d.view?.path || '' }))
  const inboxes: LibInboxReq[] = []
  for (const d of list) {
    for (const b of d.inboxes) inboxes.push({ cid: b.cid, name: b.name, lib: d.cid })
    const name = d.inboxDraft?.trim()
    if (name && !d.inboxes.length) inboxes.push({ name, lib: d.cid, confirm: confirmed.has(name) })
  }
  const opts: Record<string, LibOpt> = {}
  for (const d of list) opts[d.cid] = d.mode === 'self' ? { mode: 'self', depth: d.depth, scrape: d.scrape } : { mode: 'organize', scrape: d.scrape }
  return { extra_libs, inboxes, opts }
}

/** 发请求；同名子目录已存在且不空时问一次，确认了带上 confirm 重发 */
async function submit(list: DraftLib[], clean: string[] = []): Promise<boolean> {
  const confirmed = new Set<string>()
  for (;;) {
    try {
      const r = await libraryApi.saveLibraries({ ...buildReq(list, confirmed), clean_local: clean })
      apply(r.data)
      emit('saved', r.data.libs.slice(1).map((l) => ({ cid: l.cid, path: l.path })))
      void staging.reload()
      message.success(r.subs_moved ? `已保存；${r.subs_moved} 个订阅改为转存到转存目录本身` : '已保存')
      for (const id of r.jobs ?? []) await queue.submitted(id)
      return true
    } catch (e) {
      const need = e instanceof ApiError && e.status === 409 ? (e.data.confirm as LibInboxConfirm[] | undefined) : undefined
      if (!need?.length) {
        toastError(e, '保存失败')
        return false
      }
      const ok = await dialog.confirm({
        title: '转存目录里已有同名子目录',
        content:
          need.map((n) => `「${n.name}」里现在有 ${n.items} 项`).join('\n') +
          '\n\n绑定后，它们和之后放进来的内容都会整理进对应的媒体库。',
        actions: [
          { label: '取消', value: false, variant: 'tertiary' },
          { label: '绑定', value: true, variant: 'primary' },
        ],
      })
      if (!ok) return false
      need.forEach((n) => confirmed.add(n.name))
    }
  }
}

async function save() {
  saving.value = true
  try {
    if (await resolveNew()) await submit(drafts.value)
  } finally {
    saving.value = false
  }
}

function discard() {
  if (view.value) apply(view.value)
}

// ---- 移除 ----

const rm = ref<{ show: boolean; lib: DraftLib | null; clean: boolean }>({ show: false, lib: null, clean: true })
function openRemove(d: DraftLib) {
  if (dirty.value) {
    message.warning('先保存或放弃其他改动，再移除媒体库')
    return
  }
  rm.value = { show: true, lib: d, clean: true }
}
async function confirmRemove() {
  const lib = rm.value.lib
  if (!lib || !view.value) return
  saving.value = true
  try {
    const rest = draftsFrom(view.value).filter((d) => d.cid !== lib.cid)
    if (await submit(rest, rm.value.clean ? [lib.cid] : [])) rm.value.show = false
  } finally {
    saving.value = false
  }
}

// ---- 提示 ----

const anyUnmapped = computed(() => view.value?.libs.some((l) => l.emby?.unmapped) ?? false)
const fixingMapping = ref(false)
/** 路径映射的本地一侧只填了某个库：改成留空 = 跟随本地根目录（后端 embyPathRoots） */
async function fixMapping() {
  fixingMapping.value = true
  try {
    const emby = await configApi.getSetting<Record<string, unknown>>('emby', {})
    const pm = String(emby.path_mapping ?? '')
    const embySide = pm.includes('#') ? pm.slice(pm.indexOf('#') + 1) : ''
    await configApi.saveSetting('emby', { ...emby, path_mapping: `#${embySide}` })
    message.success('路径映射已改成跟随本地媒体库根目录')
    await load()
  } catch (e) {
    toastError(e, '修改路径映射失败')
  } finally {
    fixingMapping.value = false
  }
}

/** 库在 115 上改了名：规则里写的旧名一键换成新名（新名后端认，设计文档 R4） */
const fixing = ref('')
async function fixRename(from: string, to: string) {
  if (dirty.value) {
    message.warning('先保存或放弃卡片上的改动')
    return
  }
  const ok = await dialog.confirm({
    title: `把「${from}」改成「${to}」`,
    content: `二级分类里写着 library: ${from} 的规则都改成 ${to}，条件和顺序不动。改完这几类会照常整理进「${to}」。`,
    actions: [
      { label: '取消', value: false, variant: 'tertiary' },
      { label: '改', value: true, variant: 'primary' },
    ],
  })
  if (!ok) return
  fixing.value = from
  try {
    const r = await libraryApi.fixRename(from)
    apply(r.data)
    message.success(r.message)
  } catch (e) {
    toastError(e, '修改失败')
  } finally {
    fixing.value = ''
  }
}

const CATEGORY_LINK = { name: 'organize', query: { tab: 'category' } }
</script>

<template>
  <!-- 只有一个库也显示卡片：「整理」「刮削」两行对单库同样有用（LIBRARY-ROLES-PLAN.md §8-2） -->
  <SectionCard
    v-if="!loading && view"
    title="媒体库"
    :hint="multi ? '每个库在哪、由谁整理、刮不刮；怎么分类、怎么刮在各自的页面里' : '这个库由谁整理、刮不刮；怎么分类、怎么刮在各自的页面里'"
  >
    <HAlert v-if="anyUnmapped" status="warning" title="Emby 路径映射只对得上一部分库" class="mb">
      路径映射的本地一侧只填了某一个库的目录，其他库在 Emby 里的路径换不回本地（入库刷新、删除联动都会对不上）。
      <template #actions>
        <HButton size="sm" variant="secondary" :loading="fixingMapping" @click="fixMapping">改成跟随本地根目录</HButton>
      </template>
    </HAlert>
    <HAlert v-if="view.emby_error" status="default" class="mb">读不到 Emby 的媒体库（{{ view.emby_error }}），下面不显示 Emby 覆盖情况。</HAlert>
    <p v-if="!view.names_known" class="muted small mb">有的库名字还没读到（打开一次网盘文件页或跑一次同步后就有），暂时显示 cid。</p>

    <div class="cards">
      <article v-for="(d, i) in drafts" :key="d.cid || `new-${i}`" class="lib">
        <header class="lib-head">
          <h3>{{ libLabel(d) }}</h3>
          <span v-if="multi && d.view?.default" class="tag-rest">默认放这里</span>
          <span v-if="d.isNew" class="tag-new">新加，保存后生效</span>
          <span class="grow" />
          <HButton v-if="d.isNew" size="sm" variant="ghost" @click="dropNew(i)"><X :size="14" />不加了</HButton>
          <HButton v-else-if="!d.view?.default" size="sm" variant="ghost" @click="openRemove(d)">
            <Trash2 :size="14" />移除
          </HButton>
        </header>

        <div v-if="d.isNew" class="row">
          <div class="row-label">115 目录</div>
          <div class="row-body">
            <Cid115Input
              :ref="(el) => (newInputs[i] = el as InstanceType<typeof Cid115Input> | null)"
              v-model="d.pick!"
              placeholder="选择另一个 115 媒体库目录"
            />
            <p class="muted small">和已有的库不能互相包含，目录名也不能重名（本地 STRM 第一层就是库目录名）。</p>
          </div>
        </div>
        <p v-else class="lib-path">
          115：{{ d.view?.path || d.cid }}<template v-if="d.view?.local"><span class="sep">·</span>本地：{{ d.view.local }}</template>
          <template v-if="d.view?.default"><span class="sep">·</span>在上面「媒体库位置」里改</template>
        </p>

        <!-- 谁来整理（LIBRARY-ROLES-PLAN.md） -->
        <div class="row">
          <div class="row-label">整理</div>
          <div class="row-body">
            <HSegmented
              :model-value="d.mode || undefined"
              :options="MODE_OPTS"
              size="sm"
              :disabled="d.view?.default"
              :aria-label="`${libLabel(d)} 由谁整理`"
              @update:model-value="(v) => setMode(d, v as LibMode)"
            />
            <p class="muted small">{{ modeHint(d) }}</p>
          </div>
        </div>

        <!-- 自己整理：片目在第几层 + 预览 -->
        <div v-if="d.mode === 'self'" class="row">
          <div class="row-label">片目在</div>
          <div class="row-body">
            <HSelect
              :model-value="d.depth"
              :options="DEPTH_OPTS"
              class="depth"
              aria-label="片目在第几层"
              @update:model-value="(v) => setDepth(d, Number(v))"
            />
            <template v-if="d.preview">
              <p v-if="!d.preview.total" class="muted small">
                {{ d.isNew ? '台账里还没有这个目录的视频：保存后跑一次全量同步，再回来看认得对不对。' : '按这个层级一部都认不出来；还没同步过的话先跑一次全量同步。' }}
              </p>
              <template v-else>
                <p class="cat-line">
                  认出 {{ d.preview.total }} 部：
                  <template v-for="(t, k) in d.preview.titles.slice(0, 4)" :key="t.key">
                    <span v-if="k" class="sep">·</span>{{ t.title }}
                  </template>
                  <template v-if="d.preview.total > 4"> …</template>
                  <button class="cat-link btn-link" @click="d.expanded = !d.expanded">{{ d.expanded ? '收起' : '看全部' }}</button>
                </p>
                <p v-if="d.preview.odd" class="warn-line">
                  <TriangleAlert :size="13" />有 {{ d.preview.odd }} 部看着不像一部片，层级可能选错了（展开看原因）
                </p>
                <p v-if="d.preview.loose" class="muted small">另有 {{ d.preview.loose }} 个视频比片目那一层还浅，不算任何一部。</p>
                <ul v-if="d.expanded" class="titles">
                  <li v-for="t in d.preview.titles" :key="t.key" :class="{ odd: t.odd }">
                    <MediaTypeChip :type="t.media_type" />
                    <span class="t-name">{{ t.title }}</span>
                    <span v-if="t.odd" class="t-odd">{{ t.odd }}</span>
                    <span v-else class="muted t-path">{{ t.key }}</span>
                  </li>
                  <li v-if="d.preview.total > d.preview.titles.length" class="muted">还有 {{ d.preview.total - d.preview.titles.length }} 部没列出</li>
                </ul>
              </template>
            </template>
            <p v-else-if="d.previewing" class="muted small">正在按台账认片目…</p>
            <p v-else-if="d.isNew" class="muted small">选好 115 目录后这里会按台账预览认出的片目。</p>
          </div>
        </div>

        <!-- 分类：只读，改去二级分类（§0.5） -->
        <div v-if="!d.isNew && d.mode === 'organize'" class="row">
          <div class="row-label">分类</div>
          <div class="row-body">
            <p class="cat-line">
              <template v-if="d.view?.default && !multi">按二级分类的 {{ d.view.rules.length }} 条规则整理进来</template>
              <template v-else-if="d.view?.default">没指定库的规则都落在这里（{{ d.view.rules.length }} 条）</template>
              <template v-else-if="d.view?.rules.length">
                <template v-for="(r, k) in d.view.rules" :key="`${r.media_type}:${r.name}`">
                  <span v-if="k" class="sep">·</span><MediaTypeChip :type="r.media_type" /> {{ r.name }}
                </template>
              </template>
              <template v-else-if="inboxOn(d)">没有规则指向这里，只收转存入口进来的</template>
              <span v-else class="warn-line">
                <TriangleAlert :size="13" />没有分类指向这里，也没有转存入口：什么都不会整理进来。在二级分类里给规则选这个库、打开下面的转存入口，或者改成「我自己整理」
              </span>
            </p>
            <RouterLink :to="CATEGORY_LINK" class="cat-link">{{ idle(d) ? '去二级分类给规则选这个库 →' : '去二级分类修改 →' }}</RouterLink>
          </div>
        </div>

        <!-- 转存入口：只有本站整理的库有；只有一个库时什么都进它，入口没意义（已有的照样列出来能解绑） -->
        <div v-if="d.mode === 'organize' && (multi || d.inboxes.length)" class="row">
          <div class="row-label">转存入口</div>
          <div class="row-body">
            <div class="inbox-line">
              <HSwitch
                :model-value="inboxOn(d)"
                :disabled="!stagingRoot"
                size="sm"
                :aria-label="`${libLabel(d)} 的转存入口`"
                @update:model-value="(v: boolean) => toggleInbox(d, v)"
              />
              <span class="muted small">
                {{ stagingRoot ? `放进这个子目录的内容，不看分类，都整理进「${libLabel(d)}」` : '先在「影视转存 → 设置 → 转存目录」设置转存目录' }}
              </span>
            </div>
            <div v-for="(b, k) in d.inboxes" :key="b.cid || b.name" class="inbox">
              <FolderInput :size="14" />转存 / {{ b.name }}
              <button class="x" :aria-label="`解绑 ${b.name}`" @click="d.inboxes.splice(k, 1)"><X :size="12" /></button>
              <p v-if="b.problem" class="problem"><TriangleAlert :size="13" />{{ b.problem }}</p>
            </div>
            <div v-if="d.inboxDraft !== undefined && !d.inboxes.length" class="inbox-new">
              <span class="muted small">转存 /</span>
              <HInput v-model="d.inboxDraft" class="inbox-name" :input-attrs="{ 'aria-label': '转存入口子目录名' }" />
              <span class="muted small">保存时建立</span>
            </div>
          </div>
        </div>

        <!-- 刮不刮（阶段 2）：取代原来全局的「刮削方式」与「整理后 / 同步后自动刮削」 -->
        <div class="row">
          <div class="row-label">刮削</div>
          <div class="row-body">
            <HSegmented
              :model-value="d.scrape"
              :options="SCRAPE_OPTS"
              size="sm"
              :aria-label="`${libLabel(d)} 的刮削`"
              @update:model-value="(v) => (d.scrape = v as LibScrape)"
            />
            <p class="muted small">{{ scrapeHint(d) }}</p>
          </div>
        </div>

        <template v-if="d.view?.emby">
          <HAlert v-if="!d.view.emby.covered && !d.view.emby.unmapped" status="warning" class="mt">
            <template v-if="d.view.emby.missing?.length">
              这个库按分类进了 Emby，但还有分类目录不在任何 Emby 媒体库里：<code v-for="m in d.view.emby.missing" :key="m" class="miss-dir">{{ m }}</code>。在 Emby 里给它们建库（或加进已有的库），这些分类里的片才会出现在 Emby。
            </template>
            <template v-else>
              {{ d.view.emby.partial ? 'Emby 只覆盖了这个库的一部分目录' : 'Emby 里还没有覆盖这个库的媒体库' }}<template v-if="d.view.emby.suggest">。要整库进 Emby，在 Emby 里新建媒体库，路径选 <code>{{ d.view.emby.suggest }}</code></template>。
            </template>
          </HAlert>
        </template>
      </article>
    </div>

    <HAlert v-if="view.orphan_rules.length" status="danger" title="有分类规则指向认不出的库" class="mt">
      <span v-for="r in view.orphan_rules" :key="`${r.media_type}:${r.name}`" class="orphan">
        {{ r.name }} 写的是「{{ r.library }}」<template v-if="view.rename_fixes.find((f) => f.from === r.library)">（看起来是「{{ view.rename_fixes.find((f) => f.from === r.library)!.to }}」在 115 上改了名）</template>；
      </span>
      整理到这几类时会停下。
      <template #actions>
        <HButton
          v-for="f in view.rename_fixes"
          :key="f.from"
          size="sm"
          variant="secondary"
          :loading="fixing === f.from"
          @click="fixRename(f.from, f.to)"
        >
          把「{{ f.from }}」改成「{{ f.to }}」
        </HButton>
        <RouterLink :to="CATEGORY_LINK" class="cat-link">去二级分类修改 →</RouterLink>
      </template>
    </HAlert>

    <div class="add-row">
      <HButton size="sm" variant="secondary" @click="addLib"><Plus :size="14" />添加媒体库</HButton>
      <span v-if="!multi" class="muted small">想把动漫、4K，或者自己整理好的老库分开放进另一个 115 目录？</span>
    </div>

    <FormActions>
      <HButton variant="primary" :loading="saving" :disabled="!dirty" @click="save">保存</HButton>
      <HButton variant="tertiary" :disabled="!dirty || saving" @click="discard">放弃修改</HButton>
    </FormActions>
  </SectionCard>

  <HModal v-model:show="rm.show" :title="`移除「${rm.lib ? libLabel(rm.lib) : ''}」`" width="480px">
    <ul v-if="rm.lib?.view" class="rm-list">
      <li v-if="rm.lib.view.rules.length">
        {{ rm.lib.view.rules.map((r) => r.name).join('、') }} 改回「{{ defaultName }}」（二级分类里的 library 会被删掉）
      </li>
      <li v-for="b in rm.lib.view.inboxes" :key="b.cid">
        转存入口「{{ b.name }}」解绑；子目录本身和里面的东西不删，引用它的订阅改为转存到转存目录本身
      </li>
      <li>115 上的「{{ rm.lib.view.path || rm.lib.cid }}」不动</li>
    </ul>
    <label class="check-row">
      <HCheckbox v-model:checked="rm.clean" />
      <span>同时清理本地的 STRM 与台账<template v-if="rm.lib?.view?.local">（{{ rm.lib.view.local }}）</template>；Emby 里对应的媒体库请自己删除</span>
    </label>
    <template #footer>
      <HButton variant="tertiary" @click="rm.show = false">取消</HButton>
      <HButton variant="danger" :loading="saving" @click="confirmRemove">移除</HButton>
    </template>
  </HModal>
</template>

<style scoped>
.add-hint {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 10px 14px;
  border: 1px dashed var(--border);
  border-radius: var(--r-lg);
  font-size: 13px;
  color: var(--muted);
}
.cards {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.lib {
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  background: var(--surface);
}
.lib-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.lib-head h3 {
  margin: 0;
  font-size: 15px;
}
.tag-rest,
.tag-new {
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 12px;
}
.tag-rest {
  background: var(--accent-soft);
  color: var(--accent);
}
.tag-new {
  background: var(--surface-secondary);
  color: var(--muted);
}
.grow {
  flex: 1;
}
.lib-path {
  margin: 4px 0 10px;
  font-size: 12px;
  color: var(--muted);
  overflow-wrap: anywhere;
}
.sep {
  margin: 0 6px;
  color: var(--muted);
}
.row {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr);
  gap: 8px;
  padding: 8px 0;
  border-top: 1px solid var(--separator, var(--border));
}
.row-label {
  padding-top: 3px;
  font-size: 13px;
  color: var(--muted);
}
.cat-line {
  margin: 2px 0 0;
  font-size: 13px;
  line-height: 1.9;
}
.cat-link {
  display: inline-block;
  margin-top: 2px;
  font-size: 12.5px;
  color: var(--accent);
  white-space: nowrap;
}
.muted {
  margin: 4px 0 0;
  color: var(--muted);
}
.small {
  font-size: 12px;
}
.inbox-line {
  display: flex;
  align-items: center;
  gap: 8px;
}
.inbox {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
  font-size: 13px;
}
.inbox-new {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
}
.inbox-new .muted {
  margin: 0;
}
.inbox-name {
  width: 160px;
}
.x {
  display: inline-grid;
  place-items: center;
  width: 18px;
  height: 18px;
  border: 0;
  border-radius: 999px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
}
.x:hover {
  background: var(--surface-secondary);
  color: var(--foreground);
}
.warn-line {
  display: inline-flex;
  align-items: flex-start;
  gap: 4px;
  margin: 2px 0 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--warning);
}
.warn-line svg {
  flex: none;
  margin-top: 4px;
}
.btn-link {
  margin-left: 8px;
  padding: 0;
  border: 0;
  background: none;
  cursor: pointer;
}
.depth {
  width: 240px;
  margin-bottom: 4px;
}
.titles {
  margin: 6px 0 0;
  padding: 8px 10px;
  max-height: 280px;
  overflow: auto;
  list-style: none;
  border-radius: var(--r-sm);
  background: var(--surface-secondary);
  font-size: 12.5px;
}
.titles li {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 0;
}
.t-name {
  flex: none;
}
.titles li.odd .t-name,
.t-odd {
  color: var(--warning);
}
.t-path,
.t-odd {
  margin: 0 0 0 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.miss-dir + .miss-dir {
  margin-left: 6px;
}
.problem {
  flex-basis: 100%;
  display: flex;
  align-items: center;
  gap: 4px;
  margin: 0;
  font-size: 12px;
  color: var(--danger);
}
.orphan {
  margin-right: 4px;
}
.add-row {
  margin-top: 12px;
}
.mt {
  margin-top: 10px;
}
.mb {
  margin-bottom: 10px;
}
.rm-list {
  margin: 0 0 12px;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.8;
}
.check-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 13px;
  cursor: pointer;
}
@media (max-width: 720px) {
  .row {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
