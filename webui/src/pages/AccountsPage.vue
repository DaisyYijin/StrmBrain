<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import HAlert from '@/components/hero/HAlert.vue'
import HButton from '@/components/hero/HButton.vue'
import HInput from '@/components/hero/HInput.vue'
import HNumberInput from '@/components/hero/HNumberInput.vue'
import HSegmented from '@/components/hero/HSegmented.vue'
import HSelect from '@/components/hero/HSelect.vue'
import HTabs from '@/components/hero/HTabs.vue'
import { ChevronRight, Info, QrCode, ShieldCheck } from '@lucide/vue'
import SectionCard from '@/components/ui/SectionCard.vue'
import FieldRow from '@/components/ui/FieldRow.vue'
import FormActions from '@/components/ui/FormActions.vue'
import AccountHero from '@/components/accounts/AccountHero.vue'
import WorkspaceCard, { type WsSlot } from '@/components/accounts/WorkspaceCard.vue'
import LibraryCards from '@/components/accounts/LibraryCards.vue'
import QrLoginModal from '@/components/QrLoginModal.vue'
import Cid115Input from '@/components/Cid115Input.vue'
import LocalPathInput from '@/components/LocalPathInput.vue'
import { configApi, libraryApi, organizeApi, storageApi } from '@/api'
import type { LibLeftover } from '@/api/library'
import { useQueueStore } from '@/stores/queue'
import { useFullSetting } from '@/pages/strm/fullSetting'
import { plainProps } from '@/utils/autofill'
import { DEVICE_OPTIONS } from '@/types/storage'
import type { StorageCheck } from '@/types/storage'
import { toastError, useFeedback } from '@/composables/useFeedback'
import { useTabQuery } from '@/composables/useTabQuery'

const { message, dialog } = useFeedback()
const media = useFullSetting()

/**
 * 两个页签：account（账号与连接设置）/ library（媒体库位置与工作目录）。
 * 别处跳「改媒体库目录」带 ?tab=library；页签值别改，收藏和站内链接都认它。
 */
const tab = useTabQuery('account')
const TAB_ITEMS = computed(() => [
  { value: 'account', label: '115 账号' },
  { value: 'library', label: '媒体库', dot: libUnset.value },
])
/** 媒体库还没保存过：读完配置之后才下结论，免得进页面先闪一下引导 */
const libUnset = computed(() => !media.loading.value && (!media.saved.value.cid || media.saved.value.cid === '0'))
/** 账号还没绑定：媒体库页签里选不了 115 目录，提示先回账号页签扫码 */
const accountUnbound = computed(() => !accountLoading.value && !account.value)
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
 * 其他媒体库（full.extra_libs）由下面的库卡片管（LibraryCards，PUT /libraries）。
 * 它存完之后这边手里的 full 要跟上：「保存媒体库位置」是整存 full 的，拿着旧的 extra_libs 一存就把刚加的库冲掉了
 */
const libCards = ref<InstanceType<typeof LibraryCards> | null>(null)
async function onLibsSaved(extras: { cid: string; path: string }[]) {
  if (media.dirty.value) media.model.value.extra_libs = extras
  else await media.load()
  void loadLeftovers()
}
/**
 * 移出配置的媒体库在本地留下的 STRM 与台账（后端 libleft.go）。删掉一个附加库、或 115 上给库改了名之后出现；
 * 只列出来，用户点了才清，清理也只动本地
 */
const queue = useQueueStore()
const leftovers = ref<LibLeftover[]>([])
async function loadLeftovers() {
  try {
    leftovers.value = (await libraryApi.getLeftovers()).data ?? []
  } catch {
    leftovers.value = []
  }
}
const offFinished = queue.onFinished((j) => {
  if (j.kind === 'libclean') void loadLeftovers()
})
onUnmounted(offFinished)

async function cleanLeftover(l: LibLeftover) {
  const ok = await dialog.confirm({
    title: `清理「${l.name}」的本地残留`,
    content: `将删除本地媒体库根目录下的「${l.name}」整个目录（STRM、字幕、NFO、图片），以及台账里它的 ${l.files} 行。网盘上的文件不动；以后想要回来，把这个库加回媒体库再跑一次全量同步即可。Emby 里对应的媒体库请自己在 Emby 里删除或刷新。`,
    actions: [
      { label: '取消', value: false, variant: 'tertiary' },
      { label: '清理本地', value: true, variant: 'danger' },
    ],
  })
  if (!ok) return
  try {
    const d = await libraryApi.cleanLeftover(l.name)
    message.success(d.message)
    await queue.submitted(d.job_id)
  } catch (e) {
    toastError(e, '提交清理失败')
  }
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
  // extra_libs 原样带回去（库卡片存完会同步到 media.model，见 onLibsSaved）
  if (await media.save()) {
    void loadLeftovers()
    void libCards.value?.reload() // 默认库换了目录：卡片上的名字、本地目录跟着变
  }
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
  void loadLeftovers()
})
</script>

