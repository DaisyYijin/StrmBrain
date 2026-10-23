<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { BookOpen, ChevronRight, Info, Play, Radio, RefreshCw } from '@lucide/vue'
import HAlert from '@/components/hero/HAlert.vue'
import HButton from '@/components/hero/HButton.vue'
import HCheckbox from '@/components/hero/HCheckbox.vue'
import HChip from '@/components/hero/HChip.vue'
import HPopconfirm from '@/components/hero/HPopconfirm.vue'
import HModal from '@/components/hero/HModal.vue'
import HNumberInput from '@/components/hero/HNumberInput.vue'
import HHelpTip from '@/components/hero/HHelpTip.vue'
import HPagination from '@/components/hero/HPagination.vue'
import HSegmented from '@/components/hero/HSegmented.vue'
import SectionCard from '@/components/ui/SectionCard.vue'
import FieldRow from '@/components/ui/FieldRow.vue'
import FormActions from '@/components/ui/FormActions.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import TabSummary, { type SummaryItem } from '@/components/strm/TabSummary.vue'
import IncrHelp from './IncrHelp.vue'
import { storageApi, syncApi } from '@/api'
import { fullTime, relTime } from '@/utils/time'
import { num } from '@/utils/format'
import { INCR_DEFAULTS, loadIncrCfg, patchIncrCfg } from '@/composables/incrSetting'
import { toastError, useFeedback } from '@/composables/useFeedback'
import { useQueueStore } from '@/stores/queue'
import type { StrmRuns } from './useStrmRuns'

/**
 * 增量同步页签（2026-10-23 按「状态 → 记录 → 设置」重排，参考 openStrm 的网盘监控页）：
 * 顶部一行概况；中间是最近事件（115 上发生了什么、处理了没有）与最近有事的轮次；
 * 原来的六个状态格与「测试事件流」收进「诊断」，平时收起、有异常自动展开；最下面是轮询间隔。
 * 「立即同步」的逻辑（未保存确认）在 useStrmRuns，由 SyncPage 持有传进来。
 *
 * 本页只管轮询间隔。同一个 setting 里的 cron 是**自动整理**的调度开关，
 * 界面在「自动整理 → 基础配置」，所以这里只 patch 自己这个字段——见 incrSetting.ts
 */
defineProps<{ runs: StrmRuns }>()
const { message } = useFeedback()
const queue = useQueueStore()

const interval = ref<number | null>(INCR_DEFAULTS.interval_sec)
/** 库里那份间隔值，用来判断输入框改过没有——incr 不走 useSetting，dirty 得自己记 */
const intervalSaved = ref(INCR_DEFAULTS.interval_sec)
const saving = ref(false)
const dirty = computed(() => (interval.value ?? 0) !== intervalSaved.value)

async function saveInterval(): Promise<boolean> {
  saving.value = true
  try {
    await patchIncrCfg({ interval_sec: interval.value ?? 0 })
    intervalSaved.value = interval.value ?? 0
    message.success('保存成功')
    void loadStatus()
    return true
  } catch (e) {
    toastError(e, '保存失败')
    return false
  } finally {
    saving.value = false
  }
}

async function resetInterval() {
  interval.value = INCR_DEFAULTS.interval_sec
  await saveInterval()
}

// ---- 事件流状态 ----
const status = ref<syncApi.IncrStatus | null>(null)
const refreshing = ref(false)
const probing = ref(false)
const probeEvents = ref<syncApi.ProbeEvent[] | null>(null)

async function loadStatus() {
  refreshing.value = true
  try {
    status.value = await syncApi.incrStatus()
  } catch {
    // 状态查询失败不打扰用户：页面其余部分照常可用
  } finally {
    refreshing.value = false
  }
}

async function probe() {
  probing.value = true
  probeEvents.value = null
  try {
    const d = await syncApi.incrProbe()
    probeEvents.value = d.events
    message.success(d.message)
    void loadStatus()
  } catch (e) {
    toastError(e, '事件流测试失败')
  } finally {
    probing.value = false
  }
}

