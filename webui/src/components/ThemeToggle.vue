<script setup lang="ts">
import { computed } from 'vue'
import { Droplets, Image, Moon, Shuffle, Sun, SunMoon } from '@lucide/vue'
import { useThemeStore } from '@/stores/theme'
import HButton from '@/components/hero/HButton.vue'
import HDropdown from '@/components/hero/HDropdown.vue'

/**
 * 主题选择：暗色 / 两套亮色配色 / 跟随系统。
 * 原来是点一下轮换一档，多了一套配色之后轮换要点四下才转回来，改成下拉直接选。
 * 最后一项「背景剧照」是独立开关，管毛玻璃背后有没有图（AppBackdrop.vue）。
 */
const theme = useThemeStore()

const current = computed(() => {
  if (theme.mode === 'auto') return { icon: SunMoon, label: '跟随系统' }
  if (theme.mode === 'dark') return { icon: Moon, label: '暗色' }
  return theme.palette === 'fresh'
    ? { icon: Droplets, label: '亮色 · 清爽' }
    : { icon: Sun, label: '亮色 · 暖橙' }
})

const options = computed(() => [
  { key: 'dark', label: '暗色', icon: Moon, checked: theme.mode === 'dark' },
  { key: 'warm', label: '亮色 · 暖橙', icon: Sun, checked: theme.mode === 'light' && theme.palette === 'warm' },
  { key: 'fresh', label: '亮色 · 清爽', icon: Droplets, checked: theme.mode === 'light' && theme.palette === 'fresh' },
  { key: 'auto', label: '跟随系统', icon: SunMoon, checked: theme.mode === 'auto' },
  // 不是单选里的一档，是独立开关：打勾 = 开着
  { key: 'wall', label: '背景剧照（毛玻璃）', icon: Image, checked: theme.wall },
  // 不喜欢当前这张：手动换下一张（开着背景剧照、且有两张以上时才出）
  ...(theme.wall && theme.wallCount > 1 ? [{ key: 'wall-next', label: '换一张背景', icon: Shuffle }] : []),
])

function onSelect(key: string) {
  if (key === 'dark' || key === 'auto') theme.setMode(key)
  else if (key === 'warm' || key === 'fresh') theme.setLight(key)
  else if (key === 'wall') theme.toggleWall()
  else if (key === 'wall-next') theme.nextWall()
}
</script>

<template>
  <HDropdown :options="options" @select="onSelect">
    <HButton variant="ghost" icon-only :aria-label="`主题：${current.label}`" :title="`主题：${current.label}`">
      <component :is="current.icon" :size="18" />
    </HButton>
  </HDropdown>
</template>
