<script setup lang="ts">
import { ChevronRight, FolderInput } from '@lucide/vue'
import SectionCard from '@/components/ui/SectionCard.vue'
import { useStagingSubs } from '@/composables/stagingSubs'

/**
 * 转存入口（按来源分库）现在在「账号与媒体库 → 媒体库」的库卡片上开（2026-10-15，MULTI-LIB-SIMPLIFY-PLAN.md §7-2）。
 * 这里只列出现有的入口、给跳转：两处都能改的话，整存整取会互相覆盖（incr.cron 同款坑）。
 * 小节本身留着：收藏的 ?sec=folder 还要能打开
 */
const { items, loaded } = useStagingSubs()
const LINK = { name: 'accounts', query: { tab: 'library' } }
</script>

<template>
  <SectionCard title="转存入口" hint="按来源分库：放进某个子目录的内容，整理时直接落进对应的媒体库">
    <p class="lead">
      在「账号与媒体库 → 媒体库」里给某个库打开「转存入口」，会在转存目录下建一个子目录。找资源、链接转存、订阅时选「转存到 那个库」，
      或者在手机 / 网盘客户端上直接把文件放进那个子目录，整理时就进那个库，不看分类规则。
    </p>
    <ul v-if="loaded && items.length" class="list">
      <li v-for="s in items" :key="s.cid">
        <FolderInput :size="14" />转存 / {{ s.name }}<span class="arrow">→</span>{{ s.lib_name || '媒体库' }}
        <span v-if="s.problem" class="problem">{{ s.problem }}</span>
      </li>
    </ul>
    <p v-else-if="loaded" class="empty">还没有转存入口，转存目录里的内容都按分类规则选库。</p>
    <RouterLink :to="LINK" class="link">去「账号与媒体库 → 媒体库」设置<ChevronRight :size="14" /></RouterLink>
  </SectionCard>
</template>

<style scoped>
.lead {
  margin: 0 0 12px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--muted);
}
.list {
  margin: 0 0 12px;
  padding: 0;
  list-style: none;
  font-size: 13px;
  line-height: 2;
}
.list li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.arrow {
  color: var(--muted);
}
.problem {
  flex-basis: 100%;
  font-size: 12px;
  line-height: 1.5;
  color: var(--danger);
}
.empty {
  margin: 0 0 12px;
  font-size: 12px;
  color: var(--muted);
}
.link {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  font-size: 13px;
  color: var(--accent);
}
</style>
