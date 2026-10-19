import { ref } from 'vue'
import { dashboardApi } from '@/api'

/**
 * 「不再显示」之后从右上角账号菜单重新打开上手清单。
 * 打开时人可能就在总览上（清单组件不会重新挂载），所以用一个计数让它自己重读。
 */
export const setupReopenTick = ref(0)

export async function reopenSetupChecklist() {
  await dashboardApi.setupHide(false)
  setupReopenTick.value++
}
