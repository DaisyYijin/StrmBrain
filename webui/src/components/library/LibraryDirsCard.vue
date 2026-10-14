<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ArrowDown, ArrowUp, Download, GripVertical, Pencil, Plus, Trash2, Upload } from '@lucide/vue'
import HAlert from '@/components/hero/HAlert.vue'
import HButton from '@/components/hero/HButton.vue'
import HChip from '@/components/hero/HChip.vue'
import HModal from '@/components/hero/HModal.vue'
import HSpinner from '@/components/hero/HSpinner.vue'
import HSwitch from '@/components/hero/HSwitch.vue'
import SectionCard from '@/components/ui/SectionCard.vue'
import FormActions from '@/components/ui/FormActions.vue'
import LibraryDirDialog from './LibraryDirDialog.vue'
import { libraryApi } from '@/api'
import type { LibDirCheck, LibDirLib, LibraryDir, LibraryDirView, LibraryDirsReply } from '@/api/library'
import { ApiError } from '@/api/client'
import { toastError, useFeedback } from '@/composables/useFeedback'
import { LIB_DIR_TYPE_LABEL, emptyLibDir, libDirConds, libDirLabel } from './libDirs'

/**
 * 媒体库目录（后端 libdirs*.go，设计见 docs/115-station-notes/LIBRARY-DIRS-PLAN.md）。
 *
 * 一张表管两件事：海报墙 / 刮削按它认片目（表里的目录，下一层是片目），整理按它挑去处（从上往下第一个类型和条件都匹配的）。
 * 取代原来的「二级分类策略」与「其他媒体库目录」。改动先攒在本地，点保存整表提交（后端校验兜底、重复等）。
 */
const { message, dialog } = useFeedback()
const route = useRoute()

const loading = ref(true)
const saving = ref(false)
const data = ref<LibraryDirsReply | null>(null)
const rows = ref<LibraryDirView[]>([])
const check = ref<LibDirCheck>({ errors: [], warnings: [] })
const dirty = ref(false)

const libs = computed<LibDirLib[]>(() => data.value?.libs ?? [])
const localRoot = computed(() => {
  const r = rows.value.find((x) => x.local && x.lib_name)
  if (!r) return ''
  const tail = [r.lib_name, r.path].filter(Boolean).join('/')
  return r.local.endsWith(tail) ? r.local.slice(0, r.local.length - tail.length).replace(/\/+$/, '') : ''
})

async function load() {
  loading.value = true
  try {
    const d = await libraryApi.getLibraryDirs()
    data.value = d
    rows.value = (d.rows ?? []).map((r) => ({ ...r }))
    check.value = d.check ?? { errors: [], warnings: [] }
    dirty.value = false
  } catch (e) {
    toastError(e, '读取媒体库目录失败')
  } finally {
    loading.value = false
  }
}

function touch() {
  dirty.value = true
}

// ---- 排序：拖动（桌面）+ 上下箭头（手机、键盘） ----
const dragFrom = ref<number | null>(null)
const dragOver = ref<number | null>(null)
function move(from: number, to: number) {
  if (from === to || to < 0 || to >= rows.value.length) return
  const [r] = rows.value.splice(from, 1)
  rows.value.splice(to, 0, r)
  touch()
}
function onDrop(i: number) {
  if (dragFrom.value !== null) move(dragFrom.value, i)
  dragFrom.value = dragOver.value = null
}

// ---- 编辑 ----
const editIndex = ref<number | null>(null)
const editRow = ref<LibraryDir | null>(null)
const showEdit = ref(false)
function openNew(seed?: Partial<LibraryDir>) {
  editIndex.value = null
  editRow.value = seed ? { ...emptyLibDir(), ...seed } : null
  showEdit.value = true
}
function openEdit(i: number) {
  editIndex.value = i
  editRow.value = { ...rows.value[i] }
  showEdit.value = true
}
function onSave(r: LibraryDir) {
  const view: LibraryDirView = { titles: 0, local: '', lib_missing: false, ...r } as LibraryDirView
  if (editIndex.value === null) {
    rows.value.push(view)
  } else {
    const old = rows.value[editIndex.value]
    const sameDir = old.lib_cid === r.lib_cid && old.path === r.path
    rows.value[editIndex.value] = { ...view, titles: sameDir ? old.titles : 0, local: sameDir ? old.local : '' }
  }
  touch()
}

