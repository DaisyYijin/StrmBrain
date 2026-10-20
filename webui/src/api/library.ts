import { http } from './client'
import type { QueuedReply } from './tasks'
import type { LibScrape } from './organize'

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
  /** 按分类建 Emby 库时还没进 Emby 的分类目录（能换算时是 Emby 路径） */
  missing?: string[]
  /** 路径映射的本地一侧不包含这个库 */
  unmapped: boolean
  /** 应在 Emby 里添加的路径 */
  suggest?: string
}

export interface LibraryView {
  cid: string
  /** 本地目录名（STRM 第一层，规则 library 认的就是它）；没定过时是 115 目录名；认不出为空 */
  name: string
  path: string
  local: string
  /** 115 上现在的目录名；和 name 不同时界面提示一句（115 上改过名不影响本地） */
  pan_name: string
  /** 台账里已经有这个库的内容：本地目录名不能再改 */
  local_locked: boolean
  /** 「默认放这里」的库：二级分类里没写 library 的规则落进它（阶段 3 起不一定是首个库） */
  default: boolean
  /** 首个媒体库（full.cid）：115 目录在「媒体库位置」里改、不能移除 */
  first: boolean
  /** 谁来整理：organize 本站整理 / self 我自己整理（后端 libopts.go） */
  mode: LibMode
  /** 自己整理的库：片目在库下第几层（1–3） */
  depth: number
  /** 刮不刮：auto 自动刮新片 / manual 只手动刮 / off 不刮（交给 Emby） */
  scrape: LibScrape
  rules: RuleLibRef[]
  inboxes: LibInbox[]
  emby?: LibEmbyState
}

export type LibMode = 'organize' | 'self'

/** 一个库让本站做什么；不在 opts 里的库按本站整理 */
export interface LibOpt {
  mode: LibMode
  depth?: number
  scrape?: LibScrape
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
  /** 每个库的整理方式（库 cid → 选项），整份替换 */
  opts: Record<string, LibOpt>
  /** 「默认放这里」的库 cid；空 = 交给后端挑第一个本站整理的库 */
  fallback?: string
  /** 要设的本地目录名（库 cid → 名字），只带用户改过的；新库不带就用 115 目录名 */
  locals?: Record<string, string>
}

/** 409：转存目录里已有同名子目录、里面有东西，要用户确认 */
export interface LibInboxConfirm {
  name: string
  lib: string
  items: number
}

export const getLibraries = () => http.get<LibrariesView>('/libraries')

/** 自己整理的库按某个层级认出来的片目（后端 libpreview.go，只读台账） */
export interface LibPreviewTitle {
  key: string
  title: string
  media_type: 'movie' | 'tv'
  videos: number
  /** 看着不像一部片的原因 */
  odd?: string
}
export interface LibPreview {
  lib: string
  depth: number
  total: number
  titles: LibPreviewTitle[]
  odd: number
  /** 比片目层还浅、不属于任何片目的视频数 */
  loose: number
}
/** name：新加、还没保存的库按它查台账 */
export const previewLibrary = (cid: string, depth: number, name?: string) =>
  http.get<LibPreview>('/libraries/preview', { params: { cid, depth, name } })
export const saveLibraries = (req: LibSaveReq) =>
  http.put<{ message: string; jobs?: number[]; subs_moved: number; data: LibrariesView }>('/libraries', req)
/** 库在 115 上改了名：把分类规则里写的旧名换成新名（新名由后端认，只传旧名） */
export const fixRename = (from: string) =>
  http.post<{ message: string; data: LibrariesView }>('/libraries/fix-rename', { from })
