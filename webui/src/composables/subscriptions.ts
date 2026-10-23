import { computed, ref } from 'vue'
import * as subscribeApi from '@/api/subscribe'
import type { Subscription } from '@/api/subscribe'
import { ApiError } from '@/api/client'
import { toastError, useFeedback } from '@/composables/useFeedback'
import { useQueueStore } from '@/stores/queue'

/**
 * 订阅列表与详情抽屉的共享状态。订阅并进影视转存页之后，「找资源」「订阅」两个页签
 * 都要用它：找资源里选中一部片要知道订阅过没有、点了直接在原地打开详情抽屉，
 * 不再跳到另一个页面。模块级单例，页签切来切去不重复拉。
 */
const rows = ref<Subscription[]>([])
const loading = ref(false)
const loaded = ref(false)

async function load() {
  loading.value = true
  try {
    rows.value = (await subscribeApi.list()).data ?? []
    loaded.value = true
  } catch (e) {
    toastError(e, '读取订阅失败')
  } finally {
    loading.value = false
  }
}

const counts = computed(() => {
  const n = { all: rows.value.length, active: 0, stalled: 0, paused: 0, done: 0 }
  for (const r of rows.value) n[r.state]++
  return n
})

function findSub(tmdbId: number, type: string) {
  return rows.value.find((r) => r.tmdb_id === tmdbId && r.media_type === type) ?? null
}

// ---- 一键订阅：找资源海报上的「订阅」，不开弹窗，按默认值订（后端 subDefaultForm） ----
// 正在订的那几部（`类型:编号`），按钮转圈、防连点
const quickBusy = ref(new Set<string>())
async function quickSubscribe(tmdbId: number, type: 'movie' | 'tv') {
  const key = `${type}:${tmdbId}`
  if (quickBusy.value.has(key) || findSub(tmdbId, type)) return
  quickBusy.value = new Set(quickBusy.value).add(key)
  const { message } = useFeedback()
  try {
    const d = await subscribeApi.quickCreate(tmdbId, type)
    message.success(d.message || '已订阅')
    if (d.job_id) void useQueueStore().submitted(d.job_id)
  } catch (e) {
    // 别处已经订过（另一个页面、机器人）：刷新列表让按钮变成「已订阅」
    if (e instanceof ApiError && e.status === 409) message.info('这部已经订阅过了')
    else toastError(e, '订阅失败')
  } finally {
    const next = new Set(quickBusy.value)
    next.delete(key)
    quickBusy.value = next
    await load()
  }
}

// ---- 详情抽屉（页面挂一份，?sub= 打开） ----
const detailId = ref<number | null>(null)
const detailOpen = ref(false)
function openDetail(id: number) {
  detailId.value = id
  detailOpen.value = true
}

export function useSubscriptions() {
  return { rows, loading, loaded, load, counts, findSub, quickBusy, quickSubscribe, detailId, detailOpen, openDetail }
}
