<script setup lang="ts">
/**
 * 批量添加媒体库：选一个 115 上级目录，列出它下面的子目录，勾几个就建几个库。
 * 名称 / 本地目录默认取目录名，转存/待整理文件夹默认 /最近接收/库名（后端按路径找，没有就建）；
 * 想用别的名字的转存文件夹，清空后到单个库的「修改」里自己选。
 * 账号、分类方案、刮削各库相同，一次设好。存一次全成或全不成（后端 libapiv2.go 的 CreateMediaLibsBatch）。
 */
import { computed, ref, watch } from 'vue'
import HButton from '@/components/hero/HButton.vue'
import HCheckbox from '@/components/hero/HCheckbox.vue'
import HInput from '@/components/hero/HInput.vue'
import HModal from '@/components/hero/HModal.vue'
import HSegmented from '@/components/hero/HSegmented.vue'
import HSelect from '@/components/hero/HSelect.vue'
import FieldRow from '@/components/ui/FieldRow.vue'
import Cid115Input, { type CidValue } from '@/components/Cid115Input.vue'
import { libraryApi, storageApi } from '@/api'
import type { MediaLibReq, MediaLibsView } from '@/api/library'
import type { LibScrape } from '@/api/organize'
import { toastError, useFeedback } from '@/composables/useFeedback'

const props = defineProps<{ view: MediaLibsView }>()
const show = defineModel<boolean>('show', { default: false })
const emit = defineEmits<{ saved: [MediaLibsView] }>()
const { message } = useFeedback()

/** 与后端 libBatchMax 一致 */
const BATCH_MAX = 30
const STAGING_BASE = '/最近接收'

interface Row {
  cid: string
  dir: string
  picked: boolean
  name: string
  local: string
  staging: string
  /** 用户动过就不再跟着名称变 */
  localTouched: boolean
  stagingTouched: boolean
  /** 已经是某个库的目录：不能再选 */
  taken: string
}

const account = ref(0)
const parent = ref<CidValue>({ cid: '', path: '' })
const rows = ref<Row[]>([])
const loading = ref(false)
const loadErr = ref('')
const schemeId = ref(0)
const depth = ref(1)
const scrape = ref<LibScrape>('manual')
const saving = ref(false)

watch(show, (v) => {
  if (!v) return
  account.value = 0
  parent.value = { cid: '', path: '' }
  rows.value = []
  loadErr.value = ''
  schemeId.value = props.view.schemes[0]?.id ?? 0
  depth.value = 1
  scrape.value = 'manual'
})

const accountOpts = computed(() => props.view.accounts.map((a) => ({ label: a.main ? `${a.name}（主账号）` : a.name, value: a.id })))
const showAccount = computed(() => props.view.accounts.length > 1)
const schemeOpts = computed(() => [
  { label: '不分类（片目直接放在库目录下）', value: 0 },
  ...props.view.schemes.map((s) => ({ label: `${s.name}（电影 ${s.movie} / 剧集 ${s.tv} 条）`, value: s.id })),
])
const SCRAPE_OPTS = [
  { label: '自动刮新片', value: 'auto' },
  { label: '只手动刮', value: 'manual' },
  { label: '不刮', value: 'off' },
]
const DEPTH_OPTS = [
  { label: '库 / 片目', value: 1 },
  { label: '库 / 分类 / 片目', value: 2 },
  { label: '库 / 分类 / 子分类 / 片目', value: 3 },
]

function setAccount(v: number) {
  if (v === account.value) return
  account.value = v
  // 换了账号：目录都是那个账号里的
  parent.value = { cid: '', path: '' }
  rows.value = []
}

/** 选好（解析出 cid）上级目录就列子目录 */
watch(
  () => parent.value.cid,
  (cid) => {
    if (cid && cid !== '0') void loadDirs(cid)
    else rows.value = []
  },
)