// ---- 最近事件 ----
const evGroup = ref<syncApi.IncrEventGroup>('all')
/** 默认藏掉整理自己产生的：整理一部剧就是几十条移动 / 改名，会把外部变更淹掉 */
const hideSelf = ref(true)
const evPage = ref(1)
const evSize = ref(20)
const events = ref<syncApi.IncrEvent[] | null>(null)
const evTotal = ref(0)
const evCounts = ref<Partial<Record<syncApi.IncrEventGroup, number>>>({})
const evHidden = ref(0)

async function loadEvents() {
  try {
    const d = await syncApi.incrEvents({ group: evGroup.value, hide_self: hideSelf.value, page: evPage.value, size: evSize.value })
    events.value = d.data
    evTotal.value = d.total
    evCounts.value = d.counts
    evHidden.value = d.hidden
  } catch {
    events.value ??= []
  }
}
watch([evGroup, hideSelf, evSize], () => {
  if (evPage.value !== 1) evPage.value = 1
  else void loadEvents()
})
// 翻页后回到列表顶上：列表在卡片里自己滚，不回去的话新一页是从中间开始看的
const evBox = ref<HTMLElement | null>(null)
watch(evPage, async () => {
  await loadEvents()
  if (evBox.value) evBox.value.scrollTop = 0
})

const GROUPS = computed(() =>
  (
    [
      ['all', '全部'],
      ['add', '新增'],
      ['del', '删除'],
      ['move', '移动 / 改名'],
    ] as const
  ).map(([value, label]) => ({ value, label: `${label} ${num(evCounts.value[value] ?? 0)}` })),
)

const EV_TYPE: Record<string, { text: string; color: 'success' | 'danger' | 'default' }> = {
  upload_file: { text: '上传', color: 'success' },
  upload_image_file: { text: '上传图片', color: 'success' },
  receive_files: { text: '转存', color: 'success' },
  copy_file: { text: '复制', color: 'success' },
  new_folder: { text: '新建目录', color: 'success' },
  copy_folder: { text: '复制目录', color: 'success' },
  move_file: { text: '移动', color: 'default' },
  move_image_file: { text: '移动', color: 'default' },
  file_rename: { text: '改名', color: 'default' },
  folder_rename: { text: '目录改名', color: 'default' },
  delete_file: { text: '删除', color: 'danger' },
}

// 附加账号的名字（事件只带账号 id）；读不到就写「附加账号」
const acctNames = ref<Record<number, string>>({})
async function loadAccts() {
  try {
    const m: Record<number, string> = {}
    for (const a of (await storageApi.accounts()).data) m[a.id] = a.name
    acctNames.value = m
  } catch {
    // 只影响显示
  }
}

// ---- 最近轮次（内存里，重启清空） ----
const rounds = ref<syncApi.IncrRound[] | null>(null)
const roundTotal = ref(0)
const roundIdle = ref(0)
const roundSince = ref('')
async function loadRounds() {
  try {
    const d = await syncApi.incrRounds()
    rounds.value = d.rounds
    roundTotal.value = d.total
    roundIdle.value = d.idle
    roundSince.value = d.since ?? ''
  } catch {
    rounds.value ??= []
  }
}
const ROUND_SHOW = 10
const roundsAll = ref(false)
const roundRows = computed(() => (roundsAll.value ? rounds.value : rounds.value?.slice(0, ROUND_SHOW)) ?? [])

// 状态、事件、轮次都只读内存或本地库：15 秒刷一次，与顶部状态条同节奏；增量任务跑完立即刷
let timer: number | undefined
function refreshAll() {
  void loadStatus()
  void loadEvents()
  void loadRounds()
}
onMounted(async () => {
  refreshAll()
  void loadAccts()
  timer = window.setInterval(() => {
    if (!document.hidden) refreshAll()
  }, 15_000)
  try {
    interval.value = (await loadIncrCfg()).interval_sec
    intervalSaved.value = interval.value ?? 0
  } catch {
    // 读不到就留在默认值上：保存时是 patch，不会把 cron 一起写坏
  }
})
const offFinished = queue.onFinished((j) => {
  if (j.kind === 'incr') refreshAll()
})
onUnmounted(() => {
  clearInterval(timer)
  offFinished()
})

/** 间隔填 0（或清空）= 关掉独立轮询，增量此时没有自己的时间表 */
const pollingOff = computed(() => (interval.value ?? 0) <= 0)

