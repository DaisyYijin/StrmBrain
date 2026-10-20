<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import type { RouteLocationRaw } from 'vue-router'
import { ChevronRight } from '@lucide/vue'
import { configApi, organizeApi } from '@/api'
import type { LibScrape, LibScrapeView } from '@/api/organize'

/**
 * 整理流水线，每一步下面一行小字写当前怎么设的，点了跳到改它的地方（2026-10-20，参考 LitePan 的流程预览）。
 * 原来是一行写死的「识别 → … → 刷新 Emby」，可这些设置散在四五处（AI 页签、洗版页签、库卡片、影视刮削、Emby），
 * 新用户拼不出整条链路。
 *
 * **只读、只放跳转**：这里多一个开关就多一处整存整取的编辑入口（incr.cron 那种互相覆盖的坑），
 * 人工确认是例外 —— 它就在本页下面，由 BasicTab 传进来。
 * 各项读不到就不写那行小字，不影响别的。
 */
const props = defineProps<{ manualConfirm: boolean }>()
const router = useRouter()

const aiOn = ref<boolean | null>(null)
const washMode = ref('')
const libs = ref<LibScrapeView[] | null>(null)
const probe = ref<boolean | null>(null)
const emby = ref<{ on: boolean; refresh: boolean } | null>(null)

onMounted(() => {
  void configApi
    .getSetting<{ enabled: boolean }>('org-gpt', { enabled: false })
    .then((v) => (aiOn.value = !!v.enabled))
    .catch(() => {})
  void organizeApi
    .getWash()
    .then((d) => {
      // 只认第一条 mode（全局那条）；按分类分开写的不细说，点进去看
      const m = /^\s*mode:\s*([a-z_]+)/m.exec(d.config ?? '')
      washMode.value = m ? m[1] : (d.config ?? '').trim() ? 'custom' : 'none'
    })
    .catch(() => {})
  void organizeApi
    .getScrapeConfig()
    .then((d) => {
      libs.value = (d.libs ?? []).filter((l) => l.mode !== 'self')
      probe.value = !!d.cfg?.probe_streams
    })
    .catch(() => {})
  void configApi
    .getSetting<{ server_url: string; refresh_enabled: boolean }>('emby', { server_url: '', refresh_enabled: true })
    .then((v) => (emby.value = { on: !!v.server_url?.trim(), refresh: v.refresh_enabled !== false }))
    .catch(() => {})
})

const WASH_TEXT: Record<string, string> = {
  replace: '更优的替换旧版',
  max_size: '更优的替换旧版',
  min_size: '更优的替换旧版',
  skip: '已有就跳过',
  coexist: '多版本共存',
  custom: '已配置',
  none: '未配置，只按文件去重',
}
const SCRAPE_TEXT: Record<LibScrape, string> = { auto: '自动刮', manual: '只手动刮', off: '交给 Emby' }

/** 刮削：一个库直接写方式，几个库写「自动 2 · 交给 Emby 1」 */
const scrapeNote = computed(() => {
  const ls = libs.value
  if (!ls?.length) return ''
  if (ls.length === 1) return SCRAPE_TEXT[ls[0].scrape]
  const n: Partial<Record<LibScrape, number>> = {}
  for (const l of ls) n[l.scrape] = (n[l.scrape] ?? 0) + 1
  return (['auto', 'manual', 'off'] as LibScrape[])
    .filter((k) => n[k])
    .map((k) => `${SCRAPE_TEXT[k]} ${n[k]}`)
    .join(' · ')
})

interface Step {
  name: string
  note?: string
  /** 小字要不要上警告色（「Emby 未配置」这种） */
  warn?: boolean
  to?: RouteLocationRaw
  /** 停在这一步（人工确认） */
  stop?: boolean
}
const tab = (t: string): RouteLocationRaw => ({ name: 'organize', query: { tab: t } })

const steps = computed<Step[]>(() => {
  const e = emby.value
  return [
    {
      name: '识别',
      note: aiOn.value === null ? undefined : aiOn.value ? '含 AI 增强' : 'TMDB 搜索',
      to: tab('ai'),
      stop: props.manualConfirm,
    },
    { name: '二级分类', note: '按分类规则', to: tab('category') },
    { name: '洗版', note: washMode.value ? (WASH_TEXT[washMode.value] ?? washMode.value) : undefined, to: tab('wash') },
    { name: '重命名', note: '按命名模板', to: tab('rename') },
    { name: '搬入媒体库' },
    { name: '写 STRM' },
    { name: '刮削', note: scrapeNote.value || undefined, to: { name: 'scrape' } },
    {
      name: '刷新 Emby',
      note: !e ? undefined : !e.on ? '未配置 Emby' : !e.refresh ? '已关闭' : probe.value ? '并提前探测' : '只刷新',
      warn: !!e && !e.on,
      to: e && !e.on ? { name: 'settings', query: { tab: 'emby' } } : probe.value === null ? undefined : { name: 'scrape', query: { tab: 'media' } },
    },
  ]
})
</script>

<template>
  <ol class="pipe" aria-label="整理流水线">
    <li v-for="(s, i) in steps" :key="s.name" class="step">
      <div class="step-body">
        <span class="step-name" :class="{ 'is-stop': s.stop }">{{ s.name }}</span>
        <button
          v-if="s.note && s.to"
          type="button"
          class="step-note link"
          :class="{ warn: s.warn }"
          :title="`去改：${s.name}`"
          @click="router.push(s.to)"
        >
          {{ s.note }}
        </button>
        <span v-else-if="s.note" class="step-note" :class="{ warn: s.warn }">{{ s.note }}</span>
      </div>
      <ChevronRight v-if="i < steps.length - 1" :size="12" class="pipe-arrow" />
    </li>
  </ol>
</template>

<style scoped>
.pipe {
  display: flex;
  flex-wrap: wrap;
  /* 步骤名对齐在同一条线上：有的步骤下面有小字、有的没有，居中对齐的话没小字的会往下掉半行 */
  align-items: flex-start;
  gap: 8px 4px;
  margin: 0 0 14px;
  padding: 10px 12px;
  border-radius: var(--r-lg);
  background: var(--surface-secondary);
  list-style: none;
}
.step {
  display: inline-flex;
  align-items: flex-start;
  gap: 4px;
}
.step-body {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 1px;
}
.step-name {
  font-size: 12.5px;
  color: var(--foreground);
  white-space: nowrap;
}
.step-name.is-stop {
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--warning-soft);
  color: var(--warning-soft-foreground);
  font-weight: 500;
}
.step-note {
  padding: 0;
  border: 0;
  background: none;
  font: inherit;
  font-size: 11px;
  color: var(--muted);
  white-space: nowrap;
}
.step-note.link {
  cursor: pointer;
}
.step-note.link:hover {
  color: var(--accent);
  text-decoration: underline;
}
.step-note.warn {
  color: var(--warning-soft-foreground);
}
.pipe-arrow {
  margin-top: 4px;
  color: var(--muted);
}
</style>
