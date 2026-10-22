package api

// 订阅挑资源的「谁更好」（2026-10-22，设计见 docs/115-station-notes/SUB-WASH-PLAN.md）。
//
// 和整理时洗版同一套（washCompare）：先比命中洗版策略 priority_level 的第几条，同一条再按默认画质顺序
// 分辨率 → 片源 → HDR → 视频编码。订阅和整理必须判得一样（维护者定的）：订阅觉得更好、整理觉得打平，
// 转进来的就被移进「已存在」。用户没配洗版策略（或策略里没写 priority_level）时只有默认画质顺序这一层。
//
// 「达标」= 命中第 1 条规则（维护者定的：最好的画面设置要求）。没有规则就没有达标一说。

// subPriority 一个订阅的资源优先级。Rules 为空 = 只按默认画质顺序
type subPriority struct {
	Rules []washRule
}

// subPriorityOf 按类型取洗版策略（订阅还不知道会进哪个分类，同 resWashRank）
func subPriorityOf(mediaType string) subPriority {
	if st := washStrategyForType(mediaType); st != nil {
		return subPriority{Rules: st.PriorityLevel}
	}
	return subPriority{}
}

// top 达标：命中第 1 条规则
func (p subPriority) top(name string) bool {
	return len(p.Rules) > 0 && washTier(name, p.Rules) == 0
}

// compare >0 a 更好，<0 b 更好，0 分不出
func (p subPriority) compare(a, b string) int {
	c, _ := washCompare(a, b, p.Rules)
	return c
}
