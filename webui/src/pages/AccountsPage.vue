<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import HButton from '@/components/hero/HButton.vue'
import HInput from '@/components/hero/HInput.vue'
import HNumberInput from '@/components/hero/HNumberInput.vue'
import HSegmented from '@/components/hero/HSegmented.vue'
import HSelect from '@/components/hero/HSelect.vue'
import { Info, Plus, QrCode, ShieldCheck, X } from '@lucide/vue'
import SectionCard from '@/components/ui/SectionCard.vue'
import FieldRow from '@/components/ui/FieldRow.vue'
import FormActions from '@/components/ui/FormActions.vue'
import AccountHero from '@/components/accounts/AccountHero.vue'
import WorkspaceCard, { type WsSlot } from '@/components/accounts/WorkspaceCard.vue'
import QrLoginModal from '@/components/QrLoginModal.vue'
import Cid115Input, { type CidValue } from '@/components/Cid115Input.vue'
import LocalPathInput from '@/components/LocalPathInput.vue'
import { configApi, organizeApi, storageApi } from '@/api'
import { useFullSetting } from '@/pages/strm/fullSetting'
import { plainProps } from '@/utils/autofill'
import { DEVICE_OPTIONS } from '@/types/storage'
import type { StorageCheck } from '@/types/storage'
import { toastError, useFeedback } from '@/composables/useFeedback'

const { message, dialog } = useFeedback()
const media = useFullSetting()
const cidInput = ref<InstanceType<typeof Cid115Input> | null>(null)
const cidValue = ref({ cid: '', path: '' })

watch(
  () => [media.model.value.cid, media.model.value.cid_path] as const,
  async ([cid, savedPath]) => {
    if (!cid) {
      cidValue.value = { cid: '', path: '' }
      return
    }
    const displayPath = savedPath || cid
    if (cid !== cidValue.value.cid || displayPath !== cidValue.value.path) {
      cidValue.value = { cid, path: displayPath }
    }
    // 兼容旧配置：过去只保存 cid，进入页面时自动反查一次可读路径。
    if (!savedPath) {
      try {
        const resolved = await storageApi.path115(cid)
        if (media.model.value.cid === cid && resolved.path) {
          cidValue.value = { cid, path: resolved.path }
        }
      } catch {
        // Cookie 暂不可用时保留 cid；重新选择目录或下次保存仍可补齐。
      }
    }
  },
  { immediate: true },
)

/**
 * 附加媒体库（full.extra_libs）。主库以外再挂几个 115 目录，同步、海报墙、深度删除和主库一样管；
 * 整理目前只往主库放。编辑时只改这份副本，点「保存媒体库位置」才写回 media.model。
 */
const extraLibs = ref<CidValue[]>([])
const extraInputs = ref<(InstanceType<typeof Cid115Input> | null)[]>([])
watch(
  () => media.model.value.extra_libs,
  (v) => {
    extraLibs.value = (v ?? []).map((e) => ({ cid: e.cid, path: e.path || e.cid }))
  },
  { immediate: true },
)
function addExtraLib() {
  extraLibs.value.push({ cid: '', path: '' })
}
function removeExtraLib(i: number) {
  extraLibs.value.splice(i, 1)
  extraInputs.value.splice(i, 1)
}

/**
 * 工作目录（转存/待整理、已存在、冗余）的现状，连同媒体库一起在「工作目录」卡里列出来。
 * 路径只为显示而存（*_path / folder_path）；没存路径的老配置退回显示 cid。
 */
const wsRaw = ref<{ org: Record<string, string>; share: Record<string, string> } | null>(null)
const creatingWs = ref(false)

async function loadMissingWs() {
  try {
    const [org, share, legacy] = await Promise.all([
      configApi.getSetting<Record<string, string>>('org-basic', {}),
      configApi.getSetting<Record<string, string>>('share', {}),
      configApi.getSetting<{ cid?: string; path?: string }>('staging_legacy', {}),
    ])
    wsRaw.value = { org, share }
    wsLegacy.value = legacy.cid ? legacy.path || `cid ${legacy.cid}` : ''
  } catch {
    wsRaw.value = null
  }
}

