<script setup lang="ts">
/**
 * Strm 管理三个页签顶部的一行概况（2026-10-23）：每格一个结论 + 一句补充，右边可放一个执行按钮。
 * 和页面最上面的 StatusStrip 不同：那条只看不做、四件事各一段；这里只讲本页签这一件事，细一些
 */
export interface SummaryItem {
  key: string
  label: string
  value: string
  sub?: string
  /** 有 tone 的格子带一个状态点、数值按状态上色 */
  tone?: 'ok' | 'warn' | 'danger' | 'off'
}

defineProps<{ items: SummaryItem[] }>()
</script>

<template>
  <div class="sum">
    <div v-for="it in items" :key="it.key" class="sum-item" :class="it.tone && `tone-${it.tone}`">
      <span class="sum-label">{{ it.label }}</span>
      <span class="sum-value"><span v-if="it.tone" class="sum-dot" /><span class="sum-text">{{ it.value }}</span></span>
      <span v-if="it.sub" class="sum-sub" :title="it.sub">{{ it.sub }}</span>
    </div>
    <div v-if="$slots.default" class="sum-act"><slot /></div>
  </div>
</template>

<style scoped>
.sum {
  display: flex;
  flex-wrap: wrap;
  align-items: stretch;
  border-radius: var(--r-card);
  background: var(--surface);
  box-shadow: var(--surface-shadow);
  overflow: hidden;
}
.sum-item {
  --tone: var(--success);
  flex: 1 1 180px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 14px 18px;
}
.sum-item + .sum-item {
  border-left: 1px solid var(--separator);
}
.tone-warn {
  --tone: var(--warning);
}
.tone-danger {
  --tone: var(--danger);
}
.tone-off {
  --tone: var(--border-tertiary);
}
.sum-label {
  font-size: 12px;
  color: var(--muted);
}
.sum-value {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 16px;
  font-weight: 600;
  color: var(--foreground);
  font-variant-numeric: tabular-nums;
}
/* 省略号只加在文字上：加在整行上时 overflow 会把状态点外面那圈 3px 的光晕裁掉左半边 */
.sum-text {
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.tone-warn .sum-value {
  color: var(--warning-soft-foreground);
}
.tone-danger .sum-value {
  color: var(--danger);
}
.sum-dot {
  margin-left: 3px;
  width: 8px;
  height: 8px;
  flex-shrink: 0;
  border-radius: 999px;
  background: var(--tone);
  box-shadow: 0 0 0 3px color-mix(in oklab, var(--tone) 22%, transparent);
}
.sum-sub {
  font-size: 12px;
  color: var(--muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sum-act {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  padding: 14px 18px;
  border-left: 1px solid var(--separator);
}
@media (max-width: 720px) {
  .sum {
    flex-direction: column;
  }
  .sum-item + .sum-item,
  .sum-act {
    border-left: 0;
    border-top: 1px solid var(--separator);
  }
}
</style>
