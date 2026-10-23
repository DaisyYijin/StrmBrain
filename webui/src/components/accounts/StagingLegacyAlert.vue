<script setup lang="ts">
/**
 * 待整理并进转存时弃用的旧待整理目录（stagingdir.go 记在 setting staging_legacy）：媒体库页签顶上提示一次。
 *
 * 原来这里是一条「所有库共用」：本地媒体根、已存在、冗余。已存在 / 冗余只有整理用得上，
 * 只在「自动整理 → 基础配置」里显示和改（一键创建也在那里）；本地媒体根写进媒体库卡片的说明里。
 */
import { onMounted, ref } from 'vue'
import HAlert from '@/components/hero/HAlert.vue'
import HButton from '@/components/hero/HButton.vue'
import { configApi } from '@/api'
import { toastError } from '@/composables/useFeedback'

const legacy = ref('')

onMounted(async () => {
  try {
    const l = await configApi.getSetting<{ cid?: string; path?: string }>('staging_legacy', {})
    legacy.value = l.cid ? l.path || `cid ${l.cid}` : ''
  } catch {
    legacy.value = ''
  }
})

async function dismiss() {
  try {
    await configApi.saveSetting('staging_legacy', {})
    legacy.value = ''
  } catch (e) {
    toastError(e, '操作失败')
  }
}
</script>

<template>
  <HAlert v-if="legacy" status="warning" title="待整理已并进转存目录">
    原来的待整理目录（{{ legacy }}）不再自动整理。里面如果还有要整理的，请移到对应媒体库的转存/待整理文件夹。
    <template #actions>
      <HButton variant="tertiary" size="sm" @click="dismiss">知道了</HButton>
    </template>
  </HAlert>
</template>
