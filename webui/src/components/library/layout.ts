/** 媒体库布局的显示小工具（布局页、确认弹窗、对账共用） */

/** 一条分类目录给人看的写法：「库 / 目录」，库根写成「库（库根）」，不限定库的只写目录 */
export function layoutDirLabel(lib: string, dir: string): string {
  if (!lib) return dir
  return dir ? `${lib} / ${dir}` : `${lib}（库根）`
}

export const LAYOUT_TYPE_LABEL: Record<string, string> = { movie: '电影', tv: '剧集', auto: '自动' }

export const LAYOUT_SOURCE_LABEL: Record<string, string> = {
  rule: '分类规则',
  manual: '手动',
  emby: 'Emby 建议',
  record: '整理记录',
}
