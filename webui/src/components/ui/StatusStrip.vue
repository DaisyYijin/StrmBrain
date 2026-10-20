<script setup lang="ts">
import { ChevronRight } from '@lucide/vue'
import HSkeleton from '@/components/hero/HSkeleton.vue'

/**
 * 功能页顶部的一行状态条（约 48px 高）：几件事各一段，圆点颜色 = 好不好，悬停看细节，
 * 每段右边一个跳转链接。只看不做：维护者要求状态条上不放执行按钮。
 *
 * 原来只有 Strm 管理页有（StrmOverview），2026-10-20 自动整理页也要一条（参考 LitePan 每个任务页
 * 顶部先说「现在怎么样」），样式抽到这里共用。
 */
export type StripTone = 'ok' | 'warn' | 'danger' | 'off' | 'loading'
export interface StripItem {
  key: string
  title: string
  tone: StripTone
  /** 这一段那一行字：有问题先说问题，正常时给最有用的一两个数字 */
  text: string
  /** 悬停提示（多行用 \n） */
  tip?: string
  /** 右边链接的文字；不填就没有链接 */
  action?: string
}

defineProps<{ items: StripItem[] }>()
const emit = defineEmits<{ act: [key: string] }>()
</script>

<template>
  <div class="card card--default strip" :style="{ '--strip-n': items.length }">
    <div v-for="t in items" :key="t.key" class="seg" :class="`tone-${t.tone}`" :title="t.tip">
      <span class="seg-dot" />
      <span class="seg-title">{{ t.title }}</span>
      <HSkeleton v-if="t.tone === 'loading'" width="90px" height="10px" radius="999px" />
      <span v-else class="seg-text">{{ t.text }}</span>
      <span v-if="t.action" class="seg-act">
        <button type="button" class="seg-link" @click="emit('act', t.key)">
          {{ t.action }}<ChevronRight :size="13" />
        </button>
      </span>
    </div>
  </div>
</template>

<style scoped>
.strip {
  flex-direction: row;
  align-items: stretch;
  gap: 0;
  padding: 0;
  overflow: hidden;
}
.seg {
  --tone: var(--success);
  flex: 1 1 0;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  height: 48px;
  padding: 0 8px 0 16px;
  font-size: 13px;
}
.seg + .seg {
  border-left: 1px solid var(--separator);
}
.tone-warn {
  --tone: var(--warning);
}
.tone-danger {
  --tone: var(--danger);
}
.tone-off,
.tone-loading {
  --tone: var(--border-tertiary);
}
.seg-dot {
  width: 8px;
  height: 8px;
  flex-shrink: 0;
  border-radius: 999px;
  background: var(--tone);
  box-shadow: 0 0 0 3px color-mix(in oklab, var(--tone) 22%, transparent);
}
.seg-title {
  flex-shrink: 0;
  font-weight: 600;
  color: var(--foreground);
  white-space: nowrap;
}
.seg-text {
  min-width: 0;
  flex: 1 1 auto;
  color: var(--muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-variant-numeric: tabular-nums;
}
.tone-warn .seg-text {
  color: var(--warning-soft-foreground);
}
.tone-danger .seg-text {
  color: var(--danger);
}
.seg-act {
  margin-left: auto;
  flex-shrink: 0;
  display: flex;
}
.seg-link {
  display: inline-flex;
  align-items: center;
  height: 28px;
  padding: 0 8px;
  border: 0;
  border-radius: 8px;
  background: none;
  font: inherit;
  font-size: 12.5px;
  color: var(--muted);
  cursor: pointer;
  transition:
    background-color 150ms ease,
    color 150ms ease;
}
.seg-link:hover {
  background: var(--default);
  color: var(--accent);
}

/* 放不下一行时两列：每段仍是一行字。三段时第三段独占一行 */
@media (max-width: 1180px) {
  .strip {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .seg + .seg {
    border-left: 0;
  }
  .seg:nth-child(even) {
    border-left: 1px solid var(--separator);
  }
  .seg:nth-child(n + 3) {
    border-top: 1px solid var(--separator);
  }
  .seg:last-child:nth-child(odd) {
    grid-column: 1 / -1;
  }
}
@media (max-width: 600px) {
  .strip {
    grid-template-columns: 1fr;
  }
  .seg:nth-child(even) {
    border-left: 0;
  }
  .seg:nth-child(n + 2) {
    border-top: 1px solid var(--separator);
  }
}
</style>
