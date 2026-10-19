import { http } from './client'
import type { CalibrateResult, Dashboard } from '@/types/dashboard'

/** refresh=true 跳过后端 60 秒的 Emby 缓存：用户点「刷新」就是想立刻核对数字 */
export const get = (refresh = false) =>
  http.get<Dashboard>('/dashboard', { params: refresh ? { refresh: 1 } : undefined })

/** 媒体库台账校准。apply=false 只预演，返回将被清除的条目 */
export const calibrate = (apply: boolean) =>
  http.post<CalibrateResult>('/media-library/calibrate', { apply })

// ---- 上手清单（总览顶部卡片，internal/api/setup.go）----
export interface SetupStep {
  key: 'pan115' | 'library' | 'workspace' | 'tmdb' | 'full' | 'emby' | 'organize'
  title: string
  done: boolean
  detail: string
  optional?: boolean
}
export interface SetupStatus {
  steps: SetupStep[]
  /** 必做的几步都做完了（可选的 Emby / 整理不算） */
  done: boolean
  hidden: boolean
}
export const setupStatus = () => http.get<SetupStatus>('/setup/status')
export const setupHide = (hidden: boolean) => http.post<{ hidden: boolean }>('/setup/hide', { hidden })
