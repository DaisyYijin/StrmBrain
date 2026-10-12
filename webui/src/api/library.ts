import { http } from './client'
import type { QueuedReply } from './tasks'

/**
 * 其他媒体库目录：只用于识别片目（海报墙、网盘文件页、对账），整理分类不读它，见后端 extradirs.go。
 * dir 是库内相对路径、不带库名，写法同分类规则的名字。
 */
export type ExtraDirType = 'movie' | 'tv' | 'auto'
export interface ExtraDir {
  dir: string
  type: ExtraDirType
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