const wsSlots = computed<WsSlot[]>(() => {
  const org = wsRaw.value?.org ?? {}
  const share = wsRaw.value?.share ?? {}
  const lib = media.saved.value
  const slot = (key: WsSlot['key'], cid?: string, path?: string): WsSlot => ({
    key,
    configured: !!cid && cid !== '0',
    path: path || (cid ? `cid ${cid}` : ''),
  })
  return [
    slot('library', lib.cid, lib.cid_path),
    ...(lib.extra_libs ?? []).filter((e) => e.cid && e.cid !== lib.cid).map((e) => ({ ...slot('library', e.cid, e.path), extra: true })),
    slot('share', share.folder, share.folder_path),
    slot('existing', org.existing, org.existing_path),
    slot('redundant', org.redundant, org.redundant_path),
  ]
})
/** 一键创建时的子目录名（与后端 wsSubDirs 一致），不是界面上的槽位名 */
const LABELS: Record<string, string> = { share: '转存', existing: '已存在', redundant: '冗余' }
/** 媒体库以外还缺的；媒体库没保存时后端会拒绝一键创建，按钮也不出 */
const missingWs = computed(() =>
  wsRaw.value ? wsSlots.value.filter((s) => s.key !== 'library' && !s.configured).map((s) => LABELS[s.key]) : [],
)

/** 待整理并进转存时被弃用的旧目录（后端 stagingdir.go）；点「知道了」清掉 */
const wsLegacy = ref('')
async function dismissLegacy() {
  try {
    await configApi.saveSetting('staging_legacy', {})
    wsLegacy.value = ''
  } catch (e) {
    toastError(e, '操作失败')
  }
}

async function createWs() {
  const ok = await dialog.confirm({
    title: '一键创建工作目录',
    content: `将在 115 网盘根目录下创建 /StrmStation/{${missingWs.value.join('、')}} 并写入配置。已配置的目录保持不动，同名目录已存在则直接复用。`,
    actions: [
      { label: '取消', value: false, variant: 'tertiary' },
      { label: '创建', value: true, variant: 'primary' },
    ],
  })
  if (!ok) return
  creatingWs.value = true
  try {
    const res = await organizeApi.initWorkspace()
    message.success(res.message)
    await loadMissingWs()
  } catch (e) {
    toastError(e, '创建失败')
  } finally {
    creatingWs.value = false
  }
}

const DEFAULT_COOKIE_PATH = '/config/115-cookies.txt'

const form = ref({
  cookie_path: DEFAULT_COOKIE_PATH,
  device: 'web',
  interval: 3.0,
  openapi_enabled: false,
  app_id: '',
})

const account = ref<StorageCheck | null>(null)
/** 首次静默检测还没回来：账号卡显示骨架，而不是先闪一下「未绑定」 */
const accountLoading = ref(true)
const checking = ref(false)
const saving = ref(false)
const qrShow = ref(false)

/** OPENAPI 启用且填了 AppID 才走开放平台授权，否则回退 Cookie 扫码 */
const qrChannel = computed<'openapi' | 'cookie'>(() =>
  form.value.openapi_enabled && form.value.app_id.trim() ? 'openapi' : 'cookie',
)

async function load() {
  try {
    const res = await storageApi.list()
    const acc = (res.data ?? []).find((s) => s.type === '115')
    if (acc) {
      form.value = {
        cookie_path: acc.cookie_path || DEFAULT_COOKIE_PATH,
        // 保存过的设备值不在选项里时保持默认网页端
        device: DEVICE_OPTIONS.some((o) => o.value === acc.device) ? acc.device : 'web',
        interval: acc.interval || 3.0,
        openapi_enabled: !!acc.openapi_enabled,
        app_id: acc.app_id || '',
      }
    }
    // 静默刷新账号卡片（真实容量/会员/头像）；未绑定时失败是正常的，不提示
    const chk = await storageApi.check().catch(() => null)
    if (chk?.valid) account.value = chk
  } catch {
    // 列表拿不到就保持默认表单，不打断页面
  } finally {
    accountLoading.value = false
  }
}

async function check() {
  checking.value = true
  try {
    const data = await storageApi.check()
    if (!data.valid) {
      message.error('Cookie 无效：' + (data.message || '未知原因'))
      return
    }
    account.value = data
    message.success('Cookie 检测成功')
  } catch (e) {
    toastError(e, 'Cookie 检测失败')
  } finally {
    checking.value = false
  }
}

/** 启用 OpenAPI 但没填 AppID 时后端会当作未启用，静默回落 Cookie——先在这里拦住 */
function validate(): boolean {
  if (form.value.openapi_enabled && !form.value.app_id.trim()) {
    message.error('启用 OPENAPI 必须填写开放平台 AppID；没有 AppID 请选择「禁用」，Cookie 通道功能完整')
    return false
  }
  return true
}