/** 要用户留意的几件事：概况第一格按它下结论，诊断有它就自动展开 */
const issues = computed(() => {
  const s = status.value
  if (!s) return []
  const out: { text: string; tone: 'warn' | 'danger' }[] = []
  if (s.last_round.error) out.push({ text: '上一轮出错', tone: 'danger' })
  if (!s.life_gate.ok) out.push({ text: '事件开关异常', tone: 'warn' })
  if (s.stall.rounds > 0) out.push({ text: `连续 ${s.stall.rounds} 轮未消费`, tone: 'warn' })
  return out
})

const summary = computed<SummaryItem[]>(() => {
  const s = status.value
  if (!s) return [{ key: 'loading', label: '状态', value: '读取中…' }]
  const r = s.last_round
  const sum = r.summary
  const off = s.interval_sec <= 0
  const first = issues.value[0]
  const roundBits: string[] = []
  if (sum) {
    roundBits.push(`#${sum.round}`)
    if (r.error) roundBits.push(r.error)
    else if (!sum.consumed) roundBits.push(`未消费：${sum.not_consumed}`)
    else {
      if (sum.strm_created) roundBits.push(`新增 ${sum.strm_created}`)
      if (sum.deleted) roundBits.push(`清理 ${sum.deleted}`)
      if (sum.moved) roundBits.push(`移动 ${sum.moved}`)
      if (roundBits.length === 1) roundBits.push('无变化')
      roundBits.push(`用时 ${sum.elapsed}`)
    }
  }
  return [
    {
      key: 'state',
      label: '状态',
      value: first ? first.text : off ? '跟随整理执行' : '运行正常',
      tone: first ? first.tone : off ? 'off' : 'ok',
      sub: first
        ? !s.life_gate.ok
          ? s.life_gate.message
          : s.stall.reason || r.error || ''
        : `事件开关正常 · ${s.endpoint.startsWith('备用') ? '备用通道' : '主通道'}`,
    },
    {
      key: 'round',
      label: '上一轮',
      value: r.at ? relTime(r.at) : '还没跑过',
      sub: roundBits.join(' · '),
    },
    {
      key: 'interval',
      label: '轮询',
      value: off ? '已关闭' : `每 ${s.interval_sec} 秒`,
      sub: roundTotal.value
        ? `启动以来 ${num(roundTotal.value)} 轮，有变化的 ${num(roundTotal.value - roundIdle.value)} 轮`
        : off
          ? '只在自动整理的 cron 命中后顺带执行'
          : '',
    },
    {
      key: 'pending',
      label: '积压事件',
      value: `${num(s.pending_events)} 条`,
      tone: s.pending_events > 0 ? 'warn' : undefined,
      sub: s.pending_events > 0 ? '有目录一直读不出来时会持续不降' : `目录路径缓存 ${num(s.path_cache)} 条`,
    },
  ]
})

// 诊断平时收起；第一次读到状态时有异常就展开，之后由用户自己开合
const diagOpen = ref(false)
let diagDecided = false
watch(status, (s) => {
  if (!s || diagDecided) return
  diagDecided = true
  diagOpen.value = issues.value.length > 0 || s.pending_events > 0
})

