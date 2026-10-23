<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import HButton from '@/components/hero/HButton.vue'
import HSelect from '@/components/hero/HSelect.vue'
import SectionCard from '@/components/ui/SectionCard.vue'
import FieldRow from '@/components/ui/FieldRow.vue'
import FormActions from '@/components/ui/FormActions.vue'
import { transferApi } from '@/api'
import type { XferTargets } from '@/api/transfer'
import { useStagingSubs } from '@/composables/stagingSubs'
import { useTransferSources } from '@/composables/transferSources'
import { toastError, useFeedback } from '@/composables/useFeedback'

/**
 * 默认转存库。媒体库表（V2，后端 xfertarget.go）里每个库一个转存/待整理文件夹，
 * 在「账号与媒体库 → 媒体库」设；这里只剩「多个库时默认放进哪个」这一项能改。
 * 只有一个库时没东西可选，SettingsTab 整个小节都不显示（2026-10-23，维护者要求删掉改不了的设置）。
 * 原来的共用转存目录（setting `share`）与子目录绑定在 V2 下已不起作用，一并删了
 */
const props = defineProps<{ targets: XferTargets }>()
const emit = defineEmits<{ saved: [XferTargets] }>()
const { message } = useFeedback()
const { reload } = useTransferSources()
const subs = useStagingSubs()

const pick = ref(props.targets.default)
watch(
  () => props.targets.default,
  (v) => (pick.value = v),
)
const saving = ref(false)

const libOpts = computed(() => [
  { label: '不设（每次提交都问）', value: 0 },
  ...props.targets.libs.map((l) => ({
    label: props.targets.multi_account && l.account_name ? `${l.name}（${l.account_name}）` : l.name,
    value: l.id,
  })),
])

async function save() {
  saving.value = true
  try {
    emit('saved', await transferApi.saveXferDefault(pick.value))
    message.success('已保存')
    void subs.reload()
    void reload() // 找资源 / 链接转存上方显示的转存目录跟着变
  } catch (e) {
    toastError(e, '保存失败')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <SectionCard title="默认转存库" hint="找资源、链接转存、离线下载、机器人与订阅提交的内容，放进哪个库的转存/待整理文件夹">
    <FieldRow
      label="默认转存到"
      tip="设了之后提交时默认放进这个库（找资源、链接转存上还能临时换）；不设 = 每次提交都要选。机器人提交只认默认，没设时会提醒你来这里设。订阅新建时也默认用它。"
    >
      <HSelect v-model="pick" :options="libOpts" aria-label="默认转存库" />
    </FieldRow>
    <FormActions>
      <HButton variant="primary" :loading="saving" @click="save">保存</HButton>
    </FormActions>
    <ul class="libs">
      <li v-for="l in targets.libs" :key="l.id">{{ l.name }} → {{ l.staging_path }}</li>
    </ul>
    <p class="muted">
      各库的转存/待整理文件夹在「<RouterLink :to="{ name: 'accounts', query: { tab: 'library' } }">账号与媒体库 → 媒体库</RouterLink>」里改。
    </p>
  </SectionCard>
</template>

<style scoped>
.muted {
  margin: 8px 0 0;
  color: var(--muted);
  font-size: 13px;
}
.libs {
  margin: 12px 0 0;
  padding-left: 18px;
  line-height: 1.7;
  color: var(--muted);
  font-size: 13px;
}
</style>
