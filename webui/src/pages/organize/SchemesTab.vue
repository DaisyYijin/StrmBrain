<script setup lang="ts">
/**
 * 二级分类：可以存多套「分类方案」（V2，MULTI-ACCOUNT-V2-PLAN.md §10.4，后端 catschemeapi.go）。
 * 每套就是一份 YAML，媒体库在库卡片上选用哪一套；被库用着的不能删。
 * 方案只管「怎么分目录」，不管「进哪个库」（规则里的 library 键作废：放进哪个库的转存文件夹就进哪个库）。
 */
import { computed, onMounted, ref } from 'vue'
import { Copy, Pencil, Plus, Trash2 } from '@lucide/vue'
import HButton from '@/components/hero/HButton.vue'
import HInput from '@/components/hero/HInput.vue'
import HModal from '@/components/hero/HModal.vue'
import { libraryApi } from '@/api'
import type { SchemeView } from '@/api/library'
import { toastError, useFeedback } from '@/composables/useFeedback'
import YamlRuleTab from './YamlRuleTab.vue'
import { DEFAULT_CATEGORY_YAML } from './defaultRules'

const { message, dialog } = useFeedback()
const schemes = ref<SchemeView[]>([])
const current = ref(0)
const loading = ref(true)

async function load() {
  try {
    const v = await libraryApi.getMediaLibs()
    schemes.value = v.schemes
    if (!schemes.value.some((s) => s.id === current.value)) current.value = schemes.value[0]?.id ?? 0
  } catch (e) {
    toastError(e, '读取分类方案失败')
  } finally {
    loading.value = false
  }
}
onMounted(load)

const cur = computed(() => schemes.value.find((s) => s.id === current.value))

const loadYaml = async () => {
  const d = await libraryApi.getScheme(current.value)
  return { config: d.yaml }
}
const persistYaml = async (yaml: string) => {
  const d = await libraryApi.updateScheme(current.value, { yaml })
  schemes.value = d.schemes
}

// ---- 新建 / 复制 / 改名 ----
const nameShow = ref(false)
const nameMode = ref<'new' | 'copy' | 'rename'>('new')
const nameText = ref('')
const nameBusy = ref(false)
function openName(mode: 'new' | 'copy' | 'rename') {
  nameMode.value = mode
  nameText.value = mode === 'rename' ? cur.value?.name || '' : mode === 'copy' ? `${cur.value?.name || ''} 副本` : ''
  nameShow.value = true
}
const nameTitle = computed(() => ({ new: '新建分类方案', copy: `复制「${cur.value?.name}」`, rename: `改名「${cur.value?.name}」` })[nameMode.value])
async function submitName() {
  nameBusy.value = true
  try {
    if (nameMode.value === 'rename') {
      schemes.value = (await libraryApi.updateScheme(current.value, { name: nameText.value })).schemes
    } else {
      // 新建 = 复制第一套（「默认」）当起点；复制 = 复制当前这套
      const d = await libraryApi.createScheme(nameText.value, nameMode.value === 'copy' ? current.value : 0)
      schemes.value = d.schemes
      current.value = d.id
    }
    nameShow.value = false
    message.success('已保存')
  } catch (e) {
    toastError(e, '保存失败')
  } finally {
    nameBusy.value = false
  }
}

async function remove() {
  const s = cur.value
  if (!s) return
  if (s.used_by.length) {
    message.error(`「${s.name}」还有媒体库在用（${s.used_by.join('、')}），先在库卡片上换一套再删`)
    return
  }
  const ok = await dialog.confirm({
    title: `删除分类方案「${s.name}」`,
    content: '删了就没了（没有库在用它）。',
    actions: [
      { label: '取消', value: false, variant: 'tertiary' },
      { label: '删除', value: true, variant: 'danger' },
    ],
  })
  if (!ok) return
  try {
    schemes.value = (await libraryApi.deleteScheme(s.id)).schemes
    current.value = schemes.value[0]?.id ?? 0
    message.success('已删除')
  } catch (e) {
    toastError(e, '删除失败')
  }
}
</script>

<template>
  <div class="schemes">
    <div class="bar">
      <div class="chips" role="tablist" aria-label="分类方案">
        <button
          v-for="s in schemes"
          :key="s.id"
          class="chip-btn"
          :class="{ on: s.id === current }"
          role="tab"
          :aria-selected="s.id === current"
          @click="current = s.id"
        >
          {{ s.name }}
          <span class="use">{{ s.used_by.length ? `${s.used_by.length} 个库在用` : '没有库在用' }}</span>
        </button>
      </div>
      <div class="ops">
        <HButton size="sm" variant="secondary" @click="openName('new')"><Plus :size="14" />新建</HButton>
        <HButton size="sm" variant="ghost" :disabled="!cur" @click="openName('copy')"><Copy :size="14" />复制</HButton>
        <HButton size="sm" variant="ghost" :disabled="!cur" @click="openName('rename')"><Pencil :size="14" />改名</HButton>
        <HButton size="sm" variant="ghost" :disabled="!cur || schemes.length <= 1" @click="remove"><Trash2 :size="14" />删除</HButton>
      </div>
    </div>
    <p v-if="cur" class="muted small">
      <template v-if="cur.used_by.length">用着它的库：{{ cur.used_by.join('、') }}。</template>
      <template v-else>还没有库用它：在「账号与媒体库 → 媒体库」的库卡片上选。</template>
      规则只管分目录；进哪个库看内容放进了哪个库的转存/待整理文件夹。
    </p>

    <YamlRuleTab
      v-if="cur"
      :key="`scheme-${cur.id}`"
      kind="category"
      :title="`分类方案「${cur.name}」`"
      hint="分类名即 115 目录名"
      note="分类名即 115 目录名，整理时目录不存在会自动创建。规则从上往下匹配，先命中的先停；不想手写 YAML 就用右上角的「添加分类规则」。"
      :load="loadYaml"
      :persist="persistYaml"
      :fallback="DEFAULT_CATEGORY_YAML"
    />
    <p v-else-if="!loading" class="muted">还没有分类方案，点「新建」建一套。</p>

    <HModal v-model:show="nameShow" :title="nameTitle" width="420px">
      <HInput v-model="nameText" placeholder="如 电影分类" :input-attrs="{ 'aria-label': '方案名' }" @enter="submitName" />
      <template #footer>
        <HButton variant="tertiary" @click="nameShow = false">取消</HButton>
        <HButton variant="primary" :loading="nameBusy" @click="submitName">保存</HButton>
      </template>
    </HModal>
  </div>
</template>

<style scoped>
.schemes {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.chip-btn {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  padding: 6px 12px;
  border: 0;
  border-radius: 999px;
  background: var(--default);
  color: var(--foreground);
  font-size: 13px;
  cursor: pointer;
}
.chip-btn.on {
  background: var(--accent);
  color: var(--accent-foreground);
}
.chip-btn .use {
  font-size: 11.5px;
  opacity: 0.75;
}
.ops {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.muted {
  color: var(--muted);
}
.small {
  margin: 0;
  font-size: 12.5px;
}
</style>