/** 状态格：每格一个结论 + 一句补充，tone 决定颜色 */
type Tone = 'ok' | 'warn' | 'danger' | 'muted'
const cells = computed(() => {
  const s = status.value
  if (!s) return []
  const out: { key: string; label: string; tip: string; value: string; sub?: string; tone: Tone }[] = [
    {
      key: 'gate',
      label: '事件开关',
      tip: '115 客户端里的「生活」事件记录。关掉之后接口会一直返回空，增量就此静默空转。',
      value: s.life_gate.ok ? '正常' : '异常',
      sub: [s.life_gate.message, s.life_gate.checked_at && `${s.life_gate.checked_at} 检查`].filter(Boolean).join(' · '),
      tone: s.life_gate.ok ? 'ok' : 'warn',
    },
    {
      key: 'endpoint',
      label: '当前通道',
      tip: '主通道更快但更容易被 115 限流；连续被拒会自动切到备用通道 24 小时。',
      value: s.endpoint || '—',
      tone: 'muted',
    },
    {
      key: 'interval',
      label: '轮询间隔',
      tip: '保存后的实际间隔。0 = 关闭独立轮询，只跟随自动整理执行。',
      value: s.interval_sec > 0 ? `每 ${s.interval_sec} 秒` : '已关闭',
      sub: s.interval_sec > 0 ? undefined : '跟随自动整理串行执行',
      tone: s.interval_sec > 0 ? 'ok' : 'warn',
    },
    {
      key: 'lock',
      label: '当前任务',
      tip: '整理、增量、全量、洗版、深删都排这一条队。「手动点整理提示有任务在跑」「转存完迟迟不入库」看的就是这里。',
      value: s.task_lock.busy ? '占用中' : '空闲',
      sub: [
        s.task_lock.busy ? s.task_lock.describe : '',
        s.task_lock.waiting?.length ? `${s.task_lock.waiting.join('、')} 在排队（已等 ${s.task_lock.waited_sec} 秒）` : '',
      ]
        .filter(Boolean)
        .join(' · '),
      tone: s.task_lock.busy ? 'warn' : 'ok',
    },
    {
      key: 'pending',
      label: '积压事件',
      tip: '拉回来但还没处理完的事件。持续不降说明有网盘目录一直读不出来，日志里会有提示。',
      value: `${s.pending_events} 条`,
      sub: `目录路径缓存 ${s.path_cache} 条`,
      tone: s.pending_events > 0 ? 'warn' : 'ok',
    },
    {
      key: 'stall',
      label: '重放检测',
      tip: '连续多少轮没能把事件消费掉。大于 0 时，日志里反复出现的同一批目录是重放，不是网盘上真有这么多变化。',
      value: s.stall.rounds > 0 ? `连续 ${s.stall.rounds} 轮未消费` : '正常',
      sub: s.stall.rounds > 0 ? [s.stall.reason, s.stall.since && `起于 ${s.stall.since}`].filter(Boolean).join(' · ') : '上一轮已消费',
      tone: s.stall.rounds > 0 ? 'warn' : 'ok',
    },
  ]
  return out
})

const helpVisible = ref(false)
</script>

