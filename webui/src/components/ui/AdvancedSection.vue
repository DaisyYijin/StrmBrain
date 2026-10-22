<script setup lang="ts">
/**
 * 「高级设置」折叠区：不常改、改错了反而出问题的选项收在这里，新用户只看到必要的几项。
 *
 * 收起时**改过默认值的要说出来**（changed 计数）：藏起来的改动最难排查。
 * 展开状态按 id 记在浏览器里（只是个人习惯，读写失败照常用）。
 */
import { ref, watch } from 'vue'
import { ChevronDown, SlidersHorizontal } from '@lucide/vue'

const props = defineProps<{
  /** 记住展开状态用，全站唯一 */
  id: string
  /** 里面有几项和默认值不一样 */
  changed?: number
  /** 折叠条上的补充说明 */
  hint?: string
}>()

const storeKey = `adv:${props.id}`
function readOpen(): boolean {
  try {
    return localStorage.getItem(storeKey) === '1'
  } catch {
    return false
  }
}
const open = ref(readOpen())
watch(open, (v) => {
  try {
    if (v) localStorage.setItem(storeKey, '1')
    else localStorage.removeItem(storeKey)
  } catch {
    /* 隐私模式等：只是记不住，不影响使用 */
  }
})
</script>

<template>
  <div class="adv" :class="{ 'is-open': open }">
    <button type="button" class="adv-bar" :aria-expanded="open" @click="open = !open">
      <SlidersHorizontal :size="14" />
      <span class="adv-title">高级设置</span>
      <!-- 说的是「和默认值不一样」，不是「没保存」：原来写「N 项已改动」配警告色，保存后还挂着，被读成没存上 -->
      <span v-if="changed" class="adv-changed" title="这几项用的不是默认值（已保存的也算）">{{ changed }} 项非默认</span>
      <span v-else-if="hint && !open" class="adv-hint">{{ hint }}</span>
      <ChevronDown :size="15" class="adv-caret" />
    </button>
    <div v-if="open" class="adv-body"><slot /></div>
  </div>
</template>

<style scoped>
.adv {
  margin-top: 4px;
  border-top: 1px dashed var(--separator);
}
.adv-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 10px 0 6px;
  border: 0;
  background: none;
  font: inherit;
  font-size: 13px;
  color: var(--muted);
  cursor: pointer;
  text-align: left;
}
.adv-bar:hover {
  color: var(--foreground);
}
.adv-title {
  font-weight: 500;
}
.adv-changed {
  padding: 0 6px;
  border-radius: var(--r-sm);
  font-size: 11.5px;
  color: var(--muted);
  background: var(--default);
}
.adv-hint {
  min-width: 0;
  overflow: hidden;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.adv-caret {
  margin-left: auto;
  flex-shrink: 0;
  transition: transform 0.2s;
}
.is-open .adv-caret {
  transform: rotate(180deg);
}
</style>
