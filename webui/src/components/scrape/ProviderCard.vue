<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { CircleCheck, RefreshCw, TriangleAlert } from '@lucide/vue'
import HButton from '@/components/hero/HButton.vue'
import HSpinner from '@/components/hero/HSpinner.vue'
import SectionCard from '@/components/ui/SectionCard.vue'
import { organizeApi } from '@/api'
import type { EmbyCheck, LibScrape } from '@/api/organize'
import { useScrapeProvider } from '@/composables/scrapeProvider'

/**
 * 影视刮削页顶部：每个库刮不刮的摘要（只读）+ Emby 媒体库设置检查。
 * 2026-10-15 起刮不刮按库设、改在「账号与媒体库 → 媒体库」的库卡片上（LIBRARY-ROLES-PLAN.md 阶段 2），
 * 这一页只管「怎么刮」。这里**不放能改的开关**：两处各存一份会互相覆盖（incr.cron 那个坑）。
 * 原来的「本站刮削 / Emby 刮削」二选一已删，后端 POST /scrape/provider 也删了。
 */
const { libs, embyScrapes, load } = useScrapeProvider()

const MODE_TEXT: Record<LibScrape, string> = {
  auto: '自动刮新片',
  manual: '只手动刮',
  off: '不刮（交给 Emby）',
}
const LIB_LINK = { name: 'accounts', query: { tab: 'library' } }

// ---- Emby 媒体库设置检查 ----

const check = ref<EmbyCheck | null>(null)
const checking = ref(false)
async function runCheck() {
  checking.value = true
  try {
    check.value = await organizeApi.embyCheck()
  } catch (e) {
    check.value = { configured: true, error: e instanceof Error ? e.message : String(e) }
  } finally {
    checking.value = false
  }
}

/** 每个 Emby 媒体库按它覆盖到的本站库的刮削方式报问题：交给 Emby 的看下载器，本站刮的看会不会再刮一遍 */
const embyLibs = computed(() =>
  (check.value?.libraries ?? []).map((l) => {
    const modes = l.modes ?? []
    const emby = modes.includes('off') || (!modes.length && embyScrapes.value)
    const station = modes.some((m) => m !== 'off') || (!modes.length && !embyScrapes.value)
    return {
      ...l,
      emby,
      issues: [...(emby ? (l.emby_issues ?? []) : []), ...(station ? (l.station_issues ?? []) : [])],
    }
  }),
)
const embyProblem = computed(() => embyLibs.value.some((l) => l.emby && (l.emby_issues ?? []).length))
const stationProblem = computed(() => embyLibs.value.some((l) => !l.emby && l.issues.length))

onMounted(() => {
  void load(true)
  void runCheck()
})
</script>

