<script setup lang="ts">
import { computed } from 'vue'
import HButton from '@/components/hero/HButton.vue'
import HSwitch from '@/components/hero/HSwitch.vue'
import HSelect from '@/components/hero/HSelect.vue'
import HAlert from '@/components/hero/HAlert.vue'
import SectionCard from '@/components/ui/SectionCard.vue'
import FieldRow from '@/components/ui/FieldRow.vue'
import FormActions from '@/components/ui/FormActions.vue'
import { useSetting } from '@/composables/useSetting'

/**
 * setting `media-probe`：整理改名前按直链读一点文件头，补全命名模板里文件名没写的画质变量
 * （后端 internal/api/mediaprobe.go，解析在 internal/mediaprobe）
 */
interface MediaProbeCfg {
  enabled: boolean
  /** 读到的一律盖过文件名里写的；默认只补没写的 */
  overwrite: boolean
  /** 剧集每集都读；默认同一个版本只读一集 */
  each_episode: boolean
}
const DEFAULTS: MediaProbeCfg = { enabled: false, overwrite: false, each_episode: false }

/** 当前的四条命名模板：一个能补的变量都没用到时提示一句，开了也不会读 */
const props = defineProps<{ templates: string[] }>()

const { model, saving, save, dirty } = useSetting('media-probe', DEFAULTS)

const VARS = ['{resource_pix', '{resource_effect', '{video_encode', '{audio_encode', '{fps', '{resource_type']
const usesVars = computed(() => props.templates.some((t) => VARS.some((v) => (t || '').includes(v))))

const WRITE_MODES = [
  { label: '只补文件名里没写的', value: 'fill' },
  { label: '以读到的为准（盖过文件名里写的）', value: 'overwrite' },
]
const writeMode = computed({
  get: () => (model.value.overwrite ? 'overwrite' : 'fill'),
  set: (v: string) => {
    model.value.overwrite = v === 'overwrite'
  },
})

const EP_MODES = [
  { label: '同一个版本只读一集', value: 'one' },
  { label: '每集都读', value: 'each' },
]
const epMode = computed({
  get: () => (model.value.each_episode ? 'each' : 'one'),
  set: (v: string) => {
    model.value.each_episode = v === 'each'
  },
})
</script>

<template>
  <SectionCard title="读取媒体信息" hint="整理改名前读一点视频文件头，补上文件名里没写的画质信息">
    <FieldRow
      label="补全命名"
      tip="开着时，命名模板里用到了分辨率、特效、视频编码、音频编码、帧率（蓝光 ISO 还有资源质量），而文件名里没写，整理会按 115 直链读一点文件头补上。每读一个文件要取一次直链，再发几次分段请求，最多读几 MB（MKV、MP4 通常几百 KB），不下载整个文件。读过的按文件 sha1 记住，重新整理不再读。"
    >
      <div class="switch-row">
        <HSwitch v-model="model.enabled" />
        <span class="switch-hint">{{ model.enabled ? '文件名没写的画质信息读文件补上' : '只按文件名（默认）' }}</span>
      </div>
    </FieldRow>

    <template v-if="model.enabled">
      <FieldRow
        label="写入方式"
        tip="「只补没写的」：文件名写了 1080p 就用 1080p，没写才用读到的。「以读到的为准」：读到的一律盖过文件名里写的，文件名写错了也能纠正，但每个文件都要读。"
      >
        <HSelect v-model="writeMode" :options="WRITE_MODES" aria-label="写入方式" />
      </FieldRow>
      <FieldRow
        label="剧集"
        tip="同一个目录里文件名画质串、发布组、扩展名一样、体积相近的几集算同一个版本，只读第一集，其余沿用。几百集的番逐集读要取几百次直链，整理期间同步都在排队。"
      >
        <HSelect v-model="epMode" :options="EP_MODES" aria-label="剧集读取方式" />
      </FieldRow>

      <HAlert v-if="!usesVars" status="warning" class="note">
        上面的命名模板一个画质变量都没用到，开着也不会读。要补哪个，就把它加进模板，比如 <code>&lt;.{resource_pix}&gt;</code>。
      </HAlert>
      <ul class="facts">
        <li>能读：MKV、MP4 / MOV、TS / M2TS、蓝光 ISO（UHD 盘写成 <code>UHD.BluRay</code>）、DVD 镜像。AVI、RMVB 等老格式照旧按文件名。</li>
        <li>声道数、TrueHD 全景声、DTS-HD MA 与 DTS:X 读音频帧认；MP4 / MKV 里 DDP 的全景声只认得出容器写明的。</li>
        <li>洗版判定看的也是补全后的画质。</li>
      </ul>
    </template>

    <FormActions>
      <HButton variant="primary" :loading="saving" :disabled="!dirty" @click="save()">保存配置</HButton>
    </FormActions>
  </SectionCard>
</template>

<style scoped>
.switch-row {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 34px;
}
.switch-hint {
  font-size: 12.5px;
  color: var(--muted);
}
.note {
  margin: 8px 0;
}
.facts {
  margin: 8px 0 0;
  padding-left: 18px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--muted);
}
code {
  font-family: var(--font-mono);
}
</style>