async function remove(i: number) {
  const r = rows.value[i]
  if (r.titles > 0) {
    // 下面还有片：删了它们就从海报墙消失。多数时候用户要的是「不再往这里整理」
    const v = await dialog.confirm({
      title: '这个目录下还有片目',
      content: `「${libDirLabel(r)}」下面认出了 ${r.titles} 部。删掉这一行，它们会从海报墙上消失（文件不动）。只是不想再往这里整理的话，改成「仅识别」。`,
      actions: [
        { label: '取消', value: '', variant: 'tertiary' },
        { label: '删除', value: 'del', variant: 'danger' },
        ...(r.organize ? [{ label: '改成仅识别', value: 'keep', variant: 'primary' as const }] : []),
      ],
    })
    if (v === 'keep') {
      r.organize = false
      r.fallback = false
      touch()
      return
    }
    if (v !== 'del') return
  }
  rows.value.splice(i, 1)
  touch()
}

function toggleOrganize(r: LibraryDirView, v: boolean) {
  r.organize = v
  if (!v) r.fallback = false
  touch()
}

async function save() {
  saving.value = true
  try {
    const d = await libraryApi.saveLibraryDirs(rows.value)
    rows.value = d.rows.map((r) => ({ ...r }))
    check.value = d.check
    dirty.value = false
    message.success('已保存：海报墙与整理都按新的目录表来')
  } catch (e) {
    toastError(e, '保存失败')
  } finally {
    saving.value = false
  }
}

// ---- 认不出的目录：一键加为仅识别（填进编辑框让用户确认类型） ----
const unclaimed = computed(() => data.value?.unclaimed)
function addUnclaimed(p: string) {
  const [libName, ...rest] = p.split('/')
  const lib = libs.value.find((l) => l.name === libName)
  if (!lib) return
  openNew({ lib_cid: lib.cid, lib_name: lib.name, path: rest.join('/'), organize: false, media_type: 'all' })
  // 认不出的视频是按「库 + 前两段」聚合的，不一定正好是分类那一层
  message.info('确认一下层级：登记的目录的下一层才是一部片，需要的话在弹窗里改成上一层')
}

// ---- 导入 / 导出（老分类规则 YAML） ----
const yamlShow = ref(false)
const yamlMode = ref<'export' | 'import'>('export')
const yamlText = ref('')
const yamlBusy = ref(false)
async function openExport() {
  if (dirty.value) {
    message.warning('有没保存的改动：导出的是已保存的版本')
  }
  try {
    yamlText.value = (await libraryApi.exportLibraryDirs()).yaml
    yamlMode.value = 'export'
    yamlShow.value = true
  } catch (e) {
    toastError(e, '导出失败')
  }
}
function openImport() {
  yamlText.value = ''
  yamlMode.value = 'import'
  yamlShow.value = true
}
async function doImport() {
  yamlBusy.value = true
  try {
    const d = await libraryApi.importLibraryDirs(yamlText.value)
    // 导入替换「参与整理」的行；仅识别的行（YAML 表达不了）原样留在后面
    rows.value = [...d.rows, ...rows.value.filter((r) => !r.organize)]
    touch()
    yamlShow.value = false
    message.success(`已按 YAML 换成 ${d.rows.length} 行参与整理的目录，看过后点保存`)
    if (d.notes?.length) message.warning(d.notes.join('；'))
  } catch (e) {
    toastError(e, e instanceof ApiError ? e.message : '导入失败')
  } finally {
    yamlBusy.value = false
  }
}
async function copyYaml() {
  try {
    await navigator.clipboard.writeText(yamlText.value)
    message.success('已复制')
  } catch {
    message.warning('复制失败，请手动选中复制')
  }
}

