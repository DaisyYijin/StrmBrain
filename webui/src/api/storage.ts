import { http } from './client'
import type { QrCode, QrStatusResult, Storage, StorageCheck } from '@/types/storage'

export const list = () => http.get<{ data: Storage[] }>('/storage')

export interface SaveStoragePayload {
  name?: string
  type: string
  device?: string
  interval?: number
  openapi_enabled?: boolean
  app_id?: string
}

/** 后端按 type upsert，不需要前端区分新建/更新 */
export const save = (payload: SaveStoragePayload) =>
  http.post<{ data: Storage; message: string }>('/storage', payload)

export const remove = (id: number) => http.del(`/storage/${id}`)

/** 检测账号可用性：后端 OpenAPI 优先、Cookie 回退，前端不需要判断走哪条 */
export const check = (type = '115') => http.post<StorageCheck>('/storage/check', { type })

// ---- 扫码登录 ----
// Cookie 通道与 OpenAPI 通道是两组接口，但请求/响应形状一致，
// 所以下面按 channel 选路径，调用方不必写两套轮询。

export type QrChannel = 'cookie' | 'openapi'

const qrBase = (ch: QrChannel) => (ch === 'openapi' ? '/storage/open/qrcode' : '/storage/qrcode')

/** account：附加账号的 id（只走 Cookie 通道）；不带 = 主号 */
export const createQrCode = (ch: QrChannel, body: { type: string; device?: string; app_id?: string; account?: number }) =>
  http.post<QrCode>(qrBase(ch), body)

export const qrStatus = (
  ch: QrChannel,
  body: { uid?: string; time?: number | string; sign?: string },
) => http.post<QrStatusResult>(`${qrBase(ch)}/status`, body)

// ---- 附加 115 账号（后端 account115.go，MULTI-ACCOUNT-PLAN.md 阶段 2） ----

/** 账号列表的一行。主号 id 恒为 0；状态与用户名是上次登录 / 检测时记下的（零 115 请求） */
export interface Account115 {
  id: number
  /** 115 用户名；附加账号还没登录过时是「未登录账号」 */
  name: string
  user_name: string
  main: boolean
  /** online / offline / invalid */
  status: string
  device: string
  logged_in: boolean
}

export const accounts = () => http.get<{ data: Account115[] }>('/storage/115/accounts')
export const createAccount = () => http.post<{ data: Account115 }>('/storage/115/accounts', {})
export const deleteAccount = (id: number) => http.del(`/storage/115/accounts/${id}`)
/** 检测附加账号 */
export const checkAccount = (id: number) =>
  http.post<StorageCheck & { warning?: string }>(`/storage/115/accounts/${id}/check`, {})

// ---- 目录浏览 ----

export interface DirEntry {
  /** 115 目录用 cid 标识，本地用 path */
  cid?: string
  name: string
  path?: string
}

export interface DirListResult {
  data?: DirEntry[]
  /** 115：目录总条目数。items 为空但 count>0 说明该目录下只有文件没有子文件夹 */
  count?: number
  channel?: string
  origin?: string
  /** 本地：目录过大时只返回前 1000 个 */
  truncated?: boolean
}

/** 附加账号的 id 才带上 account（0 / 不传 = 主号，参数保持老样子） */
const acctParam = (account?: number) => (account ? { account } : {})

/** refresh 跳过后端 5 分钟的目录缓存，给选择器的「刷新」按钮用 */
export const dirs115 = (cid: string, refresh = false, account?: number) =>
  http.get<DirListResult>('/storage/115/dirs', { params: { cid, ...(refresh ? { refresh: 1 } : {}), ...acctParam(account) } })

/** 把 /影视/电影 这样的路径逐段解析成 cid */
export const resolve115 = (path: string, account?: number) =>
  http.get<{ cid?: string }>('/storage/115/resolve', { params: { path, ...acctParam(account) } })

/** 把历史配置中的裸 cid 反查为可读路径 */
export const path115 = (cid: string, account?: number) =>
  http.get<{ cid: string; path: string }>('/storage/115/path', { params: { cid, ...acctParam(account) } })

export const localDirs = (path: string) => http.get<DirListResult>('/storage/local/dirs', { params: { path } })

export const diagnose115 = () => http.get('/storage/115/diagnose')
