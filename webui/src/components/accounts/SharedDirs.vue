<script setup lang="ts">
/**
 * 媒体库页签顶上的一条「所有库共用」：本地媒体根（只读）、已存在、冗余。
 * 原来右栏一张工作目录卡只放两行、占掉半屏，库卡片底部又重复写了一遍这两个路径；并成这一条。
 *
 * 已存在 / 冗余在「自动整理 → 基础配置」里改（org-basic 整存整取，这里不放表单，只跳过去），
 * 一键创建走 InitWorkspaceDirs（只补没配的）。待整理并进转存时弃用的旧目录（stagingdir.go）也在这里提示。
 */
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { Archive, ChevronRight, Copy, FolderPlus, HardDrive } from '@lucide/vue'
import HAlert from '@/components/hero/HAlert.vue'
import HButton from '@/components/hero/HButton.vue'
import { configApi, organizeApi } from '@/api'
import { toastError, useFeedback } from '@/composables/useFeedback'

const props = defineProps<{ mediaRoot: string; hasLibs: boolean }>()
const { message, dialog } = useFeedback()

const org = ref<Record<string, string> | null>(null)
const legacy = ref('')
const creating = ref(false)

async function load() {
  try {
    const [o, l] = await Promise.all([
      configApi.getSetting<Record<string, string>>('org-basic', {}),
      configApi.getSetting<{ cid?: string; path?: string }>('staging_legacy', {}),
    ])
    org.value = o
    legacy.value = l.cid ? l.path || `cid ${l.cid}` : ''
  } catch {
    org.value = null
  }
}
onMounted(load)

const ORG_LINK = { name: 'organize', query: { tab: 'basic' } }
const slots = computed(() => {
  const o = org.value ?? {}
  const one = (key: string, label: string, desc: string, icon: typeof Copy) => {
    const cid = o[key]
    const set = !!cid && cid !== '0'
    return { key, label, desc, icon, set, path: set ? o[`${key}_path`] || `cid ${cid}` : '' }
  }
  return [
    one('existing', '已存在', '洗版判输、重复的文件', Copy),
    one('redundant', '冗余', '被新版本换下来的旧文件', Archive),
  ]
})
/** 一键创建时的子目录名（与后端 wsSubDirs 一致） */
const missing = computed(() => (org.value ? slots.value.filter((s) => !s.set).map((s) => s.label) : []))

async function create() {
  const ok = await dialog.confirm({
    title: '一键创建工作目录',
    content: `将在 115 网盘根目录下创建 /StrmStation/{${missing.value.join('、')}} 并写入配置。已配置的目录保持不动，同名目录已存在则直接复用。`,
    actions: [
      { label: '取消', value: false, variant: 'tertiary' },
      { label: '创建', value: true, variant: 'primary' },
    ],
  })
  if (!ok) return
  creating.value = true
  try {
    message.success((await organizeApi.initWorkspace()).message)
    await load()
  } catch (e) {
    toastError(e, '创建失败')
  } finally {
    creating.value = false
  }
}

async function dismissLegacy() {
  try {
    await configApi.saveSetting('staging_legacy', {})
    legacy.value = ''
  } catch (e) {
    toastError(e, '操作失败')
  }
}
</script>

<template>
  <section class="card card--default shared">
    <div class="cell">
      <span class="ico"><HardDrive :size="16" :stroke-width="1.8" /></span>
      <div class="body">
        <div class="label">本地媒体根</div>
        <code class="path" :title="mediaRoot">{{ mediaRoot }}</code>
        <div class="desc">容器环境变量 MEDIA_ROOT，各库的本地目录都在它下面</div>
      </div>
    </div>

    <div v-for="s in slots" :key="s.key" class="cell" :class="{ 'is-missing': org && !s.set }">
      <span class="ico"><component :is="s.icon" :size="16" :stroke-width="1.8" /></span>
      <div class="body">
        <div class="label">
          {{ s.label }}
          <span v-if="org && !s.set" class="miss">未配置</span>
        </div>
        <code v-if="s.set" class="path" :title="s.path">{{ s.path }}</code>
        <div class="desc">{{ s.desc }}<template v-if="s.set">；每个账号同一路径下各一份</template></div>
      </div>
      <RouterLink :to="ORG_LINK" class="go" :aria-label="`去设置${s.label}目录`">
        {{ s.set ? '修改' : '去设置' }}<ChevronRight :size="14" />
      </RouterLink>
    </div>

    <div v-if="props.hasLibs && missing.length" class="create">
      <HButton variant="secondary" size="sm" :loading="creating" @click="create">
        <template #icon><FolderPlus :size="15" /></template>
        一键创建缺少的
      </HButton>
    </div>
  </section>

  <HAlert v-if="legacy" status="warning" title="待整理已并进转存目录">
    原来的待整理目录（{{ legacy }}）不再自动整理。里面如果还有要整理的，请移到对应媒体库的转存/待整理文件夹。
    <template #actions>
      <HButton variant="tertiary" size="sm" @click="dismissLegacy">知道了</HButton>
    </template>
  </HAlert>
</template>

<style scoped>
.shared {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr)) auto;
  flex-direction: row;
  align-items: center;
  gap: 0;
  padding: 0;
}
.cell {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  padding: 14px 18px;
  border-left: 1px solid var(--separator);
}
.cell:first-child {
  border-left: 0;
}
.ico {
  width: 32px;
  height: 32px;
  flex-shrink: 0;
  border-radius: 10px;
  display: grid;
  place-items: center;
  background: var(--accent-soft);
  color: var(--accent);
}
.is-missing .ico {
  background: var(--warning-soft);
  color: var(--warning);
}
.body {
  flex: 1;
  min-width: 0;
}
.label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--muted);
}
.miss {
  padding: 0 6px;
  border-radius: 999px;
  background: var(--warning-soft);
  color: var(--warning-soft-foreground);
  font-size: 11px;
  line-height: 17px;
}
.path {
  display: block;
  margin-top: 1px;
  font-family: var(--font-mono);
  font-size: 13px;
  font-weight: 600;
  color: var(--foreground);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.desc {
  margin-top: 1px;
  font-size: 11.5px;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.go {
  display: inline-flex;
  align-items: center;
  flex-shrink: 0;
  font-size: 12.5px;
  color: var(--muted);
  text-decoration: none;
}
.go:hover,
.is-missing .go {
  color: var(--accent);
}
.create {
  padding: 0 18px;
}

@media (max-width: 1080px) {
  .shared {
    grid-template-columns: 1fr;
  }
  .cell {
    border-left: 0;
    border-top: 1px solid var(--separator);
  }
  .cell:first-child {
    border-top: 0;
  }
  .desc {
    white-space: normal;
  }
  .create {
    padding: 0 18px 14px;
  }
}
</style>
