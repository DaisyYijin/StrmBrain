<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Download, RotateCcw, Trash2, Upload } from '@lucide/vue'
import HAlert from '@/components/hero/HAlert.vue'
import HButton from '@/components/hero/HButton.vue'
import HChip from '@/components/hero/HChip.vue'
import HInput from '@/components/hero/HInput.vue'
import HNumberInput from '@/components/hero/HNumberInput.vue'
import SectionCard from '@/components/ui/SectionCard.vue'
import FieldRow from '@/components/ui/FieldRow.vue'
import FormActions from '@/components/ui/FormActions.vue'
import SchedulePicker from '@/components/ui/SchedulePicker.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import { systemApi } from '@/api'
import type { BackupItem, BackupList } from '@/api/system'
import { useSetting } from '@/composables/useSetting'
import { toastError, useFeedback } from '@/composables/useFeedback'
import { fullTime, relTime } from '@/utils/time'

/**
 * 备份与恢复（后端 backup.go）。
 * 恢复不在运行中替换文件：先校验、暂存，并自动备份一份当前状态，重启后才换进去。
 */
const { message, dialog } = useFeedback()
const { model, saving, save } = useSetting('backup', { enabled: false, cron: '0 4 * * *', keep: 7, dir: '' })

const data = ref<BackupList | null>(null)
const loading = ref(false)
const creating = ref(false)
const busyName = ref('')
const uploading = ref(false)
const restarting = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

const items = computed(() => data.value?.items ?? [])
const KIND_LABEL: Record<BackupItem['kind'], string> = { manual: '手动', auto: '定时', prerestore: '恢复前' }

function size(n: number) {
  if (n >= 1 << 30) return (n / (1 << 30)).toFixed(1) + ' GB'
  if (n >= 1 << 20) return (n / (1 << 20)).toFixed(1) + ' MB'
  if (n >= 1 << 10) return (n / (1 << 10)).toFixed(0) + ' KB'
  return n + ' B'
}

async function load() {
  loading.value = true
  try {
    data.value = await systemApi.backups()
  } catch (e) {
    toastError(e, '读取备份列表失败')
  } finally {
    loading.value = false
  }
}
onMounted(load)

async function saveCfg() {
  await save()
  await load()
}

async function createNow() {
  creating.value = true
  try {
    const it = await systemApi.createBackup()
    message.success(`已备份 ${it.name}`)
    await load()
  } catch (e) {
    toastError(e, '备份失败')
  } finally {
    creating.value = false
  }
}

async function download(name?: string) {
  busyName.value = name || '__live'
  try {
    await systemApi.downloadBackup(name)
  } catch (e) {
    toastError(e, '下载失败')
  } finally {
    busyName.value = ''
  }
}

async function remove(it: BackupItem) {
  const ok = await dialog.confirm({
    title: '删除备份',
    content: `删除 ${it.name}？删了就找不回来了。`,
    actions: [
      { label: '取消', value: false, variant: 'tertiary' },
      { label: '删除', value: true, variant: 'danger' },
    ],
  })
  if (!ok) return
  try {
    await systemApi.deleteBackup(it.name)
    await load()
  } catch (e) {
    toastError(e, '删除失败')
  }
}

const RESTORE_TEXT =
  '会用这份备份替换全部配置（setting.yaml、账号、115 登录凭据等）与数据库（同步台账、整理记录、订阅……）。' +
  '现在的状态会先自动备份一份（列表里标「恢复前」），恢复错了可以再恢复回来。' +
  '确认后只是暂存，要重启一次才生效；从备份到重启之间做的改动都会丢掉。本地 STRM 与网盘文件不受影响。'

async function confirmRestore(src: string) {
  return dialog.confirm({
    title: `从「${src}」恢复`,
    content: RESTORE_TEXT,
    actions: [
      { label: '取消', value: false, variant: 'tertiary' },
      { label: '暂存恢复', value: true, variant: 'danger' },
    ],
  })
}

