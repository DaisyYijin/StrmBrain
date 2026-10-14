<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Plus, Trash2 } from '@lucide/vue'
import HButton from '@/components/hero/HButton.vue'
import HInput from '@/components/hero/HInput.vue'
import HSelect from '@/components/hero/HSelect.vue'
import SectionCard from '@/components/ui/SectionCard.vue'
import FormActions from '@/components/ui/FormActions.vue'
import { transferApi } from '@/api'
import { toastError, useFeedback } from '@/composables/useFeedback'
import { useStagingSubs } from '@/composables/stagingSubs'

/**
 * 按来源分库：转存目录下的子目录绑一个媒体库（后端 stagingsub.go）。
 * 整理时子目录里的条目落进绑定的库（分类目录照规则算），优先于分类规则的 library。
 * 只支持转存目录下一层的子目录
 */
const { message } = useFeedback()
const subs = useStagingSubs()

type Row = { cid: string; lib: string; problem?: string }
const rows = ref<Row[]>([])
const dirs = ref<{ cid: string; name: string }[]>([])
const loading = ref(true)
const saving = ref(false)
const newName = ref('')
const creating = ref(false)

async function read() {
  loading.value = true
  try {
    await subs.reload()
    rows.value = subs.items.value.map((s) => ({ cid: s.cid, lib: s.lib, problem: s.problem }))
    const d = await transferApi.stagingSubdirs().catch(() => ({ dirs: [] }))
    dirs.value = d.dirs
  } catch (e) {
    toastError(e, '读取失败')
  } finally {
    loading.value = false
  }
}

/** 子目录选项：转存目录下现有的 + 已绑定但这次没列出来的（不在转存目录下了，也得显示得出名字） */
const dirOptions = computed(() => {
  const out = dirs.value.map((d) => ({ label: d.name, value: d.cid }))
  for (const s of subs.items.value) {
    if (!out.some((o) => o.value === s.cid)) out.push({ label: `${s.name}（不在转存目录下）`, value: s.cid })
  }
  return out
})
const libOptions = computed(() =>
  subs.libs.value.map((l) => ({ label: (l.name || l.cid) + (l.primary ? '（主库）' : ''), value: l.cid })),
)
const nameOf = (cid: string) => dirOptions.value.find((o) => o.value === cid)?.label ?? ''
const multiLib = computed(() => subs.libs.value.length > 1)

function addRow() {
  rows.value.push({ cid: '', lib: subs.libs.value[0]?.cid ?? '' })
}

async function createDir() {
  const name = newName.value.trim()
  if (!name) return
  creating.value = true
  try {
    const d = await transferApi.mkStagingSubdir(name)
    dirs.value.push(d)
    rows.value.push({ cid: d.cid, lib: subs.libs.value[0]?.cid ?? '' })
    newName.value = ''
    message.success(`已在转存目录下新建「${d.name}」，选好媒体库后保存`)
  } catch (e) {
    toastError(e, '新建失败')
  } finally {
    creating.value = false
  }
}

async function save() {
  saving.value = true
  try {
    const items = rows.value.filter((r) => r.cid).map((r) => ({ cid: r.cid, lib: r.lib, name: nameOf(r.cid) }))
    const d = await transferApi.saveStagingLibs(items)
    subs.setAll(d)
    rows.value = d.items.map((s) => ({ cid: s.cid, lib: s.lib, problem: s.problem }))
    message.success('已保存，之后整理这些子目录里的内容会落进绑定的媒体库')
  } catch (e) {
    toastError(e, '保存失败')
  } finally {
    saving.value = false
  }
}

onMounted(read)
</script>

<template>
  <SectionCard title="子目录 → 媒体库" hint="按来源分库：放进某个子目录的内容整理进指定的媒体库">
    <p class="lead">
      例如把 <code>4K</code> 子目录绑到「4K库」，找资源、链接转存、订阅时选「转存到 4K」，或在手机 / 网盘客户端上直接把文件放进
      <code>4K</code>，整理时就进 4K库。分类目录照分类规则算，<b>子目录绑定优先于分类规则里写的 library</b>，都没有就进主库。
      只支持转存目录下一层的子目录；子目录本身不会被整理或清理掉。
    </p>
    <p v-if="!loading && !multiLib" class="warn">
      现在只有一个媒体库。要按来源分到别的库，先到「账号与媒体库 → 媒体库」添加附加媒体库。
    </p>

    <div v-if="loading" class="empty">读取中…</div>
    <template v-else>
      <div v-for="(r, i) in rows" :key="i" class="row-wrap">
        <div class="row">
          <HSelect v-model="r.cid" :options="dirOptions" placeholder="选子目录" aria-label="子目录" class="dir" />
          <span class="arrow">→</span>
          <HSelect v-model="r.lib" :options="libOptions" placeholder="选媒体库" aria-label="媒体库" class="lib" />
          <HButton variant="ghost" size="sm" aria-label="删除" @click="rows.splice(i, 1)"><Trash2 :size="15" /></HButton>
        </div>
        <p v-if="r.problem" class="problem">{{ r.problem }}</p>
      </div>
      <p v-if="!rows.length" class="empty">还没有绑定，转存目录里的内容都按分类规则选库。</p>
      <div class="tools">
        <HButton variant="tertiary" size="sm" :disabled="!dirs.length" @click="addRow"><Plus :size="14" />绑定现有子目录</HButton>
        <div class="mk">
          <HInput v-model="newName" placeholder="新子目录名，如 4K" class="mk-name" :input-attrs="{ 'aria-label': '新子目录名' }" />
          <HButton variant="tertiary" size="sm" :loading="creating" :disabled="!newName.trim()" @click="createDir">新建并绑定</HButton>
        </div>
      </div>
    </template>

    <FormActions>
      <HButton variant="primary" :loading="saving" :disabled="loading" @click="save">保存绑定</HButton>
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
.warn {
  margin: 0 0 12px;
  font-size: 12.5px;
  color: var(--warning);
}
.row-wrap {
  margin-bottom: 8px;
}
.row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.dir,
.lib {
  flex: 1;
  min-width: 0;
}
.arrow {
  color: var(--muted);
  flex-shrink: 0;
}
.problem {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--danger);
}
.tools {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 16px;
  margin-top: 2px;
}
.mk {
  display: flex;
  align-items: center;
  gap: 8px;
}
.mk-name {
  width: 180px;
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
    flex-basis: calc(100% - 24px);
  }
  .mk {
    width: 100%;
  }
  .mk-name {
    flex: 1;
    width: auto;
  }
}
</style>