async function save(): Promise<boolean> {
  if (!validate()) return false
  saving.value = true
  try {
    await storageApi.save({ name: '115主号', type: '115', ...form.value })
    message.success('保存成功')
    return true
  } catch (e) {
    toastError(e, '保存失败')
    return false
  } finally {
    saving.value = false
  }
}

async function saveAndScan() {
  if (await save()) qrShow.value = true
}

async function saveMedia() {
  const cid = (await cidInput.value?.ensureCid()) ?? ''
  if (!cid || cid === '0') {
    message.error('目录路径无法识别：请点「选择目录」重新选择，或输入纯数字 cid')
    return
  }
  if (!media.model.value.local_path.trim()) {
    message.error('请填写本地媒体库根目录')
    return
  }
  media.model.value.cid = cid
  let readablePath = cidValue.value.path.trim()
  if (!readablePath || /^\d+$/.test(readablePath)) {
    try {
      readablePath = (await storageApi.path115(cid)).path
    } catch {
      readablePath = ''
    }
  }
  media.model.value.cid_path = readablePath

  // 附加媒体库：空行丢掉；填了却解析不出 cid 的拦下来，别静默丢掉用户要加的库
  const extras: { cid: string; path: string }[] = []
  for (let i = 0; i < extraLibs.value.length; i++) {
    const row = extraLibs.value[i]
    if (!row.path.trim() && !row.cid) continue
    const ecid = (await extraInputs.value[i]?.ensureCid()) ?? row.cid
    if (!ecid || ecid === '0') {
      message.error(`第 ${i + 1} 个附加媒体库的路径无法识别：请点「选择目录」重新选择，或输入纯数字 cid`)
      return
    }
    if (ecid === cid || extras.some((e) => e.cid === ecid)) {
      message.error(`第 ${i + 1} 个附加媒体库和前面的媒体库是同一个目录`)
      return
    }
    let p = row.path.trim()
    if (!p || /^\d+$/.test(p)) {
      try {
        p = (await storageApi.path115(ecid)).path
      } catch {
        p = ''
      }
    }
    extras.push({ cid: ecid, path: p })
  }
  media.model.value.extra_libs = extras
  await media.save()
}

function reset() {
  form.value = {
    cookie_path: DEFAULT_COOKIE_PATH,
    device: 'web',
    interval: 3.0,
    openapi_enabled: false,
    app_id: '',
  }
  message.info('配置已重置（尚未保存）')
}

onMounted(() => {
  load()
  loadMissingWs()
})
</script>

