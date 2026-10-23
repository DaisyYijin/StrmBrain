<script setup lang="ts">
import { ref } from 'vue'
import HButton from '@/components/hero/HButton.vue'
import HInput from '@/components/hero/HInput.vue'
import HSwitch from '@/components/hero/HSwitch.vue'
import SectionCard from '@/components/ui/SectionCard.vue'
import SecretInput from '@/components/ui/SecretInput.vue'
import FieldRow from '@/components/ui/FieldRow.vue'
import TestBanner, { type BannerState } from '@/components/ui/TestBanner.vue'
import { http } from '@/api/client'
import { useSetting } from '@/composables/useSetting'

const { model, saving, save } = useSetting('metatube', {
  url: '',
  token: '',
  enabled: false,
})

const banner = ref<BannerState | null>(null)
const testing = ref(false)

async function testConnection() {
  const url = (model.value.url || '').trim()
  if (!url) {
    banner.value = { status: 'err', title: '请先填写 MetaTube 服务器地址' }
    return
  }
  testing.value = true
  banner.value = { status: 'pending', title: '正在连接 metatube-server…' }
  try {
    const d = await http.post<{ success: boolean; message?: string; error?: string }>('/metatube/check', {
      url,
      token: model.value.token || '',
    })
    banner.value = d.success
      ? { status: 'ok', title: 'MetaTube 连接成功', detail: d.message }
      : { status: 'err', title: 'MetaTube 连接失败', detail: d.error }
  } catch (e) {
    banner.value = { status: 'err', title: 'MetaTube 连接失败', detail: String(e) }
  } finally {
    testing.value = false
  }
}
</script>

<template>
  <div class="h-tabs-page">
    <SectionCard
      title="MetaTube（AV 番号识别）"
      hint="对接自部署的 metatube-server，为 AV 整理提供真实标题、演员、厂牌与封面。未启用或识别失败时退回纯番号入库，互不影响。"
    >
      <div class="form">
        <FieldRow
          stacked
          label="启用识别"
          tip="启用后 AV 整理时自动按番号刮削元数据，供重命名模板（{av_title}/{actor} 等）与总览面板使用。"
        >
          <HSwitch v-model="model.enabled">整理 AV 时调用 MetaTube 刮削元数据</HSwitch>
        </FieldRow>

        <FieldRow
          stacked
          label="服务器地址"
          tip="自部署 metatube-server 的访问地址。同宿主机 Docker 部署填 http://172.17.0.1:8080，跨机填实际 IP。"
        >
          <HInput v-model="model.url" placeholder="如 http://172.17.0.1:8080" />
        </FieldRow>

        <FieldRow stacked label="Token" tip="服务端设置了 MT_TOKEN 时必填，否则留空。">
          <div class="token-row">
            <SecretInput v-model="model.token" name="metatube-token" placeholder="可留空" />
            <HButton variant="tertiary" :loading="testing" @click="testConnection">测试连接</HButton>
          </div>
          <TestBanner :state="banner" />
        </FieldRow>
      </div>
    </SectionCard>

    <div class="save">
      <HButton variant="primary" :loading="saving" @click="save()">保存配置</HButton>
    </div>
  </div>
</template>

<style scoped>
.form {
  display: flex;
  flex-direction: column;
}
.token-row {
  display: flex;
  gap: 10px;
  align-items: center;
}
.save {
  margin-top: 14px;
}
</style>
