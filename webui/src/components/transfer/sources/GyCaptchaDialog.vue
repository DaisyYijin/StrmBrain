<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RefreshCw } from '@lucide/vue'
import HModal from '@/components/hero/HModal.vue'
import HButton from '@/components/hero/HButton.vue'
import type { GyCaptcha } from '@/api/resources'

/**
 * 观影的点选验证码：站点要求按顺序点图里的几个字。坐标按站点画布尺寸（350×200）换算后交给父组件，
 * 由父组件带着它重新登录；点错了父组件会换一张新图进来（captcha 变了就清空已点的）。
 */
const props = defineProps<{ captcha: GyCaptcha | null; busy?: boolean; error?: string }>()
const emit = defineEmits<{ submit: [points: number[][]]; refresh: [] }>()
const open = defineModel<boolean>('show', { required: true })

const points = ref<number[][]>([])
// 标记按图片的显示位置画（百分比），提交时才换算成站点坐标
const marks = ref<{ left: number; top: number }[]>([])
const chars = computed(() => Array.from(props.captcha?.text ?? ''))

watch(
  () => props.captcha,
  () => {
    points.value = []
    marks.value = []
  },
)

function onClick(e: MouseEvent) {
  const cap = props.captcha
  if (!cap || props.busy || points.value.length >= chars.value.length) return
  const el = e.currentTarget as HTMLElement
  const rect = el.getBoundingClientRect()
  const rx = (e.clientX - rect.left) / rect.width
  const ry = (e.clientY - rect.top) / rect.height
  points.value.push([Math.round(rx * cap.w), Math.round(ry * cap.h)])
  marks.value.push({ left: rx * 100, top: ry * 100 })
  if (points.value.length === chars.value.length) emit('submit', points.value.slice())
}

// 点标记撤回到这一步（站点前端同款）
function undo(i: number) {
  if (props.busy) return
  points.value.splice(i)
  marks.value.splice(i)
}
</script>

<template>
  <HModal v-model:show="open" title="观影登录验证" width="420px" persistent>
    <div v-if="captcha" class="gy-cap">
      <p class="gy-cap-title">
        请依次点击
        <template v-for="(c, i) in chars" :key="i">
          <span :class="['gy-cap-char', i < points.length && 'done']">“{{ c }}”</span>
          <template v-if="i < chars.length - 1">、</template>
        </template>
      </p>
      <div class="gy-cap-img" @click="onClick">
        <img :src="captcha.img" alt="验证码" draggable="false" />
        <span
          v-for="(m, i) in marks"
          :key="i"
          class="gy-cap-mark"
          :style="{ left: m.left + '%', top: m.top + '%' }"
          @click.stop="undo(i)"
        >
          {{ i + 1 }}
        </span>
      </div>
      <p v-if="error" class="gy-cap-err">{{ error }}</p>
      <p class="gy-cap-tip">点错了可以点数字撤回。验证码由观影站点出，本站只负责转交。</p>
    </div>
    <template #footer>
      <HButton variant="tertiary" :loading="busy" @click="emit('refresh')">
        <RefreshCw :size="14" />
        换一张
      </HButton>
    </template>
  </HModal>
</template>

<style scoped>
.gy-cap {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.gy-cap-title {
  margin: 0;
  font-size: 14px;
}
.gy-cap-char {
  font-weight: 600;
}
.gy-cap-char.done {
  color: var(--accent);
}
.gy-cap-img {
  position: relative;
  width: 100%;
  max-width: 350px;
  aspect-ratio: 350 / 200;
  cursor: crosshair;
  user-select: none;
  border-radius: var(--r-sm);
  overflow: hidden;
  align-self: center;
}
.gy-cap-img img {
  display: block;
  width: 100%;
  height: 100%;
}
.gy-cap-mark {
  position: absolute;
  width: 24px;
  height: 24px;
  margin: -12px 0 0 -12px;
  border-radius: 50%;
  background: var(--accent);
  color: var(--accent-foreground);
  font-size: 13px;
  line-height: 24px;
  text-align: center;
  cursor: pointer;
}
.gy-cap-err {
  margin: 0;
  color: var(--danger);
  font-size: 13px;
}
.gy-cap-tip {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
}
</style>
