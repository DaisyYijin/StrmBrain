package api

// 测试用：按「库内目录 → 类型」写一张只含对所有库生效条目的分类目录表（来源记作规则）
func layoutOf(m map[string]string) libCategoryLayout {
	l := newLibLayout()
	for dir, typ := range m {
		l.all[dir] = layoutEntry{Type: typ, Source: layoutSrcRule}
	}
	return l
}

// isTitleRel 测试用：不限定库地判断片目目录（老用例都是单库、不带库名的写法）
func (l libCategoryLayout) isTitleRel(rel string) bool { return l.isTitleRelIn("", rel) }