async function loadDirs(cid: string) {
  loading.value = true
  loadErr.value = ''
  try {
    const r = await storageApi.dirs115(cid, false, account.value || undefined)
    const used = new Map(props.view.libs.filter((l) => l.account === account.value).map((l) => [l.cid, l.name]))
    rows.value = (r.data ?? [])
      .filter((d) => d.cid)
      .map((d) => {
        const taken = used.get(d.cid!) ?? ''
        return {
          cid: d.cid!,
          dir: d.name,
          picked: false,
          name: d.name,
          local: d.name,
          staging: `${STAGING_BASE}/${d.name}`,
          localTouched: false,
          stagingTouched: false,
          taken: taken ? `已是媒体库「${taken}」` : '',
        }
      })
    if (!rows.value.length) loadErr.value = '这个目录下没有子目录'
  } catch (e) {
    rows.value = []
    loadErr.value = '读取子目录失败'
    toastError(e, '读取子目录失败')
  } finally {
    loading.value = false
  }
}

function setName(r: Row, v: string) {
  r.name = v
  const base = v.trim()
  if (!r.localTouched) r.local = base
  if (!r.stagingTouched) r.staging = base ? `${STAGING_BASE}/${base}` : ''
}

const selectable = computed(() => rows.value.filter((r) => !r.taken))
const picked = computed(() => rows.value.filter((r) => r.picked && !r.taken))
const allPicked = computed(() => !!selectable.value.length && picked.value.length === selectable.value.length)
function toggleAll(v: boolean) {
  for (const r of selectable.value) r.picked = v
}

const parentPath = computed(() => (/^\d+$/.test(parent.value.path) ? '' : parent.value.path.replace(/\/+$/, '')))

