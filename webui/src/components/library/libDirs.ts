/** 媒体库目录表的显示小工具（卡片与编辑弹窗共用） */
import type { LibraryDir } from '@/api/library'
import { countryName, genreName, languageName } from '@/pages/organize/categoryRef'

export const LIB_DIR_TYPE_LABEL: Record<string, string> = { movie: '电影', tv: '剧集', all: '电影和剧集' }

/** 「库 / 路径」，库根写「库（库根）」 */
export function libDirLabel(r: Pick<LibraryDir, 'lib_name' | 'lib_cid' | 'path'>): string {
  const lib = r.lib_name || `cid ${r.lib_cid}`
  return r.path ? `${lib} / ${r.path}` : `${lib}（库根）`
}

/** 一个条件的取值 → 中文：逗号分隔，`!` 开头是排除 */
function terms(expr: string, name: (v: string) => string): string {
  return expr
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean)
    .map((t) => {
      const neg = t.startsWith('!')
      const v = neg ? t.slice(1).trim() : t
      const n = name(v) || v
      return neg ? `非${n}` : n
    })
    .join('、')
}

/** 条件摘要：一条一个标签。没有条件返回空数组（界面显示「不限」或「兜底」） */
export function libDirConds(r: LibraryDir): string[] {
  const media = r.media_type === 'tv' ? 'tv' : 'movie'
  const out: string[] = []
  if (r.genre_ids) out.push(`类型：${terms(r.genre_ids, (v) => genreName(media, v))}`)
  if (r.origin_country) out.push(`地区：${terms(r.origin_country, countryName)}`)
  if (r.production_countries) out.push(`制片：${terms(r.production_countries, countryName)}`)
  if (r.original_language) out.push(`语言：${terms(r.original_language, languageName)}`)
  if (r.release_year) out.push(`年份：${r.release_year}`)
  if (r.custom_regex) out.push(`片名正则：${r.custom_regex}`)
  if (r.unsupported) out.push(`认不出的条件：${r.unsupported}（这一行永不匹配）`)
  return out
}

/** 新行的默认值 */
export function emptyLibDir(libCid = '', libName = ''): LibraryDir {
  return {
    lib_cid: libCid,
    lib_name: libName,
    path: '',
    media_type: 'movie',
    organize: true,
    fallback: false,
    genre_ids: '',
    original_language: '',
    origin_country: '',
    production_countries: '',
    release_year: '',
    custom_regex: '',
    unsupported: '',
    note: '',
  }
}