<template>
  <div class="tab-body">
    <TabSummary :items="summary">
      <HPopconfirm confirm-text="开始" :disabled="runs.busyIncr.value" @confirm="void runs.runIncr()">
        <HButton variant="primary" :disabled="runs.busyIncr.value" :loading="runs.runningIncr.value">
          <template #icon><Play :size="14" /></template>
          立即同步
        </HButton>
        <template #content>立即执行一次增量同步？</template>
      </HPopconfirm>
    </TabSummary>

    <SectionCard title="最近事件" hint="115 上发生了什么、本地处理了没有（近 30 天）">
      <template #extra>
        <HSegmented v-model="evGroup" :options="GROUPS" size="sm" aria-label="事件类型" />
      </template>

      <div class="ev-bar">
        <label class="ev-check">
          <HCheckbox v-model:checked="hideSelf" />
          隐藏整理自己产生的事件
        </label>
        <span v-if="hideSelf && evHidden" class="dim">已隐藏 {{ num(evHidden) }} 条</span>
      </div>

      <EmptyState v-if="events && !events.length" text="这一类最近没有事件" />
      <ul v-else-if="events" ref="evBox" class="ev-list scroll-box">
        <li v-for="e in events" :key="e.id" class="ev" :class="{ 'is-quiet': e.note === 'outside' }">
          <span class="ev-at" :title="fullTime(e.at)">{{ relTime(e.at) }}</span>
          <HChip class="ev-type" :color="EV_TYPE[e.type]?.color ?? 'default'">{{ EV_TYPE[e.type]?.text ?? e.type }}</HChip>
          <span class="ev-main">
            <span class="ev-name" :title="e.name">{{ e.name }}<template v-if="e.is_dir">/</template></span>
            <span v-if="e.path" class="ev-path" :title="e.path">{{ e.path }}</span>
          </span>
          <span class="ev-end">
            <HChip v-if="e.account" variant="tertiary">{{ acctNames[e.account] ?? '附加账号' }}</HChip>
            <HChip v-if="e.status === 'pending'" color="warning">待处理</HChip>
            <HChip v-else-if="e.note === 'self'" title="整理时已经生成 STRM，增量跳过">整理产生</HChip>
            <span v-else-if="e.note === 'outside'" class="dim" title="在转存 / 已存在 / 冗余目录里，或本地本来就没有，不用处理">不涉及媒体库</span>
            <span v-else class="dim">已处理</span>
          </span>
        </li>
      </ul>
      <HPagination
        v-if="evTotal > evSize"
        v-model:page="evPage"
        v-model:page-size="evSize"
        :total="evTotal"
        class="pager"
      />
    </SectionCard>

    <SectionCard title="最近轮次" hint="只列拉到事件、出错或没消费掉的轮次；轮次号对应日志里的 [同步#N]，重启后清空">
      <EmptyState v-if="rounds && !rounds.length" text="启动以来还没有过有变化的轮次" />
      <div v-else-if="rounds" class="table-wrap scroll-box">
        <table class="rounds">
          <thead>
            <tr>
              <th>轮次</th>
              <th>时间</th>
              <th class="r">事件</th>
              <th class="r">新增 STRM</th>
              <th class="r">清理</th>
              <th class="r">移动 / 改名</th>
              <th class="r">列目录</th>
              <th class="r">用时</th>
              <th>结果</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(r, i) in roundRows" :key="r.summary?.round ?? `e${i}`">
              <td class="mono">{{ r.summary ? `#${r.summary.round}` : '—' }}</td>
              <td :title="fullTime(r.at)">{{ relTime(r.at) }}</td>
              <template v-if="r.summary">
                <td class="r">{{ r.summary.events_pending }}</td>
                <td class="r" :class="{ zero: !r.summary.strm_created }">{{ r.summary.strm_created }}</td>
                <td class="r" :class="{ zero: !r.summary.deleted }">{{ r.summary.deleted }}</td>
                <td class="r" :class="{ zero: !r.summary.moved }">{{ r.summary.moved }}</td>
                <td class="r" :class="{ zero: !r.summary.list_calls }">{{ r.summary.list_calls }}</td>
                <td class="r">{{ r.summary.elapsed || '—' }}</td>
              </template>
              <td v-else colspan="6" />
              <td class="res">
                <HChip v-if="r.error" color="danger" :title="r.error">出错：{{ r.error }}</HChip>
                <HChip v-else-if="r.summary && !r.summary.consumed" color="warning" :title="r.summary.not_consumed">
                  未消费：{{ r.summary.not_consumed }}
                </HChip>
                <HChip v-else color="success">已处理</HChip>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="rounds && rounds.length" class="rounds-foot">
        <span class="dim">
          {{ roundSince ? `${relTime(roundSince)}启动以来` : '' }}另有 {{ num(roundIdle) }} 轮没有新事件，没列出来
        </span>
        <HButton v-if="rounds.length > ROUND_SHOW" size="sm" variant="ghost" @click="roundsAll = !roundsAll">
          {{ roundsAll ? '收起' : `显示全部 ${rounds.length} 轮` }}
        </HButton>
      </div>
    </SectionCard>

    <!-- 原来的六个状态格与测试事件流：排查「为什么没同步」用，平时收起，有异常第一次打开时自动展开 -->
    <section class="card card--default diag">
      <button type="button" class="diag-head" :aria-expanded="diagOpen" @click="diagOpen = !diagOpen">
        <ChevronRight :size="16" class="diag-chev" :class="{ open: diagOpen }" />
        <span class="diag-title">诊断</span>
        <HChip v-if="issues.length" color="warning">{{ issues.length }} 项要留意</HChip>
        <span v-else class="dim">事件开关、通道、任务锁、积压、重放检测</span>
      </button>
      <div v-if="diagOpen" class="diag-body">
        <div v-if="status" class="cells">
          <div v-for="c in cells" :key="c.key" class="cell" :class="`tone-${c.tone}`">
            <div class="cell-label">{{ c.label }}<HHelpTip :text="c.tip" /></div>
            <div class="cell-value"><span class="cell-dot" />{{ c.value }}</div>
            <div v-if="c.sub" class="cell-sub" :title="c.sub">{{ c.sub }}</div>
          </div>
        </div>
        <div class="diag-btns">
          <HButton size="sm" variant="tertiary" :loading="probing" @click="void probe()">
            <template #icon><Radio :size="14" /></template>
            测试事件流
          </HButton>
          <HButton size="sm" variant="ghost" :loading="refreshing" @click="void loadStatus()">
            <template #icon><RefreshCw :size="14" /></template>
            刷新
          </HButton>
          <span class="dim">测试只读取不处理：不推进游标、不落库、不动本地文件</span>
        </div>
        <div v-if="probeEvents" class="probe">
          <div v-if="!probeEvents.length" class="dim">最近没有任何事件。</div>
          <div v-for="e in probeEvents" :key="e.id" class="probe-row" :class="{ dim: e.ignored }">
            <span class="probe-at">{{ e.at }}</span>
            <span class="probe-kind">{{ e.kind }}</span>
            <span class="probe-type">{{ e.type }}</span>
            <span class="probe-name">{{ e.name }}</span>
            <span v-if="e.ignored" class="probe-skip">不处理</span>
          </div>
          <div class="dim probe-tip">标「不处理」的是浏览 / 标星类事件。</div>
        </div>
      </div>
    </section>

    <SectionCard title="轮询设置" hint="独立轮询，只管网盘端的外部变更">
      <template #extra>
        <HButton size="sm" variant="ghost" @click="helpVisible = true">
          <template #icon><BookOpen :size="14" /></template>
          功能介绍
        </HButton>
      </template>

      <p class="pre">
        <Info :size="14" />
        前提：115 生活 App 里开启「最近」（生活事件），并且先完整执行过一次全量同步。
      </p>
      <p class="pre">
        <Info :size="14" />
        <span>
          增量新增的片目要自动刮削 NFO / 海报，到
          <RouterLink class="jump" :to="{ name: 'scrape' }">影视刮削</RouterLink>
          打开「同步后自动刮削」。
        </span>
      </p>

      <FieldRow
        label="增量同步间隔"
        tip="每隔这么久拉一次 115 生活事件，把网盘端的变化落到本地 STRM。一轮通常只发 1~2 个请求，没有新事件时完全静默，所以跑得勤的代价很低：30 秒一轮意味着手机上传的片子最多半分钟就能进媒体库。"
        hint="0 = 关闭独立轮询；最小 15 秒，低于 15 按 15 处理"
      >
        <HNumberInput v-model="interval" :min="0" :step="15" aria-label="增量同步间隔">
          <template #suffix>秒</template>
        </HNumberInput>
      </FieldRow>

      <HAlert v-if="pollingOff" class="note-top" status="warning" title="独立轮询已关闭">
        增量现在没有自己的时间表了：只有「自动整理」的 cron 命中时，才会在整理跑完后顺带执行一次。
        手机上传、网页端的删除与改名要等到下一次整理才会反映到本地；
        <strong>那条 cron 留空的话，增量就完全不会自动执行</strong>，只能靠上面的「立即同步」手点。
        <div class="note-act">
          <RouterLink class="jump" to="/organize">去看自动整理的 cron →</RouterLink>
        </div>
      </HAlert>

      <FormActions>
        <HButton variant="primary" :loading="saving" :disabled="!dirty && !saving" @click="void saveInterval()">保存配置</HButton>
        <HButton variant="tertiary" @click="void resetInterval()">重置为 {{ INCR_DEFAULTS.interval_sec }} 秒</HButton>
      </FormActions>
    </SectionCard>

    <!-- 弹窗必须留在这个根元素里：本页整体被 SyncPage 的 <Transition> 包着，
         多个根节点会让 Transition 找不到唯一子元素，整页渲染成空白 -->
    <HModal v-model:show="helpVisible" title="增量同步是怎么回事" width="860px">
      <IncrHelp />
    </HModal>
  </div>
