import { http } from './client'
import type { QueuedReply } from './tasks'

/**
 * 其他媒体库目录：只用于识别片目（海报墙、网盘文件页、对账），整理分类不读它，见后端 extradirs.go。
 * dir 是库内相对路径、不带库名，写法同分类规则的名字。
 */
export type ExtraDirType = 'movie' | 'tv' | 'auto'
export interface ExtraDir {
  /** 库内相对路径；限定了库（lib 非空）时可以为空 = 库根本身就是分类目录 */
  dir: string
  type: ExtraDirType
  /** 只对这个库（本地媒体树第一层的库目录名）生效；空 = 所有库 */
  lib?: string
  /** 怎么来的：空 = 手动；emby / record = 从建议采纳 */
  source?: string
  /** 登记时间（RFC3339） */
  at?: string
}

export interface ExtraDirsReply {
  dirs: ExtraDir[]
  /** 和分类规则同名、被丢掉的说明 */
  skipped?: string[]
}

export const getExtraDirs = () => http.get<{ dirs: ExtraDir[]; libs: string[] }>('/library/extra-dirs')
export const saveExtraDirs = (dirs: ExtraDir[]) => http.post<ExtraDirsReply>('/library/extra-dirs', { dirs })
export const addExtraDirs = (dirs: ExtraDir[]) => http.post<ExtraDirsReply>('/library/extra-dirs/add', { dirs })

/**
 * 媒体库布局（后端 liblayout*.go，设计见 docs/115-station-notes/LIBRARY-LAYOUT-PLAN.md）：
 * 哪些目录是分类目录（片目在它下一层）。来源有分类规则（只读，改去自动整理）、手动、从 Emby 建议采纳的。
 * 写入一律先 preview 看后果、再 adopt；条目存在其他媒体库目录里。
 */
export type LayoutSource = 'rule' | 'manual' | 'emby' | 'record'
export interface LayoutRow {
  /** 空 = 对所有库生效 */
  lib: string
  /** 空 = 库根本身（只在 lib 非空时出现） */
  dir: string
  type: ExtraDirType
  source: LayoutSource
  at?: string
  titles: number
  /** 只给规则条目：empty = 台账里没片；never_organized = 有片但从没整理进来过 */
  hint?: 'empty' | 'never_organized'
}
export interface LayoutUnclaimed {
  videos: number
  dirs: { path: string; videos: number }[]
  more_dirs?: number
}
export interface LayoutOverview {
  rows: LayoutRow[]
  /** 台账里出现过的库目录名（登记「只对某个库生效」时选） */
  libs: string[]
  unclaimed: LayoutUnclaimed
  titles: number
}
export interface LayoutSuggestion {
  lib: string
  dir: string
  type: ExtraDirType
  source: LayoutSource
  titles: number
  samples: string[]
}
export interface LayoutUnsure {
  lib: string
  dir: string
  reason: string
  samples: string[]
}
export interface LayoutSuggestReply {
  configured: boolean
  result?: { mapping_ok: boolean; suggestions: LayoutSuggestion[]; unsure: LayoutUnsure[]; dismissed: number }
}
export interface LayoutChange {
  add?: ExtraDir[]
  remove?: ExtraDir[]
  source?: LayoutSource
}
export interface LayoutPreview {
  new_titles: number
  corroborated: number
  new_samples: string[] | null
  uncorroborated: string[] | null
  uncorroborated_n: number
  changed: number
  changed_samples: string[] | null
  lost: number
  lost_samples: string[] | null
  queued_scrapes: number
}

export const getLayout = (refresh = false) => http.get<LayoutOverview>('/library/layout', { params: refresh ? { refresh: 1 } : {} })
export const getLayoutSuggest = (refresh = false) =>
  http.get<LayoutSuggestReply>('/library/layout/suggest', { params: refresh ? { refresh: 1 } : {}, timeoutMs: 120000 })
export const previewLayout = (c: LayoutChange) => http.post<{ preview: LayoutPreview; skipped?: string[] }>('/library/layout/preview', c)
export const adoptLayout = (c: LayoutChange) => http.post<ExtraDirsReply>('/library/layout/adopt', c)
export const dismissLayout = (lib: string, dir: string, undo = false) => http.post<{ ok: boolean }>('/library/layout/dismiss', { lib, dir, undo })

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
