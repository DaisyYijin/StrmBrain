<script setup lang="ts">
import HButton from '@/components/hero/HButton.vue'
import SectionCard from '@/components/ui/SectionCard.vue'
import FieldRow from '@/components/ui/FieldRow.vue'
import FormActions from '@/components/ui/FormActions.vue'
import Cid115Input from '@/components/Cid115Input.vue'
import StagingLibsCard from '@/components/transfer/StagingLibsCard.vue'
import { useStagingDir } from '@/composables/stagingDir'
import { useTransferSources } from '@/composables/transferSources'

/** 转存/待整理目录（setting `share`）。原在「链接转存」页签下方，并页时挪进设置；「自动整理 → 基础配置」里也能改 */
const { state, reload } = useTransferSources()
const dir = useStagingDir()
const { value, saving } = dir

async function saveShare() {
  if (await dir.save()) reload() // 找资源 / 链接转存上方显示的转存目录跟着变
}
</script>

<template>
  <div class="stack">
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
  </div>
</template>

<style scoped>
.stack {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
</style>
