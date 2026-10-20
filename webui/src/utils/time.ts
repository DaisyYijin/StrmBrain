/** 完整时间（悬浮提示里用） */
export function fullTime(s?: string | null) {
  if (!s) return ''
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return s
  return d.toLocaleString('zh-CN', { hour12: false })
}

/** 列表里用相对时间，精确时间放在悬浮提示里 */
export function relTime(s?: string | null) {
  if (!s) return ''
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return s
  const diff = (Date.now() - d.getTime()) / 1000
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  const hm = d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  if (diff < 2 * 86400) return `昨天 ${hm}`
  const md = `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, '0')}`
  return d.getFullYear() === new Date().getFullYear() ? `${md} ${hm}` : `${d.getFullYear()}-${md}`
}

/**
 * cron 预览给的是「01-02 15:04」（routes.go 的 /sync/cron-preview，没有年份），挑个短写法：
 * 今天 / 明天 HH:mm，更远的原样给。跨年时补出来的日期会落在过去，按明年算
 */
export function relCronNext(s: string): string {
  const m = /^(\d{2})-(\d{2}) (\d{2}):(\d{2})$/.exec(s.trim())
  if (!m) return s
  const now = new Date()
  const d = new Date(now.getFullYear(), +m[1] - 1, +m[2], +m[3], +m[4])
  if (d.getTime() < now.getTime() - 86_400_000) d.setFullYear(d.getFullYear() + 1)
  const hm = `${m[3]}:${m[4]}`
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const diff = Math.round((day(d) - day(new Date())) / 86_400_000)
  if (diff === 0) return `今天 ${hm}`
  if (diff === 1) return `明天 ${hm}`
  return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, '0')} ${hm}`
}