onMounted(async () => {
  await load()
  if (route.hash === '#library-dirs') {
    await nextTick()
    document.getElementById('library-dirs')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
})
</script>

<template>
  <SectionCard id="library-dirs" title="媒体库目录" hint="认片目 · 整理去处 · 顺序就是优先级">
    <template #extra>
      <div v-if="data?.active" class="head-actions">
        <HButton variant="ghost" size="sm" @click="openImport"><Upload :size="14" />导入</HButton>
        <HButton variant="ghost" size="sm" @click="openExport"><Download :size="14" />导出</HButton>
      </div>
    </template>
    <p class="lead">
      海报墙和刮削认这里的目录：<b>目录的下一层是一部片</b>（电影目录里直接放的视频一个算一部）。
      整理新片时从上往下找第一个类型和条件都匹配、且「参与整理」的目录；都不匹配就放进第一个兜底目录。
      本地 STRM 目录跟着 115 的位置走。改了条件或顺序，已经入库的片不会自动挪，要挪用「重新整理」。
    </p>

    <div v-if="loading && !data" class="muted"><HSpinner size="sm" /> 读取中…</div>
    <HAlert v-else-if="data && !data.active" status="warning" title="还没启用">
      {{ data.reason || '暂时无法启用' }}。在那之前整理和海报墙继续用「自动整理 → 二级分类策略」里的分类规则。
      <template #actions><HButton size="sm" variant="tertiary" @click="load">再试一次</HButton></template>
    </HAlert>
    <template v-else-if="data">
      <HAlert v-if="check.errors.length" status="danger" class="msg">
        <div v-for="e in check.errors" :key="e">{{ e }}</div>
      </HAlert>
      <HAlert v-if="check.warnings.length" status="warning" class="msg">
        <div v-for="w in check.warnings" :key="w">{{ w }}</div>
      </HAlert>

      <ol class="rows">
        <li
          v-for="(r, i) in rows"
          :key="`${r.id ?? 'n'}-${i}`"
          class="row"
          :class="{ over: dragOver === i && dragFrom !== i, off: !r.organize }"
          @dragover.prevent="dragOver = i"
          @drop.prevent="onDrop(i)"
        >
          <span class="grip" draggable="true" title="拖动调整顺序" @dragstart="dragFrom = i" @dragend="dragFrom = dragOver = null">
            <GripVertical :size="15" />
          </span>
          <span class="no">{{ i + 1 }}</span>
          <div class="main">
            <div class="line1">
              <code class="dir" :title="libDirLabel(r)">{{ libDirLabel(r) }}</code>
              <HChip size="sm">{{ LIB_DIR_TYPE_LABEL[r.media_type] }}</HChip>
              <HChip v-if="r.fallback" size="sm" color="accent">兜底</HChip>
              <HChip v-if="!r.organize" size="sm" color="default" variant="tertiary">仅识别</HChip>
              <span class="cnt" title="这个目录下认出的片目">{{ r.titles }} 部</span>
            </div>
            <div class="conds">
              <template v-if="r.organize">
                <span v-for="c in libDirConds(r)" :key="c" class="cond">{{ c }}</span>
                <span v-if="!libDirConds(r).length" class="cond muted">{{ r.fallback ? '条件都不匹配时接住' : '不限条件' }}</span>
              </template>
              <span v-if="r.note" class="cond muted">{{ r.note }}</span>
            </div>
            <div v-if="r.local" class="local">本地：{{ r.local }}</div>
            <div v-if="r.lib_missing" class="warn">所属的媒体库不在配置里：不参与识别，整理选中它时会报错</div>
          </div>
          <div class="ops">
            <HSwitch :model-value="r.organize" size="sm" aria-label="参与整理" title="参与整理" @update:model-value="toggleOrganize(r, $event)" />
            <HButton variant="ghost" size="sm" icon-only aria-label="上移" :disabled="i === 0" @click="move(i, i - 1)"><ArrowUp :size="14" /></HButton>
            <HButton variant="ghost" size="sm" icon-only aria-label="下移" :disabled="i === rows.length - 1" @click="move(i, i + 1)"><ArrowDown :size="14" /></HButton>
            <HButton variant="ghost" size="sm" icon-only aria-label="修改" @click="openEdit(i)"><Pencil :size="14" /></HButton>
            <HButton variant="ghost" size="sm" icon-only aria-label="删除" @click="remove(i)"><Trash2 :size="14" /></HButton>
          </div>
        </li>
      </ol>
      <p v-if="!rows.length" class="muted">还没有目录。</p>
      <HButton variant="secondary" size="sm" @click="openNew()"><Plus :size="14" />添加目录</HButton>

      <div v-if="unclaimed?.videos" class="unclaimed">
        <span class="muted">台账里还有 <b class="warnc">{{ unclaimed.videos }}</b> 个视频不属于任何目录，在：</span>
        <button
          v-for="d in unclaimed.dirs"
          :key="d.path"
          type="button"
          class="udir"
          :disabled="!d.path.includes('/') || !libs.some((l) => d.path.startsWith(`${l.name}/`))"
          title="加为仅识别的目录（可在弹窗里改成上一层或下一层）"
          @click="addUnclaimed(d.path)"
        >
          {{ d.path || '（本地根目录）' }} <span class="muted">{{ d.videos }}</span>
        </button>
      </div>

      <FormActions v-if="dirty">
        <HButton variant="primary" :loading="saving" @click="save">保存</HButton>
        <HButton variant="tertiary" @click="load">放弃修改</HButton>
      </FormActions>
    </template>

    <LibraryDirDialog v-model:show="showEdit" :row="editRow" :libs="libs" :local-root="localRoot" @save="onSave" />

    <HModal v-model:show="yamlShow" :title="yamlMode === 'export' ? '导出（分类规则 YAML）' : '从分类规则 YAML 导入'" width="720px">
      <p class="muted yaml-note">
        <template v-if="yamlMode === 'export'">参与整理的目录导出成老分类规则的格式；仅识别的目录与「带条件的兜底」写成注释（YAML 表达不了）。</template>
        <template v-else>贴入老分类规则 YAML：会替换表里所有「参与整理」的目录，仅识别的保留。导入后先看一遍再保存。</template>
      </p>
      <textarea v-model="yamlText" class="yaml" :readonly="yamlMode === 'export'" spellcheck="false" />
      <template #footer>
        <HButton variant="tertiary" @click="yamlShow = false">关闭</HButton>
        <HButton v-if="yamlMode === 'export'" variant="primary" @click="copyYaml">复制</HButton>
        <HButton v-else variant="primary" :loading="yamlBusy" :disabled="!yamlText.trim()" @click="doImport">导入</HButton>
      </template>
    </HModal>
  </SectionCard>
</template>

<style scoped>
.lead {
  margin: 0 0 12px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--muted);
}
.muted {
  color: var(--muted);
}
.msg {
  margin-bottom: 10px;
}
.head-actions {
  display: flex;
  gap: 4px;
}
.rows {
  margin: 0 0 10px;
  padding: 0;
  list-style: none;
}
.row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 4px;
  border-bottom: 1px solid var(--separator);
  border-top: 2px solid transparent;
}
.row.over {
  border-top-color: var(--accent);
}
.row.off .dir {
  color: var(--muted);
}
.grip {
  flex: none;
  padding-top: 2px;
  color: var(--muted);
  cursor: grab;
}
.no {
  flex: none;
  width: 22px;
  padding-top: 1px;
  font-size: 12px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.line1 {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 8px;
}
.dir {
  font-size: 13px;
  word-break: break-all;
}
.cnt {
  font-size: 12px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.conds {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  font-size: 12px;
}
.local {
  font-size: 11.5px;
  color: var(--muted);
  word-break: break-all;
}
.warn {
  font-size: 12px;
  color: var(--warning);
}
.warnc {
  color: var(--warning);
}
.ops {
  flex: none;
  display: flex;
  align-items: center;
  gap: 2px;
}
.unclaimed {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 14px;
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
.yaml-note {
  margin: 0 0 8px;
  font-size: 12.5px;
}
.yaml {
  width: 100%;
  min-height: 320px;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  background: var(--surface-secondary);
  color: var(--foreground);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12.5px;
  resize: vertical;
}
@media (max-width: 720px) {
  .row {
    flex-wrap: wrap;
  }
  .grip {
    display: none;
  }
  .ops {
    width: 100%;
    justify-content: flex-end;
  }
}
</style>
