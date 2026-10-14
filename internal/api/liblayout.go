package api

import (
	"path"
	"sort"
	"strings"

	"115-station/internal/model"
)

// ==================== 媒体库布局：哪些目录是分类目录，片目在哪一层 ====================
//
// 设计见 docs/115-station-notes/LIBRARY-LAYOUT-PLAN.md（2026-10-14）。
//
// 整理落盘是 库名/<分类名>/<标题目录>/…（organize.go 的 categoryDir + 重命名模板第一段），
// 分类名就是二级分类 YAML 里写的名字，可以多级（电视剧/日番），也可以平铺（动漫番剧、综艺）。
// 所以「哪一层是标题目录」只能按登记过的分类目录算，不能按深度写死 ——
// 此前写死成 库名/电影|剧集/分类/标题，平铺配置（电影/*、动漫番剧/*）下
// 片目全认错：电影的标题目录被当成分类、文件被当成标题目录，动漫番剧 / 综艺整类被跳过。
//
// 分类目录有几个来源，合成这一张表：分类规则（整理往里放片的地方）+ 其他媒体库目录（extradirs.go，
// 用户手动登记、只用于识别，整理不往里放）。哪里都没登记的目录，里面的片不算片目（与网盘文件页口径一致）。
// 别改成按目录结构自动推断：平铺与多级分类混在一起时认不准。要推断也只能拿来出建议，用户确认后再登记。
//
// 条目有两种作用范围：对所有库生效（分类规则、老的其他媒体库目录），以及只对某一个库生效
// （Lib 非空）。只对某个库生效的条目可以是库根本身（Dir 为空）：115 的库目录直接放片目
// （电影库/片名/…）时，库根就是分类目录。同一个目录两种都有时，限定库的优先（更具体），
// 但分类规则永远为准：withExtraDirs 不让任何手动条目盖过规则。

// 条目来源（只用于展示与冲突判定，不影响 titleAt）
const (
	layoutSrcRule   = "rule"
	layoutSrcManual = "manual"
)

// layoutEntry 一个分类目录
type layoutEntry struct {
	Type   string // movie / tv；两边都有同名分类、或其他目录设为自动时为空（按片目里有没有集号判断）
	Source string
}

// libCategoryLayout 编译好的分类目录表。用 newLibLayout 建，零值只能读
type libCategoryLayout struct {
	all  map[string]layoutEntry            // 库内相对路径 → 条目，对所有库生效
	libs map[string]map[string]layoutEntry // 库名 → 库内相对路径（"" = 库根）→ 条目
	// legacy 只服务老台账路径：不带库名的那一轮，以及第一段不是任何已配置库的路径（老布局里规则对任何第一段都生效）。
	// 媒体库目录表（libdirs.go）的行全按库，老台账的第一段不是库名、按库查不到，所以把各行的路径另编一份放这里；
	// 第一段是已配置的库（libs 里有这个键，哪怕一行都没有）时永远不查它，库与库之间不串
	legacy map[string]layoutEntry
}

func newLibLayout() libCategoryLayout {
	return libCategoryLayout{all: map[string]layoutEntry{}, libs: map[string]map[string]layoutEntry{}, legacy: map[string]layoutEntry{}}
}

// lookup 库 lib 里的目录 dir 是不是分类目录。lib 为空（老台账不带库名）只看对所有库生效的条目
func (l libCategoryLayout) lookup(lib, dir string) (layoutEntry, bool) {
	if lib != "" {
		if e, ok := l.libs[lib][dir]; ok {
			return e, true
		}
	}
	if dir == "" {
		return layoutEntry{}, false
	}
	if e, ok := l.all[dir]; ok {
		return e, ok
	}
	if _, configured := l.libs[lib]; configured {
		return layoutEntry{}, false
	}
	e, ok := l.legacy[dir]
	return e, ok
}

// typeOf 分类目录的媒体类型（不是分类目录时为空）
func (l libCategoryLayout) typeOf(lib, dir string) string {
	e, _ := l.lookup(lib, dir)
	return e.Type
}

// knownTops 布局认得的库内第一段与库名：残留判定（libleft.go）据此不把它们算成移出配置的库
func (l libCategoryLayout) knownTops() map[string]bool {
	out := map[string]bool{}
	for dir := range l.all {
		top, _, _ := strings.Cut(dir, "/")
		out[top] = true
	}
	for lib := range l.libs {
		out[lib] = true
	}
	for dir := range l.legacy {
		top, _, _ := strings.Cut(dir, "/")
		out[top] = true
	}
	return out
}

// loadLibCategoryLayout 按当前分类规则与其他媒体库目录算出所有分类目录。
// 直接读表不走 classifyRules 的缓存：调用方（台账扫描）自己有 30 秒缓存
func loadLibCategoryLayout() libCategoryLayout {
	return loadRuleLayout().withExtraDirs(loadLibExtraDirs())
}

// loadRuleLayout 只按分类规则算的分类目录表（不含其他媒体库目录）
func loadRuleLayout() libCategoryLayout {
	var rules []model.CategoryRule
	if model.DB != nil {
		model.DB.Where("media_type IN ?", []string{"movie", "tv"}).Find(&rules)
	}
	return buildLibCategoryLayout(rules)
}

