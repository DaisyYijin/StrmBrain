<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import HButton from '@/components/hero/HButton.vue'
import SectionCard from '@/components/ui/SectionCard.vue'
import { libraryApi } from '@/api'
import type { ExtraDir } from '@/api/library'
import { toastError } from '@/composables/useFeedback'
import { LAYOUT_SOURCE_LABEL, layoutDirLabel } from '@/components/library/layout'

/**
 * 其他媒体库目录的只读摘要（2026-10-14 起）。增删改都在「账号与媒体库 → 媒体库布局」
 * （components/library/LibraryLayoutCard.vue）：两处都能改就是两份整存整取、互相覆盖（incr.cron 同款坑），
 * 而且那边每次改动先预览后果。删分类规则时的「保留」（CategoryDropDialog）仍往清单里追加，ver 变了重读。
 */
const props = defineProps<{ ver?: number }>()
const router = useRouter()

const rows = ref<ExtraDir[]>([])
const loading = ref(true)

async function read() {
  loading.value = true
  try {
    rows.value = (await libraryApi.getExtraDirs()).dirs
  } catch (e) {
    toastError(e, '读取失败')
  } finally {
    loading.value = false
  }
}

const SHOW = 8
const shown = computed(() => rows.value.slice(0, SHOW))

function goLayout() {
  void router.push({ name: 'accounts', hash: '#library-layout' })
}

watch(() => props.ver, read)
onMounted(read)
</script>

<template>
  <SectionCard id="extra-dirs" title="其他媒体库目录" hint="只用于识别，整理不会往里放">
    <template #extra>
      <HButton variant="tertiary" size="sm" @click="goLayout">去媒体库布局修改</HButton>
    </template>
    <p class="lead">
      分类规则以外、用来认片目的目录（自己建的目录、改规则后留下的旧分类目录）。整理选分类时不看这份清单，新整理的片子不会落进来。
      增删改在「账号与媒体库 → 媒体库布局」，那里会先预览改动会多认出哪些片目。
    </p>
    <p v-if="loading" class="empty">读取中…</p>
    <p v-else-if="!rows.length" class="empty">还没有。</p>
    <ul v-else class="list">
      <li v-for="r in shown" :key="`${r.lib}/${r.dir}`">
        <code>{{ layoutDirLabel(r.lib ?? '', r.dir) }}</code>
        <span class="src">{{ LAYOUT_SOURCE_LABEL[r.source || 'manual'] }}</span>
      </li>
      <li v-if="rows.length > SHOW" class="more">等共 {{ rows.length }} 个</li>
    </ul>
  </SectionCard>
</template>

<style scoped>
.lead {
  margin: 0 0 10px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--muted);
}
.empty,
.more {
  margin: 0;
  font-size: 12px;
  color: var(--muted);
}
.list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
}
.list li {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
}
.src {
  font-size: 11.5px;
  color: var(--muted);
}
</style>