async function restoreFrom(it: BackupItem) {
  if (!(await confirmRestore(it.name))) return
  busyName.value = it.name
  try {
    await systemApi.restoreBackup(it.name)
    message.success('已暂存，重启后生效')
    await load()
  } catch (e) {
    toastError(e, '恢复失败')
  } finally {
    busyName.value = ''
  }
}

function pickFile() {
  fileInput.value?.click()
}

async function onFile(ev: Event) {
  const input = ev.target as HTMLInputElement
  const f = input.files?.[0]
  input.value = ''
  if (!f) return
  if (!(await confirmRestore(f.name))) return
  uploading.value = true
  try {
    await systemApi.uploadRestore(f)
    message.success('已暂存，重启后生效')
    await load()
  } catch (e) {
    toastError(e, '上传恢复失败')
  } finally {
    uploading.value = false
  }
}

async function cancelPending() {
  try {
    await systemApi.cancelRestore()
    message.success('已取消')
    await load()
  } catch (e) {
    toastError(e, '取消失败')
  }
}

async function restartNow() {
  const ok = await dialog.confirm({
    title: '重启 115-Station',
    content:
      '会先停下后台任务、冲刷待发通知，再退出进程，靠容器的重启策略拉起来（docker-compose.yml 默认 restart: unless-stopped）。' +
      '没配重启策略的话进程退出后不会自己起来，请手动 docker compose up -d。恢复后登录可能失效，需要重新登录。',
    actions: [
      { label: '取消', value: false, variant: 'tertiary' },
      { label: '重启', value: true, variant: 'danger' },
    ],
  })
  if (!ok) return
  restarting.value = true
  try {
    await systemApi.restart()
  } catch (e) {
    restarting.value = false
    toastError(e, '重启失败')
    return
  }
  // 进程收尾要 3 秒、容器再拉起来还要几秒：先等它下线，回来了就刷新页面。
  // 一直没观察到下线（重启太快被轮询错过）的，20 秒后照样刷新
  const start = Date.now()
  let wentDown = false
  while (Date.now() - start < 180_000) {
    await new Promise((r) => setTimeout(r, 2000))
    try {
      await systemApi.version()
      if (wentDown || Date.now() - start > 20_000) {
        location.reload()
        return
      }
    } catch {
      wentDown = true
    }
  }
  restarting.value = false
  message.warning('等了 3 分钟还没起来，请检查容器状态')
}
</script>

