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
 * 媒体库目录表（后端 libdirs*.go，设计见 docs/115-station-notes/LIBRARY-DIRS-PLAN.md）：
 * 一行 = 一个 115 目录 + 适用类型 + 条件 + 是否参与整理 + 是否兜底，顺序即优先级。
 * 识别只看表里有哪些目录（片目在下一层）；整理按顺序找第一个类型与条件都匹配的行。本地 STRM 目录跟随 115 结构、只读。
 */
export type LibDirType = 'movie' | 'tv' | 'all'
export interface LibraryDir {
  id?: number
  priority?: number
  lib_cid: string
  lib_name: string
  /** 库内相对路径，'' = 库根本身 */
  path: string
  cid?: string
  media_type: LibDirType
  organize: boolean
  fallback: boolean
  genre_ids: string
  original_language: string
  origin_country: string
  production_countries: string
  release_year: string
  custom_regex: string
  unsupported: string
  note: string
}
export interface LibraryDirView extends LibraryDir {
  titles: number
  /** 本地 STRM 目录（只读） */
  local: string
  lib_missing: boolean
}
export interface LibDirLib {
  cid: string
  name: string
  /** 115 上的绝对路径（选择器选的目录据此换成库内路径） */
  abs: string
  main: boolean
}
export interface LibDirCheck {
  errors: string[]
  warnings: string[]
}
export interface LibraryDirsReply {
  active: boolean
  /** 没启用的原因（还没配媒体库、115 暂时读不出库名） */
  reason?: string
  rows?: LibraryDirView[]
  libs?: LibDirLib[]
  check?: LibDirCheck
  unclaimed?: { videos: number; dirs: { path: string; videos: number }[]; more_dirs?: number }
}

export const getLibraryDirs = () => http.get<LibraryDirsReply>('/library/dirs', { timeoutMs: 120000 })
export const saveLibraryDirs = (rows: LibraryDir[]) => http.put<{ rows: LibraryDirView[]; check: LibDirCheck }>('/library/dirs', { rows })
/** 追加一行仅识别（lib 为库目录名或 cid）；同库同路径已有时什么都不做 */
export const addLibraryDir = (lib: string, path: string, type: LibDirType) =>
  http.post<{ ok: boolean; added: boolean }>('/library/dirs/add', { lib, path, type })
export const exportLibraryDirs = () => http.get<{ yaml: string }>('/library/dirs/export')
export const importLibraryDirs = (yaml: string) => http.post<{ rows: LibraryDirView[]; notes?: string[] }>('/library/dirs/import', { yaml })

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
