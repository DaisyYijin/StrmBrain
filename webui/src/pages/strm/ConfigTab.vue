<script setup lang="ts">
import { computed } from 'vue'
import HButton from '@/components/hero/HButton.vue'
import HInput from '@/components/hero/HInput.vue'
import HSegmented from '@/components/hero/HSegmented.vue'
import SectionCard from '@/components/ui/SectionCard.vue'
import FieldRow from '@/components/ui/FieldRow.vue'
import FormActions from '@/components/ui/FormActions.vue'
import CopyBox from '@/components/ui/CopyBox.vue'
import AdvancedSection from '@/components/ui/AdvancedSection.vue'
import { useSetting } from '@/composables/useSetting'

// keep_ext 后端存的是字符串 'true'/'false' 而不是布尔（旧版遗留），
// 这里保持原样，改成布尔会让老配置读出来是 undefined
const { model, loading, saving, save, reset, changed } = useSetting('strm', {
  domain: '',
  format: 'pick_code_name',
  keep_ext: 'true',
  exist: 'overwrite',
})

/**
 * 按浏览器访问本站用的主机名推一个直链域名：新用户多半不知道该填什么，
 * 而 Emby 与本站同机部署时，访问后台的那个地址换成 6086 端口通常就对。
 * 只给按钮不自动写：经反代 / 域名访问时这个推算可能不对，由用户确认
 */
const suggestDomain = computed(() => {
  const host = location.hostname
  if (!host) return ''
  return `http://${host.includes(':') ? `[${host}]` : host}:6086`
})
const showSuggest = computed(() => !loading.value && !model.value.domain.trim() && !!suggestDomain.value)

const FORMAT_OPTIONS = [
  { label: 'pick_code_name', value: 'pick_code_name' },
  { label: 'pick_code', value: 'pick_code' },
]
const YES_NO = [
  { label: '是', value: 'true' },
  { label: '否', value: 'false' },
]
const EXIST_OPTIONS = [
  { label: '覆盖', value: 'overwrite' },
  { label: '跳过', value: 'skip' },
]

const example = computed(() => {
  const domain = model.value.domain.replace(/\/+$/, '')
  if (!domain) return ''
  const pickcode = 'abchrb6gnrw0hhh80'
  const id = model.value.keep_ext === 'true' ? `${pickcode}.mkv` : pickcode
  // pick_code:      /d/{pickcode}[.ext]
  // pick_code_name: /d/{pickcode}[.ext]?/{原文件名}（?/ 后的名字供播放器识别容器）
  return model.value.format === 'pick_code'
    ? `${domain}/d/${id}`
    : `${domain}/d/${id}?/钢铁侠.2008.1080p.BluRay.X264.DTS-TnT.mkv`
})
</script>

<template>
  <SectionCard title="STRM 配置" hint="直链域名与格式，全量 / 增量同步生成 STRM 时都按这里的规则写。STRM 文件名是视频名去掉扩展名（xxx.mkv → xxx.strm），与网盘上同基名的 NFO / 字幕 / 缩略图配对">
    <FieldRow
      label="STRM 直连域名"
      required
      tip="STRM 内直链的域名，需指向 302 代理端口（http://NAS_IP:6086）。播放时反代会按客户端实际访问地址自动改写，此项为兜底值。"
    >
      <HInput v-model="model.domain" type="url" mono placeholder="http://172.17.0.1:6086" />
      <p v-if="showSuggest" class="suggest">
        还没填，播放时按 http://172.17.0.1:6086 兜底（只在本机 Docker 网络里通）。
        <HButton variant="ghost" size="sm" @click="model.domain = suggestDomain">填入 {{ suggestDomain }}</HButton>
      </p>
    </FieldRow>

    <FieldRow
      v-if="example"
      label="STRM 直链示例"
      wide
      tip="按当前配置实时预览生成的直链样式，切换选项立即更新。"
    >
      <CopyBox :value="example" tone="primary" />
    </FieldRow>

    <AdvancedSection id="strm-config" :changed="changed('format', 'keep_ext', 'exist')" hint="直链格式、后缀、已存在时怎么办，默认即可">
      <FieldRow
        label="STRM 直连格式"
        :changed="changed('format')"
        tip="pick_code_name = 直链带原文件名（播放器显示友好）；pick_code = 仅文件 ID（最短）。"
      >
        <HSegmented v-model="model.format" :options="FORMAT_OPTIONS" aria-label="STRM 直连格式" />
      </FieldRow>

      <FieldRow
        label="保留文件后缀"
        :changed="changed('keep_ext')"
        tip="是 = 直链 ID 段带 .mkv 等后缀（播放器据 URL 识别容器格式）；否 = 纯 ID。"
      >
        <HSegmented v-model="model.keep_ext" :options="YES_NO" aria-label="保留文件后缀" />
      </FieldRow>

      <FieldRow
        label="本地已存在时"
        :changed="changed('exist')"
        tip="本地已有同名 STRM：覆盖 = 重新生成（推荐）；跳过 = 只生成一次。"
      >
        <HSegmented v-model="model.exist" :options="EXIST_OPTIONS" aria-label="本地已存在时" />
      </FieldRow>
    </AdvancedSection>

    <FormActions>
      <HButton variant="primary" :loading="saving" @click="save()">保存配置</HButton>
      <HButton variant="tertiary" @click="reset">重置配置</HButton>
    </FormActions>
  </SectionCard>
</template>

<style scoped>
.suggest {
  margin: 6px 0 0;
  font-size: 12.5px;
  color: var(--muted);
}
</style>
