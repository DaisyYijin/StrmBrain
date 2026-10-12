import { http } from './client'

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