</template>

<style scoped>
.tab-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.dim {
  font-size: 12px;
  color: var(--muted);
}
.mono {
  font-family: var(--font-mono);
  font-size: 12px;
}

/* 记录多了在卡片里自己滚，表头钉住；不把整页撑高 */
.scroll-box {
  max-height: min(420px, 55vh);
  overflow: auto;
  overscroll-behavior: contain;
}
.scroll-box thead th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--surface);
}

/* ---- 最近事件 ---- */
.ev-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 16px;
  margin-bottom: 4px;
}
.ev-check {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--muted);
  cursor: pointer;
}
.ev-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.ev {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  padding: 9px 0;
  font-size: 13px;
}
.ev + .ev {
  border-top: 1px solid var(--separator);
}
.ev.is-quiet .ev-name {
  color: var(--muted);
}
.ev-at {
  flex-shrink: 0;
  width: 74px;
  font-size: 12px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.ev-type {
  flex-shrink: 0;
  min-width: 52px;
  justify-content: center;
}
.ev-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.ev-name,
.ev-path {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ev-path {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--muted);
}
.ev-end {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 6px;
}
.pager {
  margin-top: 10px;
}

/* ---- 最近轮次 ---- */
.table-wrap {
  overflow-x: auto;
  margin: 0 -4px;
}
.rounds {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.rounds th {
  padding: 6px 10px;
  text-align: left;
  font-size: 12px;
  font-weight: 500;
  color: var(--muted);
  white-space: nowrap;
}
.rounds td {
  padding: 8px 10px;
  border-top: 1px solid var(--separator);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.rounds .r {
  text-align: right;
}
.rounds .zero {
  color: var(--muted);
}
.rounds .res {
  max-width: 320px;
}
.rounds .res :deep(.chip) {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
}
.rounds-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}

/* ---- 诊断 ---- */
.diag {
  padding: 0;
  gap: 0;
}
.diag-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 10px;
  width: 100%;
  padding: 16px 20px;
  border: 0;
  background: none;
  font: inherit;
  color: inherit;
  text-align: left;
  cursor: pointer;
}
.diag-chev {
  color: var(--muted);
  transition: transform 0.15s ease;
}
.diag-chev.open {
  transform: rotate(90deg);
}
.diag-title {
  font-size: 15px;
  font-weight: 600;
}
.diag-body {
  padding: 0 20px 18px;
}
.diag-btns {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 8px;
  margin-top: 12px;
}

.cells {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}
.cell {
  --tone: var(--success);
  min-width: 0;
  padding: 12px 14px;
  border-radius: var(--r-lg);
  background: var(--surface-secondary);
}
.tone-warn {
  --tone: var(--warning);
}
.tone-danger {
  --tone: var(--danger);
}
.tone-muted {
  --tone: var(--muted);
}
.cell-label {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--muted);
}
.cell-value {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-top: 4px;
  font-size: 15px;
  font-weight: 600;
  color: var(--foreground);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.tone-warn .cell-value {
  color: var(--warning-soft-foreground);
}
.cell-dot {
  width: 7px;
  height: 7px;
  flex-shrink: 0;
  border-radius: 999px;
  background: var(--tone);
}
.tone-muted .cell-dot {
  display: none;
}
.cell-sub {
  margin-top: 2px;
  font-size: 12px;
  color: var(--muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ---- 事件探针 ---- */
.probe {
  margin-top: 10px;
  padding: 10px 14px;
  border-radius: 16px;
  background: var(--surface-secondary);
}
.probe-row {
  display: flex;
  gap: 10px;
  align-items: baseline;
  font-size: 12px;
  padding: 3px 0;
}
.probe-row.dim {
  opacity: 0.5;
}
.probe-at {
  color: var(--muted);
  flex-shrink: 0;
  font-variant-numeric: tabular-nums;
}
.probe-kind,
.probe-type {
  color: var(--muted);
  flex-shrink: 0;
}
.probe-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.probe-skip {
  margin-left: auto;
  color: var(--muted);
  flex-shrink: 0;
}
.probe-tip {
  margin-top: 8px;
}

/* ---- 轮询设置 ---- */
.pre {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin: 0 0 12px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--muted);
}
.pre :deep(svg) {
  flex-shrink: 0;
  margin-top: 3px;
}
.note-top {
  margin-top: 14px;
}
.jump {
  font-size: 13px;
  font-weight: 500;
  color: var(--accent);
  text-decoration: none;
}
.jump:hover {
  text-decoration: underline;
}
.note-act {
  margin-top: 8px;
}

@media (max-width: 1080px) {
  .cells {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
/* 手机：事件一行放不下，名字与路径单独一行；探针同理 */
@media (max-width: 720px) {
  .ev {
    flex-wrap: wrap;
  }
  .ev-at {
    width: auto;
  }
  .ev-main {
    order: 3;
    flex-basis: 100%;
  }
  .ev-end {
    margin-left: auto;
  }
  .cells {
    grid-template-columns: 1fr;
  }
  .probe-row {
    flex-wrap: wrap;
  }
  .probe-name {
    flex-basis: 100%;
    white-space: normal;
    word-break: break-all;
  }
}
</style>
