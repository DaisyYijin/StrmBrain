import { computed, ref, watch } from 'vue'
import type Cid115Input from '@/components/Cid115Input.vue'
import { storageApi } from '@/api'
import { useSetting } from '@/composables/useSetting'
import { useFeedback } from '@/composables/useFeedback'

/**
 * 「转存/待整理」目录（setting `share` 的 folder / folder_path）。
 *
 * 2026-10-12 起转存与待整理是同一个目录（后端 stagingdir.go）：转存、离线下载、订阅的内容落在这里，
 * 手动要整理的也丢这里，守望者每分钟接管。这个设置项在「自动整理 → 基础配置」与「影视转存 → 设置」
 * 两处都能改，存的是同一个 key、只有这两个字段，所以两边各自整存不会互相覆盖别的东西。
 */
export function useStagingDir() {
  const { message } = useFeedback()
  const share = useSetting('share', { folder: '', folder_path: '' })
  /** 输入框的值：给人看的是路径，落库的是 cid */
  const value = ref({ cid: '', path: '' })
  const input = ref<InstanceType<typeof Cid115Input> | null>(null)

  watch(
    () => [share.model.value.folder, share.model.value.folder_path] as const,
    async ([cid, savedPath]) => {
      if (!cid) {
        value.value = { cid: '', path: '' }
        return
      }
      const displayPath = savedPath || cid
      if (cid !== value.value.cid || displayPath !== value.value.path) {
        value.value = { cid, path: displayPath }
      }
      // 只存过 cid 的旧配置：进页面反查一次可读路径，别让用户对着数字猜目录。
      if (!savedPath) {
        try {
          const resolved = await storageApi.path115(cid)
          if (share.model.value.folder === cid && resolved.path) {
            value.value = { cid, path: resolved.path }
          }
        } catch {
          // Cookie 暂不可用时保留 cid；重新选择目录或下次保存仍可补齐。
        }
      }
    },
    { immediate: true },
  )

  /** 比 cid 不比 path：Cid115Input 改了路径就把 cid 作废，解析成功才填回（同 BasicTab 的 dirsDirty） */
  const dirty = computed(() => value.value.cid.trim() !== (share.model.value.folder || '').trim())

  async function save(): Promise<boolean> {
    const cid = (await input.value?.ensureCid()) ?? ''
    if (!cid) {
      message.error('转存/待整理目录路径无法识别：请点「选择目录」重新选择，或输入纯数字 cid')
      return false
    }
    share.model.value.folder = cid
    let readablePath = value.value.path.trim()
    if (!readablePath || /^\d+$/.test(readablePath)) {
      try {
        readablePath = (await storageApi.path115(cid)).path
      } catch {
        readablePath = ''
      }
    }
    share.model.value.folder_path = readablePath
    return share.save()
  }

  return { value, input, dirty, saving: share.saving, save }
}
