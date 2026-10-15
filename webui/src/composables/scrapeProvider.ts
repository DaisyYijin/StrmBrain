import { computed, ref } from 'vue'
import { organizeApi } from '@/api'
import type { LibScrapeView } from '@/api/organize'

/**
 * 每个库刮不刮（auto 自动刮新片 / manual 只手动刮 / off 不刮，交给 Emby）。
 * 2026-10-15 起按库设（后端 libopts.go），改在「账号与媒体库 → 媒体库」的库卡片上；
 * 影视刮削页、海报墙、自动整理的只读概览都要看它，放在模块级，一处读了别处不必重读。
 * 单个片目按哪种口径看由后端直接标在卡片上（LocalTitle.emby_graded），这里只给整体情况。
 */
const libs = ref<LibScrapeView[]>([])
const loaded = ref(false)
let pending: Promise<void> | null = null

async function load(force = false) {
  if (loaded.value && !force) return
  if (pending) return pending
  pending = (async () => {
    try {
      const res = await organizeApi.getScrapeConfig()
      libs.value = res.libs ?? []
    } catch {
      // 读不到按本站刮削（老行为）
    } finally {
      loaded.value = true
      pending = null
    }
  })()
  return pending
}

const every = (m: LibScrapeView['scrape']) => computed(() => libs.value.length > 0 && libs.value.every((l) => l.scrape === m))
const some = (m: LibScrapeView['scrape']) => computed(() => libs.value.some((l) => l.scrape === m))

const embyScrapes = every('off')
const anyEmby = some('off')
const anyAuto = some('auto')

export function useScrapeProvider() {
  return {
    libs,
    loaded,
    /** 所有库都「不刮（交给 Emby）」：本站不写任何元数据 */
    embyScrapes,
    /** 有库「不刮（交给 Emby）」 */
    anyEmby,
    /** 有库「自动刮新片」：整理 / 同步后刮、定时补刮只管这些库 */
    anyAuto,
    load,
  }
}