<template>
  <HAlert v-if="data?.pending" status="warning" title="有一份备份等待恢复">
    来自「{{ data.pending.source }}」<template v-if="data.pending.manifest.version">（{{ data.pending.manifest.version }} 导出<template v-if="data.pending.manifest.created_at">于 {{ fullTime(data.pending.manifest.created_at) }}</template>）</template>，
    暂存于 {{ relTime(data.pending.staged_at) }}。重启后生效<template v-if="data.pending.safety_backup">，恢复前的状态已另存为 {{ data.pending.safety_backup }}</template>。
    <template v-if="!data.pending.manifest.has_db">这份备份里没有数据库，只恢复配置。</template>
    <div class="alert-actions">
      <HButton v-if="data.can_restart" variant="primary" size="sm" :loading="restarting" @click="restartNow">立即重启</HButton>
      <HButton variant="tertiary" size="sm" :disabled="restarting" @click="cancelPending">取消恢复</HButton>
    </div>
  </HAlert>
  <HAlert
    v-else-if="data?.last_restore"
    :status="data.last_restore.ok ? 'success' : 'danger'"
    :title="data.last_restore.ok ? '上次启动时已从备份恢复' : '上次启动时恢复失败'"
  >
    「{{ data.last_restore.source }}」· {{ fullTime(data.last_restore.at) }}
    <template v-if="data.last_restore.error">：{{ data.last_restore.error }}（配置与数据库保持恢复前的样子）</template>
  </HAlert>

  <SectionCard title="备份" hint="打包配置目录与数据库快照；图片缓存、日志、本地 STRM 不在里面">
    <template #extra>
      <HButton variant="tertiary" :loading="busyName === '__live'" @click="download()">
        <Download :size="15" /> 直接下载一份
      </HButton>
      <HButton variant="primary" :loading="creating" @click="createNow">立即备份</HButton>
    </template>

    <FieldRow label="存放位置">
      <span class="mono">{{ data?.dir || '—' }}</span>
    </FieldRow>
    <FieldRow v-if="model.enabled" label="定时备份">
      <span v-if="data?.next_auto">下一次 {{ fullTime(data.next_auto) }}</span>
      <span v-if="data?.last_auto" :class="{ err: !data.last_auto.ok }">
        · 上一次 {{ relTime(data.last_auto.at) }}{{ data.last_auto.ok ? '成功' : '失败：' + data.last_auto.error }}
      </span>
    </FieldRow>

    <EmptyState v-if="!loading && !items.length" text="还没有备份" />
    <ul v-else class="list">
      <li v-for="it in items" :key="it.name" class="row">
        <div class="meta">
          <div class="name mono">{{ it.name }}</div>
          <div class="sub">
            <HChip :color="it.kind === 'auto' ? 'accent' : it.kind === 'prerestore' ? 'warning' : 'default'">{{ KIND_LABEL[it.kind] }}</HChip>
            <span :title="fullTime(it.created_at)">{{ relTime(it.created_at) }}</span>
            <span>{{ size(it.size) }}</span>
          </div>
        </div>
        <div class="ops">
          <HButton variant="tertiary" size="sm" icon-only aria-label="下载" :loading="busyName === it.name" @click="download(it.name)">
            <Download :size="15" />
          </HButton>
          <HButton variant="tertiary" size="sm" :disabled="!!busyName" @click="restoreFrom(it)">
            <RotateCcw :size="15" /> 恢复
          </HButton>
          <HButton variant="ghost" size="sm" icon-only aria-label="删除" @click="remove(it)">
            <Trash2 :size="15" />
          </HButton>
        </div>
      </li>
    </ul>
  </SectionCard>

  <SectionCard title="从文件恢复" hint="上传之前下载的备份包（.zip），换机迁移也用这个">
    <input ref="fileInput" type="file" accept=".zip,application/zip" hidden @change="onFile" />
    <div>
      <HButton variant="secondary" :loading="uploading" @click="pickFile"><Upload :size="15" /> 选择备份文件</HButton>
    </div>
  </SectionCard>

  <SectionCard title="定时备份">
    <FieldRow label="计划" tip="按计划在后台打一份备份，只读本地配置与数据库，不请求 115。">
      <SchedulePicker v-model="model.cron" v-model:enabled="model.enabled" toggle placeholder="0 4 * * *" />
    </FieldRow>
    <FieldRow label="保留份数" tip="只算定时备份，超出的从最旧的删起；手动备份与恢复前备份不会被它清掉（恢复前的另外只留最近 3 份）。">
      <HNumberInput v-model="model.keep" :min="1" :max="365" />
    </FieldRow>
    <FieldRow label="存放目录" tip="留空 = 数据目录下的 backups。想放到另一块盘，填容器里的绝对路径（记得在 compose 里挂载）。">
      <HInput v-model="model.dir" mono placeholder="留空 = /data/backups" />
    </FieldRow>
    <FormActions>
      <HButton variant="primary" :loading="saving" @click="saveCfg">保存配置</HButton>
    </FormActions>
  </SectionCard>
</template>

<style scoped>
.mono {
  font-family: var(--font-mono, ui-monospace, monospace);
  word-break: break-all;
}
.err {
  color: var(--danger);
}
.alert-actions {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}
.row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 0;
  border-top: 1px solid var(--separator);
}
.row:first-child {
  border-top: none;
}
.meta {
  flex: 1;
  min-width: 0;
}
.name {
  font-size: 13px;
}
.sub {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
  font-size: 12px;
  color: var(--muted);
}
.ops {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}
</style>