<template>
  <div class="page">
    <HTabs v-model="tab" :items="TAB_ITEMS" />

    <Transition name="tab" mode="out-in">
      <div v-if="tab !== 'library'" key="account" class="pane">
        <AccountHero
          :account="account"
          :loading="accountLoading"
          :checking="checking"
          @check="check"
          @scan="qrShow = true"
        />

        <!-- 没设媒体库时同步、整理、刮削都跑不起来，在账号页签就引导过去 -->
        <HAlert v-if="libUnset" status="warning" title="还没有设置媒体库">
          <template v-if="accountUnbound">先扫码登录 115 账号，再到「媒体库」页签选择 115 媒体库目录和本地 STRM 根目录。</template>
          <template v-else>同步、整理、刮削都要先知道 115 媒体库目录和本地 STRM 根目录，请到「媒体库」页签设置。</template>
          <template #actions>
            <HButton v-if="accountUnbound" variant="primary" size="sm" @click="qrShow = true">
              <template #icon><QrCode :size="15" /></template>
              扫码登录
            </HButton>
            <HButton :variant="accountUnbound ? 'secondary' : 'primary'" size="sm" @click="tab = 'library'">
              去设置媒体库<ChevronRight :size="15" />
            </HButton>
          </template>
        </HAlert>

        <SectionCard title="连接设置" hint="Cookie 文件 · 登录设备 · 开放平台 · 风控间隔">
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
      </div>

      <div v-else key="library" class="pane">
        <HAlert v-if="accountUnbound" status="accent" title="115 账号还没绑定">
          选择 115 目录要先登录账号。
          <template #actions>
            <HButton variant="secondary" size="sm" @click="tab = 'account'">去绑定账号<ChevronRight :size="15" /></HButton>
          </template>
        </HAlert>

        <div class="cols">
          <!-- ==== 左：媒体库位置（表单） ==== -->
          <div class="col">
            <SectionCard title="媒体库位置" hint="115 源目录 → 本地 STRM 根目录">
              <p class="media-note">
                <Info :size="14" />全量同步、增量同步、自动整理、影视刮削和 Emby 路径映射共用这一处位置配置。
              </p>

              <FieldRow
                label="首个媒体库"
                required
                tip="115 上的一个目录，在下面的媒体库卡片里排第一、不能移除；二级分类里没指定库的规则默认整理到这里。要再加别的 115 目录，用下面的「添加媒体库」。"
              >
                <Cid115Input ref="cidInput" v-model="cidValue" />
              </FieldRow>

              <HAlert v-if="leftovers.length" status="warning" title="本地还有不在配置里的媒体库" class="leftovers">
                这些目录是移出配置的媒体库（或 115 上改过名的库）留下的，同步不再管它们，海报墙和 Emby 里还看得到。
                <ul class="leftover-list">
                  <li v-for="l in leftovers" :key="l.name">
                    <span>
                      <b>{{ l.name }}</b>：台账 {{ l.files }} 行（视频 {{ l.videos }} 个）<template v-if="!l.local">，本地目录已不在</template>
                    </span>
                    <HButton variant="tertiary" size="sm" @click="cleanLeftover(l)">清理本地</HButton>
                  </li>
                </ul>
              </HAlert>

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

            <LibraryCards v-if="!libUnset" ref="libCards" @saved="onLibsSaved" />
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
      </div>
    </Transition>

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

.pane {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}
.tab-enter-active,
.tab-leave-active {
  transition:
    opacity 0.16s ease,
    transform 0.16s ease;
}
.tab-enter-from {
  opacity: 0;
  transform: translateY(5px);
}
.tab-leave-to {
  opacity: 0;
  transform: translateY(-3px);
}

/* ---- 媒体库页签两栏：左媒体库位置，右工作目录 ---- */
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
.leftovers {
  margin: 4px 0 8px;
}
.leftover-list {
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
}
.leftover-list li {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 2px 0;
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
