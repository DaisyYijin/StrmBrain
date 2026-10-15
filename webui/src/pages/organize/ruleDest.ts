/**
 * 二级分类大纲上每条规则的落点（库名 + 分类目录，多库时才显示）。
 *
 * 认库的口径必须和整理时一模一样（后端 libroots.go 的 pickRuleLib）：
 * 没写 library = 默认库；先按 cid 认，再按库目录名认（去掉首尾斜杠）；都认不出 = 整理到这一类会报错停下。
 * 两边不一致的话，大纲上说进 A 库、整理却进了 B 库，比不显示还糟。
 */
import type { OutlineGroup } from './ruleLint'
import { parseCategory } from './ruleLint'

export interface DestLib {
  cid: string
  name: string
  default: boolean
}

export type RuleDest = NonNullable<OutlineGroup['items'][number]['dest']>

const unquote = (v: string) => v.trim().replace(/^(['"])(.*)\1$/, '$2').trim()

/** library 的值 → 落进哪个库；null = 认不出 */
export function libOfValue(raw: string | undefined, libs: DestLib[]): DestLib | null {
  const v = unquote(raw ?? '')
  if (!v) return libs.find((l) => l.default) ?? libs[0] ?? null
  const byCid = libs.find((l) => l.cid === v)
  if (byCid) return byCid
  const name = v.replace(/^\/+|\/+$/g, '')
  return libs.find((l) => l.name !== '' && l.name === name) ?? null
}

/** 一条规则的落点标签 */
export function destOf(raw: string | undefined, libs: DestLib[]): RuleDest {
  const lib = libOfValue(raw, libs)
  if (lib) return { text: lib.name || `cid ${lib.cid}`, tone: lib.default ? 'default' : 'set' }
  return {
    text: `认不出「${unquote(raw ?? '')}」`,
    tone: 'bad',
    hint: '没有这个媒体库（移除了，或在 115 上改了名），整理到这一类时会停下',
  }
}

/** 给大纲的每条规则配上落点。groups 与 parseCategory 的 sections / rules 一一对应（outlineCategory 就是这么生成的） */
export function withDest(groups: OutlineGroup[], src: string, libs: DestLib[]): OutlineGroup[] {
  const sections = parseCategory(src).sections
  return groups.map((g, gi) => ({
    ...g,
    items: g.items.map((it, ii) => ({
      ...it,
      // 原来那个「→ X 库」小标签不要了，免得说两遍
      tags: it.tags.filter((t) => !t.startsWith('→ ')),
      dest: destOf(sections[gi]?.rules[ii]?.library?.value, libs),
    })),
  }))
}
