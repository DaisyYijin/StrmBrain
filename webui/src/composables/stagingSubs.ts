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
    .then((r) => {
      items.value = r.items
      libs.value = r.libs
      loaded.value = true
    })
    .catch(() => {
      // 读不到当作没有绑定：下拉不显示，提交照旧进转存目录
    })
    .finally(() => {
      pending = null
    })
  return pending
}

function setAll(r: { items: StagingLib[]; libs: LibChoice[] }) {
  items.value = r.items
  libs.value = r.libs
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
  const lib = s.lib_name || '媒体库'
  const twins = all.filter((x) => x.lib === s.lib && !x.problem).length > 1
  return s.name !== lib || twins ? `${lib}（转存/${s.name}）` : lib
}

/** 能选的入口（失效的不给选），值是子目录 cid */
export function stagingSubChoices(): { label: string; value: string }[] {
  return items.value.filter((s) => !s.problem).map((s) => ({ label: stagingSubLabel(s), value: s.cid }))
}

/** 下拉选项：转存目录本身（按分类选库）+ 各入口 */
const options = computed(() => [{ label: '转存目录（按分类选库）', value: '' }, ...stagingSubChoices()])

/** 当前选的子目录；绑定被删掉了就退回转存目录 */
const current = computed(() => (items.value.some((s) => s.cid === pick.value && !s.problem) ? pick.value : ''))

export function useStagingSubs() {
  if (!loaded.value) reload()
  return { items, libs, loaded, options, current, reload, setAll, setPick }
}
