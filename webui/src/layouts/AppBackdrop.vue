<script setup lang="ts">
/**
 * 整站背后的剧照（毛玻璃的「背后」，2026-10-23，参考 MoviePilot 玻璃主题的 background-container）。
 *
 * 原来毛玻璃只给浮层用、页面底是实色：玻璃背后什么都没有，模糊了也看不出来（维护者反馈「完全没看到」）。
 * 现在背后固定铺一张剧照、压暗（亮色下提亮），页面底色透明，卡片 / 侧栏 / 顶栏半透明 + 背景模糊透出它。
 *
 * 剧照复用登录页那一份（/auth/wallpapers，后端预取转码好的，TMDB 不通也有图，见 loginwall.go）：
 * 先铺列表自带的 32px 小图，大图在内存里加载好再淡入；之后每分钟换一张，拉不到就跳过这一轮。
 * 一张都没有、或用户在主题菜单里关了「背景剧照」，就不挂 html.has-wall，全站照旧是实色。
 * 透明与模糊的规则都挂在 html.has-wall 下（main.css「背景剧照」一节）。
 */
import { onBeforeUnmount, ref, watch } from 'vue'
import { authApi } from '@/api'
import type { LoginWallpaper } from '@/api/auth'
import { useThemeStore } from '@/stores/theme'

const theme = useThemeStore()
/** 比登录页慢：后台页面上图一直在换会分心 */
const WALL_INTERVAL = 60_000

interface WallLayer {
  thumb: string
  full: string
}
const layers = ref<[WallLayer, WallLayer]>([
  { thumb: '', full: '' },
  { thumb: '', full: '' },
])
const front = ref(0)
const shown = ref(false)
let walls: LoginWallpaper[] = []
let idx = 0
let timer: number | undefined
let alive = true
const reducedTransparency = () => window.matchMedia?.('(prefers-reduced-transparency: reduce)').matches ?? false

/** 背后的图大半被卡片盖着、又压了暗角，不按设备像素比要更大的那档 */
function wallUrl(w: LoginWallpaper) {
  if (w.path.startsWith('data:')) return w.path // 假数据模式（api/mock.ts）直接给图，真实数据是 TMDB 路径
  const need = Math.max(window.innerWidth, (window.innerHeight * 16) / 9)
  return `/api/auth/wallpaper?path=${encodeURIComponent(w.path)}&w=${Math.round(need)}`
}
function loadImage(url: string) {
  return new Promise<boolean>((resolve) => {
    const img = new Image()
    img.onload = () => resolve(true)
    img.onerror = () => resolve(false)
    img.src = url
  })
}

async function showFirst(i: number) {
  const w = walls[i]
  layers.value[front.value] = { thumb: w.thumb, full: '' }
  shown.value = true
  const url = wallUrl(w)
  if ((await loadImage(url)) && alive && idx === i) layers.value[front.value].full = url
}
async function showNext(i: number) {
  const w = walls[i]
  const url = wallUrl(w)
  if (!(await loadImage(url)) || !alive || !theme.wall) return
  const next = 1 - front.value
  layers.value[next] = { thumb: w.thumb, full: url }
  front.value = next
}

async function start() {
  if (!walls.length) {
    try {
      walls = (await authApi.wallpapers()).items ?? []
    } catch {
      walls = [] // 背景图只是点缀，拿不到不提示
    }
  }
  // 系统要求减少透明度：不铺图，页面保持实色（毛玻璃令牌在 main.css 里同样退回实色）
  if (!alive || !theme.wall || !walls.length || reducedTransparency()) return
  theme.wallCount = walls.length
  idx = Math.floor(Math.random() * walls.length)
  void showFirst(idx)
  restartTimer()
}
function restartTimer() {
  window.clearInterval(timer)
  if (walls.length > 1) {
    timer = window.setInterval(() => {
      if (document.hidden) return // 标签页在后台不换，省流量
      idx = (idx + 1) % walls.length
      void showNext(idx)
    }, WALL_INTERVAL)
  }
}

/** 主题菜单「换一张背景」：切到下一张，并重新计时，免得刚换上就被自动轮换顶掉 */
watch(
  () => theme.wallSkip,
  () => {
    if (!shown.value || walls.length < 2) return
    idx = (idx + 1) % walls.length
    void showNext(idx)
    restartTimer()
  },
)
function stop() {
  window.clearInterval(timer)
  shown.value = false
}

watch(
  () => theme.wall,
  (on) => (on ? void start() : stop()),
  { immediate: true },
)
// 有图铺着才让页面透明：不然半透明卡片底下是实色，比原来还灰
watch(shown, (v) => document.documentElement.classList.toggle('has-wall', v), { immediate: true })

onBeforeUnmount(() => {
  alive = false
  stop()
  document.documentElement.classList.remove('has-wall')
})
</script>

<template>
  <div v-if="shown" class="app-backdrop" aria-hidden="true">
    <div v-for="(l, i) in layers" :key="i" class="wall" :class="{ 'is-front': i === front }">
      <img v-if="l.thumb" class="wall-thumb" :src="l.thumb" alt="" />
      <img v-if="l.full" class="wall-full" :src="l.full" alt="" />
    </div>
    <div class="wall-shade" />
  </div>
</template>

<style scoped>
/* 负的 z-index：画在根背景之上、所有内容之下。前提是祖先都没开层叠上下文，所以挂在 .layout 里第一个 */
.app-backdrop {
  position: fixed;
  inset: 0;
  z-index: -1;
  overflow: hidden;
  pointer-events: none;
  background: var(--background);
}
.wall {
  position: absolute;
  inset: 0;
  opacity: 0;
  transition: opacity 1.5s ease;
}
.wall.is-front {
  opacity: 1;
}
.wall img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
}
/* 小图放大会有马赛克：糊掉它，大图到了盖在上面 */
.wall-thumb {
  filter: blur(24px) saturate(0.8);
  transform: scale(1.08);
}
.wall-full {
  animation: wall-in 0.8s ease both;
}
/* 画面本身也降一点饱和度：鲜艳的剧照透过玻璃仍然花 */
.wall img {
  filter: saturate(0.8);
}
@keyframes wall-in {
  from {
    opacity: 0;
  }
}
/* 压一层页面底色：暗色压暗、亮色洗白，字不至于压在高对比的画面上。
   上浅下深，顶栏一带透出的画面多一点。第一版（38%→62%）画面太跳、看久了累眼睛，压重了 */
.wall-shade {
  position: absolute;
  inset: 0;
  background: linear-gradient(
    color-mix(in oklab, var(--background) 58%, transparent),
    color-mix(in oklab, var(--background) 80%, transparent)
  );
}
html:not(.dark) .wall-shade {
  background: linear-gradient(
    color-mix(in oklab, var(--background) 66%, transparent),
    color-mix(in oklab, var(--background) 84%, transparent)
  );
}
</style>