// buildLibCategoryLayout 纯函数部分：规则 → 分类目录表。
// 某个媒体类型一条规则都没有时，整理会落到 电影/未分类 或 剧集/未分类（classifyMedia 的兜底）
func buildLibCategoryLayout(rules []model.CategoryRule) libCategoryLayout {
	out := newLibLayout()
	has := map[string]bool{}
	add := func(dir, mediaType string) {
		if dir = libSubPath(dir); dir == "" {
			return
		}
		if prev, ok := out.all[dir]; ok && prev.Type != mediaType {
			out.all[dir] = layoutEntry{Source: layoutSrcRule} // movie 与 tv 都有这个分类名：类型要按文件判断
			return
		}
		out.all[dir] = layoutEntry{Type: mediaType, Source: layoutSrcRule}
	}
	for _, r := range rules {
		add(r.Name, r.MediaType)
		has[r.MediaType] = true
	}
	for _, mt := range []string{"movie", "tv"} {
		if !has[mt] {
			add(libSubPath(mediaTypeCategory(mt), "未分类"), mt)
		}
	}
	return out
}

// layoutHit 台账路径落在哪个片目
type layoutHit struct {
	Key       string // 片目 key（台账口径，含库名前缀）；平铺片目是 STRM 的相对路径
	TitleDir  string // 标题目录名；平铺片目是去掉 .strm 的视频名
	Category  string // 分类目录（库内相对路径）；库根分类为空
	MediaType string
	Lib       string // 带库名那一轮匹配上时的库名；老台账不带库名的为空
}

// titleAt 台账相对路径（含文件名）落在哪个标题目录下。
// 路径首段一般是库名；老台账有不带库名的，先按带库名算，对不上再按不带算。
// 分类有多级且互相嵌套时（电视剧 与 电视剧/日番 都是分类）取最长匹配；库根分类最短。
//
// 视频直接放在电影分类目录里（平铺，见 isSoloTitleKey）时，这个视频自己就是一部：
// key 是它的相对路径，titleDir 是去掉 .strm 的视频名
func (l libCategoryLayout) titleAt(relPath string) (layoutHit, bool) {
	segs := strings.Split(strings.Trim(relPath, "/"), "/")
	last := segs[len(segs)-1]
	for _, off := range []int{1, 0} {
		lib := ""
		if off == 1 {
			if len(segs) < 2 {
				continue
			}
			lib = segs[0]
		}
		// 平铺：分类段一直到文件的上一层，比任何「分类/标题目录」都长，先看它。
		// 分类段为空（n == 0）只可能命中限定库的库根条目：视频直接放在库根下
		if n := len(segs) - 1 - off; n >= 0 && isSoloTitleKey(last) {
			cat := strings.Join(segs[off:off+n], "/")
			if e, hit := l.lookup(lib, cat); hit && soloAllowed(e.Type, last) {
				return layoutHit{Key: strings.Join(segs, "/"), TitleDir: soloTitleStem(last), Category: cat, MediaType: "movie", Lib: lib}, true
			}
		}
		best := -1
		// 标题目录之下至少还有一个文件：分类段最多到 len-2
		for n := 0; off+n <= len(segs)-2; n++ {
			if _, hit := l.lookup(lib, strings.Join(segs[off:off+n], "/")); hit {
				best = n
			}
		}
		if best < 0 {
			continue
		}
		cat := strings.Join(segs[off:off+best], "/")
		return layoutHit{
			Key: strings.Join(segs[:off+best+1], "/"), TitleDir: segs[off+best],
			Category: cat, MediaType: l.typeOf(lib, cat), Lib: lib,
		}, true
	}
	return layoutHit{}, false
}

// titleOf 同 titleAt，拆成几个返回值（多数调用方只要 key）
func (l libCategoryLayout) titleOf(relPath string) (key, titleDir, category, mediaType string, ok bool) {
	h, ok := l.titleAt(relPath)
	return h.Key, h.TitleDir, h.Category, h.MediaType, ok
}

// isCategoryOrAncestorIn 库 lib 里的这个相对路径是分类目录，或是某个多级分类的上级（电视剧 之于 电视剧/日番）
func (l libCategoryLayout) isCategoryOrAncestorIn(lib, rel string) bool {
	if _, ok := l.lookup(lib, rel); ok {
		return true
	}
	for c := range l.all {
		if strings.HasPrefix(c, rel+"/") {
			return true
		}
	}
	for c := range l.libs[lib] {
		if strings.HasPrefix(c, rel+"/") {
			return true
		}
	}
	return false
}

// layoutParent 库内相对路径的上一层；在库根下时为空
func layoutParent(rel string) string {
	if p := path.Dir(rel); p != "." {
		return p
	}
	return ""
}

// isTitleRelIn 库 lib 里的相对路径（不含库名）是不是片目目录：父目录是分类（可以是库根分类），
// 自己不是分类也不是分类的上级
func (l libCategoryLayout) isTitleRelIn(lib, rel string) bool {
	rel = strings.Trim(rel, "/")
	if rel == "" {
		return false
	}
	if _, ok := l.lookup(lib, layoutParent(rel)); !ok {
		return false
	}
	return !l.isCategoryOrAncestorIn(lib, rel)
}

// ledgerTopNames 台账里实际出现过的第一段（本地媒体树的库目录名，老台账里也可能是分类名）。
// 只用来校验限定库的条目有没有写错库名；不能拿来剥目录前缀（老台账的第一段可能就是分类）
func ledgerTopNames() []string {
	counts, err := ledgerTopCounts()
	if err != nil {
		return nil
	}
	out := make([]string, 0, len(counts))
	for top := range counts {
		out = append(out, top)
	}
	sort.Strings(out)
	return out
}