async function submit() {
  if (!picked.value.length) return
  if (picked.value.length > BATCH_MAX) {
    message.error(`一次最多添加 ${BATCH_MAX} 个媒体库`)
    return
  }
  const bad = picked.value.find((r) => !r.name.trim() || !r.local.trim())
  if (bad) {
    message.error(`「${bad.dir}」的名称和本地目录不能空`)
    return
  }
  const libs: MediaLibReq[] = picked.value.map((r) => ({
    name: r.name.trim(),
    account: account.value,
    cid: r.cid,
    path: parentPath.value ? `${parentPath.value}/${r.dir}` : '',
    local: r.local.trim(),
    staging_cid: '',
    staging_path: r.staging.trim(),
    scheme_id: schemeId.value,
    depth: schemeId.value ? 0 : depth.value,
    scrape: scrape.value,
  }))
  saving.value = true
  try {
    const v = await libraryApi.createMediaLibsBatch(libs)
    message.success(`已添加 ${libs.length} 个媒体库：跑一次全量同步就能把它们的 STRM 生成出来`)
    emit('saved', v)
    show.value = false
  } catch (e) {
    toastError(e, '批量添加失败')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <HModal v-model:show="show" title="批量添加媒体库" width="880px" persistent>
    <div class="form">
      <FieldRow v-if="showAccount" label="账号" tip="这几个库都在这个 115 账号上。">
        <HSelect :model-value="account" :options="accountOpts" aria-label="账号" @update:model-value="(v) => setAccount(Number(v))" />
      </FieldRow>

      <FieldRow label="115 上级目录" required tip="选一个目录（如 /影视），它下面的每个子目录可以建成一个库。">
        <Cid115Input :key="`parent-${account}`" v-model="parent" :account="account || undefined" placeholder="如 /影视" />
      </FieldRow>

      <div v-if="loading" class="hint">正在读取子目录…</div>
      <div v-else-if="loadErr" class="hint">{{ loadErr }}</div>
      <div v-else-if="rows.length" class="table" role="table" aria-label="子目录">
        <div class="tr th" role="row">
          <span class="c-check">
            <HCheckbox
              :checked="allPicked"
              :indeterminate="!!picked.length && !allPicked"
              :disabled="!selectable.length"
              aria-label="全选"
              @update:checked="toggleAll"
            />
          </span>
          <span>子目录</span>
          <span>名称</span>
          <span>本地目录（{{ view.media_root }}/ 下）</span>
          <span>转存/待整理文件夹</span>
        </div>
        <div v-for="r in rows" :key="r.cid" class="tr" :class="{ 'is-taken': r.taken, 'is-off': !r.picked }" role="row">
          <span class="c-check">
            <HCheckbox v-model:checked="r.picked" :disabled="!!r.taken" :aria-label="`选择 ${r.dir}`" />
          </span>
          <span class="c-dir" :title="r.dir">
            {{ r.dir }}
            <small v-if="r.taken">{{ r.taken }}</small>
          </span>
          <template v-if="!r.taken">
            <HInput
              :model-value="r.name"
              :disabled="!r.picked"
              :input-attrs="{ 'aria-label': `${r.dir} 的名称` }"
              @update:model-value="(v: string) => setName(r, v)"
            />
            <HInput
              :model-value="r.local"
              :disabled="!r.picked"
              :input-attrs="{ 'aria-label': `${r.dir} 的本地目录` }"
              @update:model-value="(v: string) => ((r.local = v), (r.localTouched = true))"
            />
            <HInput
              :model-value="r.staging"
              :disabled="!r.picked"
              placeholder="留空 = 只同步"
              :input-attrs="{ 'aria-label': `${r.dir} 的转存文件夹` }"
              @update:model-value="(v: string) => ((r.staging = v), (r.stagingTouched = true))"
            />
          </template>
        </div>
      </div>
      <p v-if="rows.length" class="hint">
        转存/待整理文件夹默认 {{ STAGING_BASE }}/库名，115 上没有就自动建；留空 = 只同步 STRM。想用别的文件夹，留空后在库卡片的「修改」里选。
      </p>

      <FieldRow label="二级分类" tip="这几个库整理时都按这套分类方案分目录。不分类 = 片目直接放在库目录下。">
        <HSelect v-model="schemeId" :options="schemeOpts" aria-label="分类方案" />
      </FieldRow>
      <FieldRow v-if="!schemeId" label="片目在" tip="海报墙、刮削按这一层认「一部片」。">
        <HSelect v-model="depth" :options="DEPTH_OPTS" aria-label="片目在第几层" />
      </FieldRow>
      <FieldRow label="刮削" tip="自动刮新片：整理进来、同步进来的新片都刮；只手动刮：在海报墙勾选后刮；不刮：交给 Emby。">
        <HSegmented :model-value="scrape" :options="SCRAPE_OPTS" size="sm" @update:model-value="(v) => (scrape = v as LibScrape)" />
      </FieldRow>
    </div>
    <template #footer>
      <HButton variant="tertiary" @click="show = false">取消</HButton>
      <HButton variant="primary" :loading="saving" :disabled="!picked.length" @click="submit">
        {{ picked.length ? `添加 ${picked.length} 个媒体库` : '添加' }}
      </HButton>
    </template>
  </HModal>
</template>

<style scoped>
.form {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.hint {
  margin: 4px 0 8px;
  font-size: 12.5px;
  color: var(--muted);
}
.table {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 4px 0;
  max-height: 46vh;
  overflow-y: auto;
}
.tr {
  display: grid;
  grid-template-columns: 28px minmax(90px, 0.9fr) minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1.3fr);
  align-items: center;
  gap: 8px;
  padding: 4px 6px;
  border-radius: var(--r-sm);
}
.th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--overlay, var(--surface));
  font-size: 12px;
  color: var(--muted);
}
.tr:not(.th):hover {
  background: var(--surface-secondary, var(--default));
}
.c-check {
  display: flex;
  justify-content: center;
}
.c-dir {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 600;
  font-size: 13px;
}
.c-dir small {
  margin-left: 6px;
  font-weight: 400;
  color: var(--muted);
}
.is-taken .c-dir {
  grid-column: 2 / -1;
  color: var(--muted);
}
.is-off:not(.is-taken) .c-dir {
  color: var(--muted);
}

@media (max-width: 720px) {
  .th {
    display: none;
  }
  .tr {
    grid-template-columns: 28px minmax(0, 1fr);
  }
  .tr > :deep(*:nth-child(n + 3)) {
    grid-column: 2;
  }
}
</style>
