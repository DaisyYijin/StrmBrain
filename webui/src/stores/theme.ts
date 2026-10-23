import { defineStore } from 'pinia'
import { computed, ref, watchEffect } from 'vue'

export type ThemeMode = 'light' | 'dark' | 'auto'
/**
 * 亮色有两套配色：warm 是影院风的暖橙（默认），fresh 是冷调的清爽青蓝。
 * 暗色只有一套，配色只在亮色下生效（跟随系统且系统是亮色时同样生效）。
 */
export type LightPalette = 'warm' | 'fresh'

const STORAGE_KEY = 'ui.theme'
const PALETTE_KEY = 'ui.palette'
const WALL_KEY = 'ui.wall'
const media = window.matchMedia('(prefers-color-scheme: dark)')

// localStorage 在无痕模式 / 禁用站点数据时可能抛异常，读写都兜住
function read(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}
function write(key: string, v: string) {
  try {
    localStorage.setItem(key, v)
  } catch {
    // 存不下就只管这一次
  }
}

function readMode(): ThemeMode {
  const v = read(STORAGE_KEY)
  // 没选过的新访客默认暗色（影院风的主场）；index.html 首帧脚本同一口径
  return v === 'light' || v === 'dark' || v === 'auto' ? v : 'dark'
}
function readPalette(): LightPalette {
  return read(PALETTE_KEY) === 'fresh' ? 'fresh' : 'warm'
}

export const useThemeStore = defineStore('theme', () => {
  const mode = ref<ThemeMode>(readMode())
  const palette = ref<LightPalette>(readPalette())
  /**
   * 背景剧照（毛玻璃的「背后」）：整站背后铺一张 TMDB 剧照，卡片 / 侧栏 / 顶栏半透明透出它（layouts/AppBackdrop.vue）。
   * 没有剧照可铺（没配 TMDB）时不生效，页面照旧是实色
   */
  const wall = ref(read(WALL_KEY) !== '0')
  /** 可换的剧照有几张（AppBackdrop 读到列表后填）；不到两张时菜单里不出「换一张」 */
  const wallCount = ref(0)
  /** 每点一次「换一张背景」加一，AppBackdrop 盯着它切到下一张 */
  const wallSkip = ref(0)
  // 系统偏好单独跟踪：auto 模式下用户切换系统主题要立刻生效，不能只在启动时读一次
  const systemDark = ref(media.matches)
  media.addEventListener('change', (e) => {
    systemDark.value = e.matches
  })

  const isDark = computed(() => (mode.value === 'auto' ? systemDark.value : mode.value === 'dark'))

  watchEffect(() => {
    const root = document.documentElement
    root.classList.toggle('dark', isDark.value)
    // 配色标记常挂着，CSS 只在非 .dark 时读它（main.css 的 html[data-palette='fresh']:not(.dark)）
    root.dataset.palette = palette.value
    write(STORAGE_KEY, mode.value)
    write(PALETTE_KEY, palette.value)
    write(WALL_KEY, wall.value ? '1' : '0')
  })

  function setMode(next: ThemeMode) {
    mode.value = next
  }

  /** 选一套亮色配色，顺带切到亮色 —— 在暗色下点「清爽」却看不到变化会让人以为没生效 */
  function setLight(next: LightPalette) {
    palette.value = next
    mode.value = 'light'
  }

  function toggleWall() {
    wall.value = !wall.value
  }
  function nextWall() {
    wallSkip.value++
  }

  return { mode, palette, wall, wallCount, wallSkip, isDark, systemDark, setMode, setLight, toggleWall, nextWall }
})
