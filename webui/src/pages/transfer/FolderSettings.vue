<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import HButton from '@/components/hero/HButton.vue'
import HSelect from '@/components/hero/HSelect.vue'
import SectionCard from '@/components/ui/SectionCard.vue'
import FieldRow from '@/components/ui/FieldRow.vue'
import FormActions from '@/components/ui/FormActions.vue'
import Cid115Input from '@/components/Cid115Input.vue'
import StagingLibsCard from '@/components/transfer/StagingLibsCard.vue'
import { transferApi } from '@/api'
import type { XferTargets } from '@/api/transfer'
import { useStagingDir } from '@/composables/stagingDir'
import { useStagingSubs } from '@/composables/stagingSubs'
import { useTransferSources } from '@/composables/transferSources'
import { toastError, useFeedback } from '@/composables/useFeedback'

/**
 * 转存放到哪里。
 * 媒体库表（V2，后端 xfertarget.go）：每个库一个转存/待整理文件夹（在「账号与媒体库 → 媒体库」设），
 * 这里只选「默认转存库」：只有一个库时不用选；多个库没设默认 = 每次提交都问。
 * 老口径（媒体库表还空）：共用一个转存目录（setting `share`）+ 子目录绑定
 */
const { message } = useFeedback()
const { state, reload } = useTransferSources()
const dir = useStagingDir()
const { value, saving } = dir
const subs = useStagingSubs()

const targets = ref<XferTargets | null>(null)
const pick = ref(0)
const savingDef = ref(false)
async function loadTargets() {
  try {
    targets.value = await transferApi.xferTargets()
    pick.value = targets.value.default
  } catch {
    targets.value = null
  }
}
onMounted(loadTargets)

const libOpts = computed(() => [
  { label: '不设（每次提交都问）', value: 0 },
  ...(targets.value?.libs ?? []).map((l) => ({
    label: targets.value?.multi_account && l.account_name ? `${l.name}（${l.account_name}）` : l.name,
    value: l.id,
  })),
])

async function saveDefault() {
  savingDef.value = true
  try {
    targets.value = await transferApi.saveXferDefault(pick.value)
    message.success('已保存')
    void subs.reload()
  } catch (e) {
    toastError(e, '保存失败')
  } finally {
    savingDef.value = false
  }
}

async function saveShare() {
  if (await dir.save()) reload() // 找资源 / 链接转存上方显示的转存目录跟着变
}
</script>

<template>
  <div class="stack">
    <template v-if="targets?.v2">
      <SectionCard title="默认转存库" hint="找资源、链接转存、离线下载、机器人与订阅提交的内容，放进哪个库的转存/待整理文件夹">
        <p v-if="!targets.libs.length" class="muted">
          还没有库设转存/待整理文件夹：到「<RouterLink :to="{ name: 'accounts', query: { tab: 'library' } }">账号与媒体库 → 媒体库</RouterLink>」给库设一个。
        </p>
        <p v-else-if="targets.libs.length === 1" class="muted">
          只有一个库能转存：都放进「{{ targets.libs[0].name }}」的转存文件夹（{{ targets.libs[0].staging_path }}），不用选。
        </p>
        <template v-else>
          <FieldRow
            label="默认转存到"
            tip="设了之后提交时默认放进这个库（找资源、链接转存上还能临时换）；不设 = 每次提交都要选。机器人提交只认默认，没设时会提醒你来这里设。订阅新建时也默认用它。"
          >
            <HSelect v-model="pick" :options="libOpts" aria-label="默认转存库" />
          </FieldRow>
          <FormActions>
            <HButton variant="primary" :loading="savingDef" @click="saveDefault">保存</HButton>
          </FormActions>
        </template>
        <ul v-if="targets.libs.length" class="libs muted">
          <li v-for="l in targets.libs" :key="l.id">{{ l.name }} → {{ l.staging_path }}</li>
        </ul>
      </SectionCard>
    </template>
    <template v-else>
      <SectionCard title="转存/待整理目录" hint="链接转存、离线下载、找资源与订阅提交的内容都落在这里，手动要整理的也放这里，由自动整理接管入库">
        <FieldRow label="转存/待整理" tip="必须与媒体库目录互不包含。提交后立即触发一次整理；手动放进来的、没赶上的，由守望者每分钟检查一次这个目录接管。">
          <Cid115Input :ref="(el) => (dir.input.value = el as never)" v-model="value" placeholder="转存 / 离线下载 / 待整理的目录" />
        </FieldRow>
        <FormActions>
          <HButton variant="primary" :loading="saving" @click="saveShare">保存目录</HButton>
        </FormActions>
      </SectionCard>
      <!-- 换了（已保存的）转存目录，原来的子目录就不在它下面了：跟着重建、重新列子目录 -->
      <StagingLibsCard :key="state?.folder ?? ''" />
    </template>
  </div>
</template>

<style scoped>
.stack {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.muted {
  color: var(--muted);
  font-size: 13px;
}
.libs {
  margin: 12px 0 0;
  padding-left: 18px;
  line-height: 1.7;
}
</style>
