import { http } from './client'

// ============ TMDB 选片（影视转存与整理记录共用） ============
export interface TmdbCandidate {
  id: number
  title: string
  year?: string
  media_type: 'movie' | 'tv'
  poster?: string
  overview?: string
  vote?: number
  /** 原名：影视转存用它判断资源标题是不是这部片 */
  original_title?: string
}

export const tmdbSearch = (query: string) =>
  http.get<{ data?: TmdbCandidate[]; hint?: string }>('/tmdb/search', { params: { query } })

/** TMDB 榜单（找资源页的趋势 / 热门）：不带 list 只返回榜单清单 */
export interface DiscoverList {
  key: string
  label: string
}
export const tmdbDiscoverLists = () => http.get<{ lists: DiscoverList[] }>('/tmdb/discover')
export const tmdbDiscover = (list: string, page = 1) =>
  http.get<{ data?: TmdbCandidate[]; has_more?: boolean }>('/tmdb/discover', { params: { list, page } })

export const tmdbImageUrl = (path: string, size = 'w154') =>
  `/api/tmdb/img?path=${encodeURIComponent(path)}&size=${size}`

// ============ 盘搜 PanSou（搜索走 /transfer/resources） ============
export const pansouConfig = () => http.get<{ base_url?: string }>('/pansou/config')
export const savePansou = (base_url: string) => http.post<{ base_url?: string }>('/pansou/config', { base_url })

// ============ TG 频道 ============
export const tgConfig = () => http.get<{ channels?: string }>('/tgsearch/config')
export const tgSaveConfig = (channels: string) => http.post<{ channels?: string }>('/tgsearch/config', { channels })

// ============ 观影 ============
export interface GyConfig {
  base_url?: string
  username?: string
  password?: string
  logged_in?: boolean
}

export const gyConfig = () => http.get<GyConfig>('/guanying/config')
export const gyCheck = () => http.get<{ logged_in: boolean }>('/guanying/check')
export const gySaveConfig = (body: { base_url: string; username: string; password: string }) =>
  http.post('/guanying/config', body)
export const gyLogin = (body: { base_url: string; username: string; password: string }) =>
  http.post<{ message?: string }>('/guanying/login', body, { timeoutMs: 120_000 })
export const gyLogout = () => http.post('/guanying/logout')

// ============ 不太灵影视 ============
export const mkConfig = () =>
  http.get<{ base_url?: string; username?: string; has_token?: boolean; token_at?: string }>('/mukaku/config')
export const mkSaveConfig = (body: { base_url?: string; token?: string }) => http.post('/mukaku/config', body)
export const mkCaptcha = () => http.get<{ img: string; key: string }>('/mukaku/captcha')
export const mkLogin = (body: { username: string; password: string; code: string; key: string }) =>
  http.post('/mukaku/login', body, { timeoutMs: 60_000 })

// ============ RE0 ============
export const re0Config = () =>
  http.get<{
    base_url?: string
    client_id?: string
    client_secret?: string
    authorized?: boolean
    authorized_as?: string
    /** 授权里缺的权限（2026-10 之前的授权没有 meta / write），非空要重新授权 */
    missing_scopes?: string[] | null
    /** 网页登录（2026-10-14 起，RE0 不再开放 OpenAPI 申请） */
    username?: string
    password?: string
    use_proxy?: boolean
    /** 当前走哪条通道：openapi（已授权的老用户优先）/ web / 空 = 未配置 */
    channel?: '' | 'openapi' | 'web'
  }>('/re0/config')
export interface Re0SaveBody {
  base_url: string
  client_id: string
  client_secret: string
  username: string
  password: string
  use_proxy: boolean
}
export const re0SaveConfig = (body: Re0SaveBody) => http.post<{ message?: string }>('/re0/config', body)
export interface Re0CheckResult {
  app_ok: boolean
  app_name?: string
  authorized: boolean
  user?: string
  level?: string | number | null
  points?: number | null
  banned?: boolean
  missing_scopes?: string[] | null
  message?: string
  /** web = 这次检查的是网页登录 */
  channel?: string
}
// 网页通道要真开浏览器登录一次，可能要过 Cloudflare 验证（最长 90 秒）
export const re0Check = () => http.get<Re0CheckResult>('/re0/check', { timeoutMs: 200_000 })
/** 网页登录用的浏览器（CloakBrowser）：本地状态与下载进度 */
export interface Re0BrowserState {
  state: 'missing' | 'downloading' | 'verifying' | 'extracting' | 'ready' | 'error' | 'unsupported'
  version: string
  platform: string
  path?: string
  done: number
  total: number
  speed: number
  source?: string
  error?: string
  reason?: string
}
/** 正在做的网页操作走到哪一步 */
export interface Re0OpState {
  what: string
  step: string
  /** 开始时间（unix 秒） */
  since: number
}
export const re0Browser = () =>
  http.get<{ browser: Re0BrowserState; running: boolean; channel: string; op?: Re0OpState }>('/re0/browser')
export const re0BrowserDownload = (force = false) =>
  http.post<{ message: string; browser: Re0BrowserState }>('/re0/browser/download', { force })
export interface Re0Checkin {
  enabled: boolean
  last_done?: string
  last_result?: string
  last_result_at?: string
  done_today: boolean
  hour: number
  /** 签不了的原因（未授权 / 授权缺 write），空 = 可以签 */
  blocked?: string
}
export const re0Checkin = () => http.get<Re0Checkin>('/re0/checkin')
export const re0SaveCheckin = (enabled: boolean) => http.post('/re0/checkin', { enabled })
// 网页签到要开浏览器、可能要登录与过验证
export const re0RunCheckin = () => http.post<{ message: string }>('/re0/checkin/run', {}, { timeoutMs: 260_000 })
export const re0OAuthStart = (redirectUri: string) =>
  http.get<{ authorize_url?: string }>('/re0/oauth/start', { params: { redirect_uri: redirectUri } })
export const re0EgressIP = () => http.get<{ ip: string; ipv6: boolean; exact: boolean }>('/re0/egress-ip')
