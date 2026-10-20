<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Pencil, Plus, QrCode, ShieldCheck, Trash2, Upload } from '@lucide/vue'
import SectionCard from '@/components/ui/SectionCard.vue'
import SecretInput from '@/components/ui/SecretInput.vue'
import HButton from '@/components/hero/HButton.vue'
import HChip from '@/components/hero/HChip.vue'
import HInput from '@/components/hero/HInput.vue'
import HModal from '@/components/hero/HModal.vue'
import QrLoginModal from '@/components/QrLoginModal.vue'
import { storageApi } from '@/api'
import type { Account115 } from '@/api/storage'
import { toastError, useFeedback } from '@/composables/useFeedback'

/**
 * 附加 115 账号（后端 account115.go，MULTI-ACCOUNT-PLAN.md 阶段 2）。
 *
 * 只管登录、检测、改名、删除。附加账号的用途是把它的目录挂成「我自己整理」的库（阶段 3），
 * 整理、转存、订阅、机器人都只用主账号 —— 文案里要一直说清楚，免得用户以为能往附加账号里转存。
 * 后端没打开 STATION_MULTI_ACCOUNT 时整块不显示（阶段 3 之前登录了也挂不了库）。
 */

const { message, dialog } = useFeedback()

const enabled = ref(false)
const list = ref<Account115[]>([])
const extras = computed(() => list.value.filter((a) => !a.main))
const busy = ref(0)

async function load() {
  try {
    const r = await storageApi.accounts()
    enabled.value = r.enabled
    list.value = r.data ?? []
  } catch {
    enabled.value = false // 老后端没有这个接口：不显示
  }
}
onMounted(load)

function statusOf(a: Account115): { text: string; color: 'success' | 'warning' | 'danger' | 'default' } {
  if (!a.logged_in) return { text: '未登录', color: 'default' }
  if (a.status === 'invalid') return { text: 'Cookie 失效', color: 'danger' }
  if (a.status === 'online') return { text: '已登录', color: 'success' }
  return { text: '未检测', color: 'warning' }
}

// ---- 添加 / 改名 ----

const nameDlg = ref<{ show: boolean; id: number; name: string; saving: boolean }>({ show: false, id: 0, name: '', saving: false })
function openAdd() {
  nameDlg.value = { show: true, id: 0, name: '', saving: false }
}
function openRename(a: Account115) {
  nameDlg.value = { show: true, id: a.id, name: a.name, saving: false }
}
async function saveName() {
  const d = nameDlg.value
  d.saving = true
  try {
    if (d.id) {
      await storageApi.renameAccount(d.id, d.name.trim())
      message.success('已改名')
      d.show = false
      await load()
      return
    }
    const r = await storageApi.createAccount(d.name.trim())
    d.show = false
    await load()
    scan(r.data.id) // 建好就去扫码，少点一次
  } catch (e) {
    toastError(e, d.id ? '改名失败' : '添加失败')
  } finally {
    d.saving = false
  }
}

// ---- 登录 ----

const qr = ref<{ show: boolean; id: number }>({ show: false, id: 0 })
function scan(id: number) {
  qr.value = { show: true, id }
}
async function onScanned() {
  message.success('附加账号已登录')
  await load()
}

const cookieDlg = ref<{ show: boolean; id: number; cookie: string; saving: boolean }>({ show: false, id: 0, cookie: '', saving: false })
function openImport(a: Account115) {
  cookieDlg.value = { show: true, id: a.id, cookie: '', saving: false }
}
async function saveCookie() {
  const d = cookieDlg.value
  d.saving = true
  try {
    const r = await storageApi.checkAccount(d.id, d.cookie.trim())
    if (!r.valid) {
      message.error('Cookie 无效：' + (r.message || '未知原因'))
      return
    }
    if (r.warning) message.warning(r.warning)
    else message.success(`已导入（${r.username}）`)
    d.show = false
    await load()
  } catch (e) {
    toastError(e, '导入失败')
  } finally {
    d.saving = false
  }
}

async function check(a: Account115) {
  busy.value = a.id
  try {
    const r = await storageApi.checkAccount(a.id)
    if (r.valid) message.success(`「${a.name}」有效：${r.username}，${r.capacity}`)
    else message.error(`「${a.name}」${r.message || 'Cookie 无效'}`)
    await load()
  } catch (e) {
    toastError(e, '检测失败')
  } finally {
    busy.value = 0
  }
}