<template>
  <SectionCard title="各库刮不刮" hint="每个库在「账号与媒体库 → 媒体库」里选；这一页只管怎么刮">
    <div class="layout">
      <div class="choose">
        <ul class="modes">
          <li v-for="l in libs" :key="l.cid">
            <span class="lib-label">{{ l.name || `cid ${l.cid}` }}</span>
            <span class="mode" :class="l.scrape">{{ MODE_TEXT[l.scrape] }}</span>
          </li>
        </ul>
        <RouterLink :to="LIB_LINK" class="link">去媒体库修改 →</RouterLink>
        <p class="effect">
          「自动刮新片」的库：整理进来的、同步进来的新片目都刮，定时补全也补它；「只手动刮」只在<RouterLink :to="{ name: 'local' }">海报墙</RouterLink>勾选后刮；
          「不刮」的库本站不写 NFO 与图片，海报墙改看 Emby 的海报与识别结果。
        </p>
      </div>

      <!-- Emby 媒体库设置检查：只读，不替用户改 Emby -->
      <div class="check">
        <div class="check-head">
          <span class="check-title">Emby 媒体库设置</span>
          <HSpinner v-if="checking" size="sm" />
          <HButton v-else variant="ghost" size="sm" icon-only aria-label="重新检查" title="重新检查" @click="runCheck">
            <RefreshCw :size="14" />
          </HButton>
        </div>
        <template v-if="check">
          <p v-if="!check.configured" class="check-line muted">
            未配置 Emby，没法检查（<RouterLink :to="{ name: 'settings', query: { tab: 'emby' } }">系统配置 → Emby</RouterLink>）。
          </p>
          <p v-else-if="check.error" class="check-line warn"><TriangleAlert :size="14" />{{ check.error }}</p>
          <p v-else-if="check.unmatched" class="check-line warn">
            <TriangleAlert :size="14" />Emby 里有媒体库，但没有一个的路径对得上本地媒体库：检查 Emby 设置里的路径映射。
          </p>
          <template v-else>
            <div class="libs">
              <div v-for="l in embyLibs" :key="l.name" class="lib">
                <span class="lib-icon" :class="l.issues.length ? 'warn' : 'ok'">
                  <TriangleAlert v-if="l.issues.length" :size="14" />
                  <CircleCheck v-else :size="14" />
                </span>
                <div class="lib-body">
                  <div class="lib-name">{{ l.name }}</div>
                  <div v-if="l.issues.length" class="lib-issues">
                    <div v-for="i in l.issues" :key="i">{{ i }}</div>
                  </div>
                  <div v-else class="lib-ok">
                    {{ l.emby ? `元数据：${l.meta_fetchers.join('、') || '—'}；图像：${l.image_fetchers.join('、') || '—'}` : '设置合适' }}
                  </div>
                </div>
              </div>
            </div>
            <p v-if="embyProblem" class="check-foot">
              交给 Emby 刮的库：在 Emby「媒体库 → 编辑媒体库」里勾选元数据下载器与图像获取器（TheMovieDb 等），否则没人刮。
            </p>
            <p v-if="stationProblem" class="check-foot">
              本站刮的库：到「刮削」页签右侧展开「用本站刮削时，Emby 媒体库要这样设」照着改；不改的话 Emby 会自己再刮一遍，结果以 Emby 的为准。
            </p>
          </template>
        </template>
      </div>
    </div>
  </SectionCard>
</template>

<style scoped>
/* 左边各库摘要、右边 Emby 设置检查：并排放，页签上方不再压一大块 */
.layout {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}
.choose {
  min-width: 0;
}
.modes {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0 0 8px;
  padding: 0;
  list-style: none;
}
.modes li {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
}
.lib-label {
  min-width: 72px;
  font-weight: 500;
}
.mode {
  padding: 1px 10px;
  border-radius: 999px;
  font-size: 12px;
  background: var(--surface-secondary);
  color: var(--muted);
}
.mode.auto {
  background: var(--accent-soft);
  color: var(--accent);
}
.link {
  font-size: 12.5px;
  color: var(--accent);
  text-decoration: none;
}
.effect {
  margin: 10px 0 0;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--muted);
}
.effect a,
.check a {
  color: var(--accent);
  text-decoration: none;
}

.check {
  min-width: 0;
  padding: 10px 12px;
  border-radius: var(--r-lg);
  background: var(--surface-secondary);
}
/* 库多的时候在框里滚，不把左边撑高 */
.libs {
  max-height: 168px;
  overflow-y: auto;
}
.check-head {
  display: flex;
  align-items: center;
  gap: 6px;
  min-height: 28px;
}
.check-title {
  flex: 1;
  font-size: 12px;
  font-weight: 500;
  color: var(--muted);
}
.check-line {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin: 4px 0;
  font-size: 12.5px;
  line-height: 1.6;
}
.check-line :deep(svg) {
  flex-shrink: 0;
  margin-top: 3px;
}
.muted {
  color: var(--muted);
}
.warn {
  color: var(--warning);
}
.lib {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 6px 0;
}
.lib + .lib {
  border-top: 1px solid var(--separator);
}
.lib-icon {
  flex-shrink: 0;
  margin-top: 2px;
}
.lib-icon.ok {
  color: var(--success);
}
.lib-body {
  flex: 1;
  min-width: 0;
  font-size: 12.5px;
  line-height: 1.6;
}
.lib-name {
  font-weight: 500;
  color: var(--foreground);
}
.lib-issues {
  color: var(--warning);
}
.lib-ok {
  color: var(--muted);
  overflow-wrap: anywhere;
}
.check-foot {
  margin: 6px 0 2px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--muted);
}

@media (max-width: 1024px) {
  .layout {
    grid-template-columns: 1fr;
  }
  .libs {
    max-height: none;
  }
}
</style>
