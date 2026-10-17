<script setup lang="ts">
import { ref } from 'vue'
import HModal from '@/components/hero/HModal.vue'
import HButton from '@/components/hero/HButton.vue'
import HChip from '@/components/hero/HChip.vue'
import { heroTone } from '@/components/hero/tone'
import SectionCard from '@/components/ui/SectionCard.vue'
import FormActions from '@/components/ui/FormActions.vue'
import { useSetting } from '@/composables/useSetting'
import VariablesTab from './VariablesTab.vue'
import MediaProbeCard from '@/components/organize/MediaProbeCard.vue'
import RenameRuleField from './RenameRuleField.vue'
import { DEFAULT_RENAME, renderRenameExample } from '@/utils/rename'

const { model, saving, save, reset } = useSetting('org-rename', DEFAULT_RENAME)

const GROUPS = [
  {
    type: 'movie' as const,
    label: '电影',
    tagType: 'info' as const,
    folderTip: '入库后的文件夹名模板。用 / 可以建多级目录。',
    fileTip: '入库后的文件名模板，必须以 {ext} 结尾才能保住扩展名。',
  },
  {
    type: 'tv' as const,
    label: '剧集',
    tagType: 'success' as const,
    folderTip: '入库后的文件夹名模板。默认带 /Season {season_num} 一层；若去掉，入库时会自动补一层 Season 目录。',
    fileTip: '入库后的文件名模板，必须以 {ext} 结尾才能保住扩展名。',
  },
]

const rulesVisible = ref(false)

/**
 * 「读取媒体信息」卡片排在两组模板下面，要滚一屏多才看得到；
 * 标题旁放个入口写明开没开，点了滚过去
 */
const probeOn = ref(false)
const probeEl = ref<{ $el: HTMLElement } | null>(null)
function gotoProbe() {
  probeEl.value?.$el.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
</script>

<template>
  <SectionCard title="重命名策略" hint="入库后的目录与文件命名模板">
    <template #extra>
      <div class="head-extra">
        <HButton variant="ghost" size="sm" @click="gotoProbe">
          读取媒体信息：<span :class="probeOn ? 'on' : 'off'">{{ probeOn ? '已开' : '未开' }}</span>
        </HButton>
        <HButton variant="ghost" size="sm" @click="rulesVisible = true">规则说明</HButton>
      </div>
    </template>

    <div v-for="g in GROUPS" :key="g.type" class="group">
      <div class="group-head">
        <HChip :color="heroTone(g.tagType)">{{ g.label }}</HChip>
        <span class="path">
          <code>{{ renderRenameExample(model[`${g.type}_folder`]) }}</code>/<code>{{
            renderRenameExample(model[`${g.type}_file`])
          }}</code>
        </span>
      </div>

      <RenameRuleField
        v-model="model[`${g.type}_folder`]"
        label="文件夹命名规则"
        :tip="g.folderTip"
        :scope="g.type"
        kind="folder"
        :fallback="DEFAULT_RENAME[`${g.type}_folder`]"
      />

      <RenameRuleField
        v-model="model[`${g.type}_file`]"
        label="文件命名规则"
        :tip="g.fileTip"
        :scope="g.type"
        kind="file"
        :fallback="DEFAULT_RENAME[`${g.type}_file`]"
      />
    </div>

    <FormActions>
      <HButton variant="primary" :loading="saving" @click="save()">保存配置</HButton>
      <HButton variant="tertiary" @click="reset">重置配置</HButton>
    </FormActions>
  </SectionCard>

  <!-- 模板一个画质变量都没用到时卡片里提示一句：开着也不会读 -->
  <MediaProbeCard
    ref="probeEl"
    class="probe-card"
    @enabled="probeOn = $event"
    :templates="[model.movie_folder, model.movie_file, model.tv_folder, model.tv_file]"
  />

  <HModal v-model:show="rulesVisible" title="重命名规则说明" width="920px">
    <VariablesTab />
  </HModal>
</template>

<style scoped>
.group {
  padding: 14px 16px;
  margin-bottom: 14px;
  border: 1px solid var(--c-border);
  border-radius: var(--radius);
}
.group-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 4px;
  flex-wrap: wrap;
}
/* 组标题旁直接摆出整条入库路径，不用把两行示例在脑子里拼起来 */
.path {
  min-width: 0;
  font-size: 11.5px;
  color: var(--c-text-4);
  word-break: break-all;
}
.probe-card {
  margin-top: 16px;
  scroll-margin-top: 80px;
}
.head-extra {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.on {
  color: var(--success);
}
.off {
  color: var(--muted);
}
.path code {
  font-family: var(--font-mono);
  color: var(--c-text-3);
}
</style>
