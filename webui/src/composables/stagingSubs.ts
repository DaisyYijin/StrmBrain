import { computed, ref } from 'vue'
import { transferApi } from '@/api'
import type { LibChoice, StagingLib } from '@/api/transfer'

/**
 * 转存子目录绑定（按来源分库）：设置页的绑定表、找资源 / 链接转存的「转存到」、订阅弹窗共用一份。
 * 在设置页改了绑定，切回找资源马上看得到，不用刷新。
 */
const items = ref<StagingLib[]>([])
const libs = ref<LibChoice[]>([])
const loaded = ref(false)
/**
 * 媒体库表（V2，后端 xfertarget.go）：「转存到」选的是库（值 lib:<id>），不再有转存子目录。
 * 空值 = 默认转存库（只有一个库时就是它）；多个库又没设默认时 needPick，每次都要选
 */
const v2 = ref(false)
const defaultLib = ref(0)
const needPick = ref(false)
let pending: Promise<void> | null = null

/** 上次选的「转存到」，只是本机的便利，读写失败当作没有 */
const PICK_KEY = 'transfer.subdir'
function readPick(): string {
  try {
    return localStorage.getItem(PICK_KEY) ?? ''
  } catch {
    return ''
  }
}
const pick = ref(readPick())

async function reload() {
  pending ??= transferApi
    .stagingLibs()
    .then((r) => setAll(r))
    .catch(() => {
      // 读不到当作没有绑定：下拉不显示，提交照旧进转存目录
    })
    .finally(() => {
      pending = null
    })
  return pending
}

function setAll(r: { items: StagingLib[]; libs: LibChoice[]; v2?: boolean; default?: number; need_pick?: boolean }) {
  items.value = r.items
  libs.value = r.libs
  v2.value = !!r.v2
  defaultLib.value = r.default ?? 0
  needPick.value = !!r.need_pick
  loaded.value = true
}

function setPick(v: string) {
  pick.value = v
  try {
    localStorage.setItem(PICK_KEY, v)
  } catch {
    /* 存不了就只在这次会话里记着 */
  }
}

/**
 * 一个入口在下拉里的名字：用户选的是「进哪个库」，所以写库名（2026-10-15 起，MULTI-LIB-SIMPLIFY-PLAN.md §2.2）。
 * 入口子目录名和库名不一样、或者一个库开了几个入口时，括号里补上子目录名，免得两项看着一样
 */
export function stagingSubLabel(s: StagingLib, all: StagingLib[] = items.value): string {
  if (v2.value) return s.name
  const lib = s.lib_name || '媒体库'
  const twins = all.filter((x) => x.lib === s.lib && !x.problem).length > 1
  return s.name !== lib || twins ? `${lib}（转存/${s.name}）` : lib
}

/** 能选的入口（失效的不给选），值是子目录 cid */
export function stagingSubChoices(opts?: { mainOnly?: boolean }): { label: string; value: string }[] {
  return items.value
    .filter((s) => !s.problem && (!opts?.mainOnly || !s.account))
    .map((s) => ({ label: stagingSubLabel(s), value: s.cid }))
}

/** 默认这一项的名字（V2：默认转存库叫什么） */
function defaultLabel(): string {
  if (!v2.value) return '转存目录（按分类选库）'
  const d = items.value.find((s) => s.cid === `lib:${defaultLib.value}`) ?? (items.value.length === 1 ? items.value[0] : undefined)
  return d ? `默认：${d.name}` : '选一个库'
}

/** 下拉选项：默认（老口径是转存目录本身，V2 是默认转存库）+ 各入口 / 各库。多个库又没设默认时没有「默认」这一项 */
const options = computed(() => [...(needPick.value ? [] : [{ label: defaultLabel(), value: '' }]), ...stagingSubChoices()])

/** 当前选的；绑定被删掉了就退回默认 */
const current = computed(() => (items.value.some((s) => s.cid === pick.value && !s.problem) ? pick.value : ''))

/** 下拉要不要出现：老口径有绑定才出；V2 有不止一个库才出（一个库没得选） */
const showPicker = computed(() => (v2.value ? items.value.length > 1 : items.value.length > 0))

export function useStagingSubs() {
  if (!loaded.value) reload()
  return { items, libs, loaded, options, current, reload, setAll, setPick, v2, needPick, showPicker }
}