async function remove(a: Account115) {
  const ok = await dialog.confirm({
    title: `删除附加账号「${a.name}」`,
    content: '只删本站记下的登录信息，115 网盘上的东西不动。',
    actions: [
      { label: '取消', value: false, variant: 'tertiary' },
      { label: '删除', value: true, variant: 'danger' },
    ],
  })
  if (!ok) return
  try {
    await storageApi.deleteAccount(a.id)
    message.success('已删除')
    await load()
  } catch (e) {
    toastError(e, '删除失败')
  }
}
</script>

<template>
  <SectionCard v-if="enabled" title="附加账号" hint="别的 115 账号：只用来挂载目录、生成 STRM 和播放；整理、转存、订阅都只用主账号">
    <ul v-if="extras.length" class="accts">
      <li v-for="a in extras" :key="a.id" class="acct">
        <div class="acct-main">
          <span class="acct-name">{{ a.name }}</span>
          <HChip :color="statusOf(a).color">{{ statusOf(a).text }}</HChip>
          <span v-if="a.user_name" class="muted small">115：{{ a.user_name }}</span>
        </div>
        <div class="acct-ops">
          <HButton size="sm" variant="secondary" @click="scan(a.id)"><QrCode :size="14" />{{ a.logged_in ? '重新登录' : '扫码登录' }}</HButton>
          <HButton size="sm" variant="ghost" @click="openImport(a)"><Upload :size="14" />导入 Cookie</HButton>
          <HButton v-if="a.logged_in" size="sm" variant="ghost" :loading="busy === a.id" @click="check(a)"><ShieldCheck :size="14" />检测</HButton>
          <HButton size="sm" variant="ghost" :aria-label="`给 ${a.name} 改名`" @click="openRename(a)"><Pencil :size="14" /></HButton>
          <HButton size="sm" variant="ghost" :aria-label="`删除 ${a.name}`" @click="remove(a)"><Trash2 :size="14" /></HButton>
        </div>
      </li>
    </ul>
    <p v-else class="muted small empty">还没有附加账号。</p>
    <HButton size="sm" variant="secondary" @click="openAdd"><Plus :size="14" />添加账号</HButton>

    <HModal v-model:show="nameDlg.show" :title="nameDlg.id ? '改名' : '添加附加账号'" width="380px" persistent>
      <HInput v-model="nameDlg.name" placeholder="比如「副号」「老婆的号」" :input-attrs="{ 'aria-label': '账号名字', maxlength: '30' }" />
      <p v-if="!nameDlg.id" class="muted small">只是给你自己看的名字。下一步用 115 App 扫码登录这个账号。</p>
      <template #footer>
        <HButton variant="tertiary" @click="nameDlg.show = false">取消</HButton>
        <HButton variant="primary" :loading="nameDlg.saving" :disabled="!nameDlg.name.trim()" @click="saveName">
          {{ nameDlg.id ? '保存' : '下一步：扫码登录' }}
        </HButton>
      </template>
    </HModal>

    <HModal v-model:show="cookieDlg.show" title="导入 Cookie" width="460px" persistent>
      <SecretInput v-model="cookieDlg.cookie" name="extra-acct-cookie" placeholder="UID=...; CID=...; SEID=...; KID=..." />
      <p class="muted small">扫码被 115 风控时用：从浏览器里复制这个账号的 Cookie，检测有效才保存。</p>
      <template #footer>
        <HButton variant="tertiary" @click="cookieDlg.show = false">取消</HButton>
        <HButton variant="primary" :loading="cookieDlg.saving" :disabled="!cookieDlg.cookie.trim()" @click="saveCookie">检测并保存</HButton>
      </template>
    </HModal>

    <QrLoginModal v-model:show="qr.show" channel="cookie" device="web" app-id="" :account="qr.id" @success="onScanned" />
  </SectionCard>
</template>

<style scoped>
.accts {
  list-style: none;
  margin: 0 0 12px;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.acct {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
}
.acct-main {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.acct-name {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.acct-ops {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.empty {
  margin: 0 0 12px;
}
.muted {
  color: var(--muted);
}
.small {
  font-size: 12px;
}
</style>