<template>
  <div class="page">
    <AccountHero
      :account="account"
      :loading="accountLoading"
      :checking="checking"
      @check="check"
      @scan="qrShow = true"
    />

    <div class="cols">
      <!-- ==== 左：连接设置 + 媒体库位置（都是表单） ==== -->
      <div class="col">
        <SectionCard title="连接设置" hint="Cookie 文件 · 登录设备 · 开放平台 · 风控间隔" class="col-conn">
          <FieldRow
            label="Cookie 路径"
            tip="指定 115 Cookie 文件路径，扫码登录后自动写入该文件。"
          >
            <div class="h-field-row">
              <HInput v-model="form.cookie_path" placeholder="/config/115-cookies.txt" :input-attrs="plainProps('acc-cookie-path')" />
              <HButton variant="secondary" :loading="checking" @click="check">
                <template #icon><ShieldCheck :size="15" /></template>
                检测
              </HButton>
            </div>
          </FieldRow>

          <FieldRow
            label="Cookie 设备"
            tip="网页端 Cookie 与 115Browser UA 配套，兼容性最好（默认推荐）。App 端 Cookie 有设备槽位校验，与桌面 UA 不匹配会报“服务器开小差”。避免选常用设备导致被挤下线。"
          >
            <div class="h-field-row">
              <HSelect v-model="form.device" :options="DEVICE_OPTIONS" />
              <HButton variant="secondary" @click="qrShow = true">
                <template #icon><QrCode :size="15" /></template>
                扫码登录
              </HButton>
            </div>
          </FieldRow>

          <FieldRow
            label="启用 OPENAPI"
            tip="需先在 115 开放平台（open.115.com）申请应用拿到 AppID。没有 AppID 请保持「禁用」——Cookie 通道功能完整，且全量同步可用「快速模式」。"
          >
            <HSegmented v-model="form.openapi_enabled" :options="[{ label: '启用', value: true }, { label: '禁用（默认）', value: false }]" />
          </FieldRow>

          <FieldRow
            v-if="form.openapi_enabled"
            label="开放平台 AppID"
            required
            tip="在 115 开放平台（open.115.com）申请应用后获取。填入后点击“保存并扫码授权”，用 115 App 扫码完成 OAuth 授权。"
          >
            <div class="h-field-row">
              <HInput v-model="form.app_id" placeholder="请输入 115 开放平台 AppID" :input-attrs="plainProps('acc-open-app-id')" />
              <HButton variant="secondary" @click="saveAndScan">保存并扫码授权</HButton>
            </div>
          </FieldRow>

          <FieldRow
            label="API 请求间隔"
            tip="设置 API 请求间隔可以减少风控概率。读接口默认 1s，写接口建议 ≥3s。"
          >
            <HNumberInput v-model="form.interval" :min="0.5" :step="0.5" style="width: 150px">
              <template #suffix>秒</template>
            </HNumberInput>
          </FieldRow>

          <FormActions>
            <HButton variant="primary" :loading="saving" @click="save">保存配置</HButton>
            <HButton variant="tertiary" @click="reset">重置配置</HButton>
          </FormActions>
        </SectionCard>

        <SectionCard title="媒体库位置" hint="115 源目录 → 本地 STRM 根目录">
          <p class="media-note">
            <Info :size="14" />全量同步、增量同步、自动整理、影视刮削和 Emby 路径映射共用这一处位置配置。
          </p>

          <FieldRow
            label="115 媒体库目录"
            required
            tip="主媒体库。全量同步、增量同步、整理入库与洗版判定共同锚定此目录；整理目前只往这里放。"
          >
            <Cid115Input ref="cidInput" v-model="cidValue" />
          </FieldRow>

          <FieldRow
            label="附加媒体库"
            tip="主媒体库以外的 115 目录，同步、海报墙、深度删除都和主库一样管，本地 STRM 放在「本地根目录/库目录名」下。各库之间不能互相包含，目录名也不能重名。整理入库目前只往主媒体库放。Emby 路径映射的本地一侧请留空（跟随本地根目录），只填某个库目录的话，其他库在 Emby 里对不上。"
          >
            <div class="extra-libs">
              <div v-for="(_row, i) in extraLibs" :key="i" class="extra-lib">
                <Cid115Input
                  :ref="(el) => (extraInputs[i] = el as InstanceType<typeof Cid115Input> | null)"
                  v-model="extraLibs[i]"
                  placeholder="选择另一个 115 媒体库目录"
                  class="extra-lib-input"
                />
                <HButton variant="ghost" icon-only :aria-label="`移除第 ${i + 1} 个附加媒体库`" @click="removeExtraLib(i)">
                  <template #icon><X :size="15" /></template>
                </HButton>
              </div>
              <HButton variant="secondary" size="sm" @click="addExtraLib">
                <template #icon><Plus :size="15" /></template>
                添加媒体库
              </HButton>
            </div>
          </FieldRow>

          <FieldRow
            label="本地媒体库根目录"
            required
            tip="STRM、字幕、NFO 与图片的统一本地保存根目录，也是影视刮削使用的根目录。"
          >
            <LocalPathInput v-model="media.model.value.local_path" />
          </FieldRow>

          <FormActions>
            <HButton variant="primary" :loading="media.saving.value" @click="saveMedia">保存媒体库位置</HButton>
          </FormActions>
        </SectionCard>
      </div>

      <!-- ==== 右：工作目录一览（宽屏吸顶，左边表单滚动时一直看得见） ==== -->
      <div class="col col-side">
        <WorkspaceCard
          :slots="wsSlots"
          :can-create="!!media.saved.value.cid && missingWs.length > 0"
          :creating="creatingWs"
          :legacy="wsLegacy"
          @create="createWs"
          @dismiss-legacy="dismissLegacy"
        />
      </div>
    </div>

    <QrLoginModal
      v-model:show="qrShow"
      :channel="qrChannel"
      :device="form.device"
      :app-id="form.app_id"
      @success="(message.success('115 账号绑定成功'), check())"
    />
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* ---- 两栏：左连接设置，右媒体库与工作目录 ---- */
.cols {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}
.col {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}
.col-side {
  position: sticky;
  top: calc(68px + 16px);
}
.media-note {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin: 0 0 14px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--muted);
}
.extra-libs {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
}
.extra-lib {
  display: flex;
  align-items: center;
  gap: 4px;
  width: 100%;
}
.extra-lib-input {
  flex: 1;
  min-width: 0;
}
.media-note :deep(svg) {
  flex-shrink: 0;
  margin-top: 3px;
}

@media (max-width: 1280px) {
  .cols {
    grid-template-columns: 1fr;
  }
  .col-side {
    position: static;
  }
}
</style>
