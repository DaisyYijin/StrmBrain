import { http } from './client'
import type { QueuedReply } from './tasks'

/**
 * 移出配置的媒体库的本地残留（后端 libleft.go）：台账第一段既不是配置中的库名、也不是分类目录。
 * 清理只删本地 STRM / 附属与台账行，网盘不动；入任务队列执行
 */
export interface LibLeftover {
  name: string
  files: number
  videos: number
  /** 本地目录还在 */
  local: boolean
}

export const getLeftovers = () => http.get<{ data: LibLeftover[]; error?: string }>('/library/leftovers')
export const cleanLeftover = (name: string) => http.post<QueuedReply>('/library/leftovers/clean', { name })

// ---- 媒体库卡片（后端 libraries.go / librariessave.go） ----

/** 一条分类规则（只读显示；改去二级分类） */
export interface RuleLibRef {
  media_type: 'movie' | 'tv'
  name: string
  /** 认不出的库时带上 YAML 里写的原值 */
  library?: string
}

/** 绑定到这个库的转存子目录 */
export interface LibInbox {
  cid: string
  name: string
  lib: string
  lib_name?: string
  problem?: string
}

/** 这个库在 Emby 里有没有被覆盖 */
export interface LibEmbyState {
  covered: boolean
  partial: boolean
  /** 路径映射的本地一侧不包含这个库 */
  unmapped: boolean
  /** 应在 Emby 里添加的路径 */
  suggest?: string
}

export interface LibraryView {
  cid: string
  /** 115 目录名；认不出为空 */
  name: string
  path: string
  local: string
  /** 「其余都放这里」的那个库（full.cid） */
  default: boolean
  rules: RuleLibRef[]
  inboxes: LibInbox[]
  emby?: LibEmbyState
}

export interface LibRenameFix {
  from: string
  to: string
  lib: string
}

export interface LibrariesView {
  libs: LibraryView[]
  orphan_rules: RuleLibRef[]
  rename_fixes: LibRenameFix[]
  /** 各库名字都认得出；认不全时不报「认不出的库」 */
  names_known: boolean
  staging_root: string
  local_root: string
  emby_configured: boolean
  emby_error?: string
}

/** 一个转存入口：cid 是已有子目录；cid 为空 = 按 name 在转存目录下新建（同名已存在就复用） */
export interface LibInboxReq {
  cid?: string
  name: string
  lib: string
  /** 同名子目录已存在且不空时，用户确认过 */
  confirm?: boolean
}

export interface LibSaveReq {
  extra_libs: { cid: string; path: string }[]
  inboxes: LibInboxReq[]
  /** 这次移除的库里要同时清本地的（库 cid） */
  clean_local?: string[]
}

/** 409：转存目录里已有同名子目录、里面有东西，要用户确认 */
export interface LibInboxConfirm {
  name: string
  lib: string
  items: number
}

export const getLibraries = () => http.get<LibrariesView>('/libraries')
export const saveLibraries = (req: LibSaveReq) =>
  http.put<{ message: string; jobs?: number[]; subs_moved: number; data: LibrariesView }>('/libraries', req)
/** 库在 115 上改了名：把分类规则里写的旧名换成新名（新名由后端认，只传旧名） */
export const fixRename = (from: string) =>
  http.post<{ message: string; data: LibrariesView }>('/libraries/fix-rename', { from })
