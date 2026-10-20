<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import StatusStrip from '@/components/ui/StatusStrip.vue'
import type { StripItem } from '@/components/ui/StatusStrip.vue'
import { syncApi, tasksApi } from '@/api'
import type { TaskJob } from '@/api/tasks'
import { loadIncrCfg } from '@/composables/incrSetting'
import { useQueueStore } from '@/stores/queue'
import { recordStats, refreshRecordStats } from '@/stores/recordStats'
import { JOB_STATUS, resultSummary } from '@/utils/jobStatus'
import { fullTime, relCronNext, relTime } from '@/utils/time'

/**
 * 自动整理页顶部的状态条（2026-10-20，参考 LitePan 每个任务页顶部先说「现在怎么样」）：
 * 这一页的页签全是配置，原来进来看不到下次几点整理、上一轮怎么样、有几条等确认。
 *
 * 数据全是已有的只读接口（setting `incr` / cron 预览 / 任务历史 / 记录条数），零 115 请求。
 * cron 只看已保存的：基础配置里改了没保存的还不会生效，保存后切页签回来再读。
 */
const emit = defineEmits<{ goto: [tab: string] }>()
const router = useRouter()
const queue = useQueueStore()

/** 自动整理的两种任务：定时 / 手动「开始整理」与转存后整理。空转的轮次跑完就删，不进历史 */
const ORG_KINDS = ['organize', 'transfer']

const cron = ref<string | undefined>(undefined)
const nextAt = ref('')
const lastJob = ref<TaskJob | null | undefined>(undefined)

async function loadCron() {
  try {
    cron.value = (await loadIncrCfg()).cron.trim()
    nextAt.value = cron.value ? ((await syncApi.cronPreview(cron.value)).next?.[0] ?? '') : ''
  } catch {
    // cron 写错了就不显示下一次
    if (cron.value === undefined) cron.value = ''
    nextAt.value = ''
  }
}
async function loadLast() {
  try {
    lastJob.value = (await tasksApi.history({ kind: ORG_KINDS.join(','), page: 1, size: 1 })).data[0] ?? null
  } catch {
    lastJob.value = null
  }
}

onMounted(() => {
  void loadCron()
  void loadLast()
  void refreshRecordStats()
})
const offFinished = queue.onFinished((j) => {
  if (ORG_KINDS.includes(j.kind) || j.kind === 'confirm' || j.kind === 'ignore') {
    void loadLast()
    void refreshRecordStats()
  }
})
onUnmounted(offFinished)

const running = computed(() => queue.active.find((j) => ORG_KINDS.includes(j.kind)))

const items = computed<StripItem[]>(() => {
  const out: StripItem[] = []

  // ---- 定时整理 ----
  if (cron.value === undefined) {
    out.push({ key: 'cron', title: '定时整理', tone: 'loading', text: '' })
  } else if (!cron.value) {
    out.push({
      key: 'cron',
      title: '定时整理',
      tone: 'off',
      text: '未开启 · 转存目录照常每分钟接管',
      tip: '关掉定时只是不按点扫一遍；转存守望者每分钟照样接管转存目录里的新内容',
      action: '设置',
    })
  } else {
    out.push({
      key: 'cron',
      title: '定时整理',
      tone: 'ok',
      text: nextAt.value ? `下次 ${relCronNext(nextAt.value)}` : cron.value,
      tip: `cron：${cron.value}`,
      action: '设置',
    })
  }

  // ---- 上一轮 ----
  const j = lastJob.value
  if (running.value) {
    const r = running.value
    out.push({
      key: 'last',
      title: '整理',
      tone: 'ok',
      text:
        r.status === 'running'
          ? `执行中${r.progress?.total ? ` · ${r.progress.done}/${r.progress.total}` : ''}`
          : '排队中',
      tip: r.title,
      action: '查看',
    })
  } else if (j === undefined) {
    out.push({ key: 'last', title: '上一轮', tone: 'loading', text: '' })
  } else if (!j) {
    out.push({ key: 'last', title: '上一轮', tone: 'off', text: '还没整理过' })
  } else {
    const bad = j.status === 'failed' || j.status === 'interrupted'
    const at = j.finished_at || j.started_at || j.created_at
    out.push({
      key: 'last',
      title: '上一轮',
      tone: bad ? 'danger' : j.status === 'partial' ? 'warn' : 'ok',
      // 结果摘要（成功 3 · 待确认 1）比 message 短，一段里放得下
      text: [relTime(at), JOB_STATUS[j.status]?.text ?? j.status, resultSummary(j) || j.message].filter(Boolean).join(' · '),
      tip: [fullTime(at), j.title, j.message].filter(Boolean).join('\n'),
      action: '详情',
    })
  }

  // ---- 待确认 ----
  const n = recordStats.value.awaiting ?? 0
  out.push({
    key: 'awaiting',
    title: '待确认',
    tone: n > 0 ? 'warn' : 'ok',
    text: n > 0 ? `${n} 项等你确认后入库` : '没有',
    tip: '人工确认、AI 判定、同名、同集多份停下的条目',
    action: n > 0 ? '去确认' : undefined,
  })
  return out
})

function act(key: string) {
  if (key === 'cron') emit('goto', 'basic')
  else if (key === 'awaiting') void router.push({ name: 'tasks', query: { tab: 'records', status: 'awaiting' } })
  else if (key === 'last') {
    const id = running.value ? undefined : lastJob.value?.id
    void router.push({ name: 'tasks', query: id ? { tab: 'history', job: String(id) } : { tab: 'jobs' } })
  }
}
</script>

<template>
  <StatusStrip :items="items" @act="act" />
</template>
