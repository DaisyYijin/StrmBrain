<script setup lang="ts">
import { nextTick, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Plus, Trash2 } from '@lucide/vue'
import HButton from '@/components/hero/HButton.vue'
import HInput from '@/components/hero/HInput.vue'
import HSelect from '@/components/hero/HSelect.vue'
import SectionCard from '@/components/ui/SectionCard.vue'
import FormActions from '@/components/ui/FormActions.vue'
import { libraryApi } from '@/api'
import type { ExtraDir, ExtraDirType } from '@/api/library'
import { toastError, useFeedback } from '@/composables/useFeedback'

/**
 * 其他媒体库目录：用户自己在媒体库里建的目录、改分类规则后留下的旧分类目录。
 * 只让海报墙 / 网盘文件页 / 对账认出里面的片目，整理不往这里放新片（后端 extradirs.go）。
 * 对账弹窗的「加入」和删分类规则时的「保留」也会往这里加，ver 变了就重读。
 */
const props = defineProps<{ ver?: number }>()
const { message } = useFeedback()

const rows = ref<ExtraDir[]>([])
const libs = ref<string[]>([])
const loading = ref(true)
const saving = ref(false)

const TYPE_OPTIONS: { label: string; value: ExtraDirType }[] = [
  { label: '电影', value: 'movie' },
  { label: '剧集', value: 'tv' },
  { label: '自动（有集号算剧集）', value: 'auto' },
]

async function read() {
  loading.value = true
  try {
    const d = await libraryApi.getExtraDirs()
    rows.value = d.dirs.map((r) => ({ ...r }))
    libs.value = d.libs || []
  } catch (e) {
    toastError(e, '读取失败')
  } finally {
    loading.value = false
  }
}

function addRow() {
  rows.value.push({ dir: '', type: 'movie' })
}

async function save() {
  saving.value = true
  try {
    // 限定库的库根条目 dir 本来就是空的，别当空行丢掉
    const d = await libraryApi.saveExtraDirs(rows.value.filter((r) => r.dir.trim() || r.lib))
    rows.value = d.dirs.map((r) => ({ ...r }))
    if (d.skipped?.length) message.warning(d.skipped.join('；'))
    else message.success('已保存，海报墙按新目录重新识别')
  } catch (e) {
    toastError(e, '保存失败')
  } finally {
    saving.value = false
  }
}

watch(() => props.ver, read)
// 海报墙「去登记目录」带着 #extra-dirs 跳过来：读完把卡片滚进视野（路由的 scrollBehavior 一律回顶部）
const route = useRoute()
onMounted(async () => {
  await read()
  if (route.hash === '#extra-dirs') {
    await nextTick()
    document.getElementById('extra-dirs')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
})
</script>

<template>
  <SectionCard id="extra-dirs" title="其他媒体库目录" hint="只用于识别，整理不会往里放">
    <p class="lead">
      自己在媒体库里建、不走分类规则的目录（例如 <code>LGNB电影/LGNB中国香港电影</code>），加到这里后海报墙、网盘文件页和对账就能认出里面的片目。
      写库内相对路径，<b>不带库名</b>{{ libs.length ? `（${libs.join('、')}）` : '' }}，可以多级。
      电影目录里直接放的视频一个算一部。整理选分类时不看这份清单，新整理的片子不会落进来。
    </p>

    <div v-if="loading" class="empty">读取中…</div>
    <template v-else>
      <div v-for="(r, i) in rows" :key="i" class="row">
        <!-- 只对某个库生效的条目（目前只能经接口登记，布局页做好后在那里管）：标出库名，库根条目不让改路径 -->
        <span v-if="r.lib" class="scope" :title="`只对库「${r.lib}」生效`">仅 {{ r.lib }}</span>
        <HInput
          v-model="r.dir"
          mono
          :disabled="!!r.lib && !r.dir"
          :placeholder="r.lib ? '（库根本身）' : '例如 纪录片 或 LGNB电影/港片'"
          class="dir"
        />
        <HSelect v-model="r.type" :options="TYPE_OPTIONS" aria-label="类型" class="type" />
        <HButton variant="ghost" size="sm" aria-label="删除" @click="rows.splice(i, 1)"><Trash2 :size="15" /></HButton>
      </div>
      <p v-if="!rows.length" class="empty">还没有。海报墙「与 Emby 对账」里列出的规则外目录可以一键加进来。</p>
      <HButton variant="tertiary" size="sm" class="add" @click="addRow"><Plus :size="14" />添加目录</HButton>
    </template>

    <FormActions>
      <HButton variant="primary" :loading="saving" :disabled="loading" @click="save">保存</HButton>
      <HButton variant="tertiary" @click="read">放弃修改</HButton>
    </FormActions>
  </SectionCard>
</template>

<style scoped>
.lead {
  margin: 0 0 12px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--muted);
}
.lead code {
  font-size: 12px;
  color: var(--foreground);
}
.row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.dir {
  flex: 1;
  min-width: 0;
}
.type {
  width: 200px;
  flex-shrink: 0;
}
.scope {
  flex: none;
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 2px 8px;
  border-radius: var(--r-sm);
  font-size: 12px;
  color: var(--muted);
  background: var(--surface-secondary);
}
.add {
  margin-top: 2px;
}
.empty {
  margin: 0 0 10px;
  font-size: 12px;
  color: var(--muted);
}
@media (max-width: 720px) {
  .row {
    flex-wrap: wrap;
  }
  .dir {
    flex-basis: 100%;
  }
  .type {
    flex: 1;
    width: auto;
  }
}
</style>
