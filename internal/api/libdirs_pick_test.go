package api

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"115-station/internal/model"
)

// 媒体库目录表：整理选目录（pickDir）与分派开关。用例编号对应 LIBRARY-DIRS-PLAN.md §6

// resetClassifyRules 老分类规则的 10 秒缓存（换一套规则时清掉）
func resetClassifyRules() {
	classifyRuleMu.Lock()
	classifyRuleCache = map[string][]model.CategoryRule{}
	classifyRuleAt = map[string]time.Time{}
	classifyRuleMu.Unlock()
}

// setLibraryDirsActive 测试里切开关（写 setting 并清缓存）
func setLibraryDirsActive(t *testing.T, on bool) {
	t.Helper()
	model.DB.Where("key = ?", libraryDirsActiveKey).Delete(&model.Setting{})
	if on {
		model.DB.Create(&model.Setting{Key: libraryDirsActiveKey, Value: "1"})
	}
	forgetLibraryDirs()
	t.Cleanup(forgetLibraryDirs)
}

// g1Corpus 一批形态各异的条目：地区、语种、类型、年份边界、正则、空字段
func g1Corpus() []*TmdbMedia {
	m := func(mt, title, orig, lang, year string, genres []int, origin, prod []string) *TmdbMedia {
		return &TmdbMedia{MediaType: mt, Title: title, OriginalTitle: orig, OrigLanguage: lang, Year: year,
			GenreIDs: genres, OrigCountry: origin, ProdCountry: prod}
	}
	return []*TmdbMedia{
		m("movie", "流浪地球", "流浪地球", "zh", "2019", []int{878}, nil, []string{"CN"}),
		m("movie", "哪吒之魔童降世", "哪吒之魔童降世", "zh", "2019", []int{16}, nil, []string{"CN"}),
		m("movie", "千与千寻", "千と千尋の神隠し", "ja", "2001", []int{16, 14}, nil, []string{"JP"}),
		m("movie", "阿凡达", "Avatar", "en", "2009", []int{28, 878}, nil, []string{"US", "GB"}),
		m("movie", "无间道", "無間道", "cn", "2002", []int{80}, nil, []string{"HK"}),
		m("movie", "寄生虫", "기생충", "ko", "2019", []int{35, 18}, nil, []string{"KR"}),
		m("movie", "徒手攀岩", "Free Solo", "en", "2018", []int{99}, nil, []string{"US"}),
		m("movie", "老电影", "Old", "en", "1985", []int{18}, nil, []string{"US"}),
		m("movie", "什么都没有", "", "", "", nil, nil, nil),
		m("tv", "繁花", "繁花", "zh", "2023", []int{18}, []string{"CN"}, nil),
		m("tv", "葬送的芙莉莲", "葬送のフリーレン", "ja", "2023", []int{16, 10765}, []string{"JP"}, nil),
		m("tv", "孤独摇滚", "ぼっち・ざ・ろっく！", "ja", "2022", []int{16, 35}, []string{"JP"}, nil),
		m("tv", "绝命毒师", "Breaking Bad", "en", "2008", []int{18, 80}, []string{"US"}, nil),
		m("tv", "奔跑吧", "奔跑吧", "zh", "2014", []int{10764}, []string{"CN"}, nil),
		m("tv", "地球脉动", "Planet Earth", "en", "2006", []int{99}, []string{"GB"}, nil),
		m("tv", "小猪佩奇", "Peppa Pig", "en", "2004", []int{16, 10762}, []string{"GB"}, nil),
		m("tv", "请回答1988", "응답하라 1988", "ko", "2015", []int{18, 35}, []string{"KR"}, nil),
		m("tv", "空剧", "", "", "", nil, nil, nil),
	}
}

// g1RuleSets 默认规则 + 几种典型写法（维护者同意用这些代替现网规则）
func g1RuleSets(t *testing.T) map[string][]model.CategoryRule {
	seedCategoryRules(t)
	if err := model.InitDefaultCategories(model.DB); err != nil {
		t.Fatal(err)
	}
	var defaults []model.CategoryRule
	model.DB.Find(&defaults)
	if len(defaults) < 5 {
		t.Fatalf("默认规则没写进来: %d", len(defaults))
	}
	return map[string][]model.CategoryRule{
		"默认规则": defaults,
		"平铺 + 附加库 + 正则 + 排除 + 年份": {
			{ID: 1, MediaType: "tv", Name: "动漫番剧", GenreIds: "16", Library: "动漫库", Priority: 1},
			{ID: 2, MediaType: "tv", Name: "综艺", GenreIds: "10764,10767", Priority: 2},
			{ID: 3, MediaType: "tv", Name: "纪录片", GenreIds: "99", Priority: 3},
			{ID: 4, MediaType: "tv", Name: "国产剧", OriginCountry: "CN,TW,HK", Priority: 4},
			{ID: 5, MediaType: "tv", Name: "外国剧", OriginCountry: "!CN,!TW,!HK", Priority: 5},
			{ID: 6, MediaType: "tv", Name: "剧集", IsDefault: true, Priority: 6},
			{ID: 7, MediaType: "movie", Name: "电影/漫威", CustomRegex: "复仇者|钢铁侠|Avatar", Priority: 1},
			{ID: 8, MediaType: "movie", Name: "电影/动画", GenreIds: "16", Library: "200", Priority: 2},
			{ID: 9, MediaType: "movie", Name: "电影/老片", ReleaseYear: "1900-1999", Priority: 3},
			{ID: 10, MediaType: "movie", Name: "光盘", Ext: "iso", Priority: 4},
			{ID: 11, MediaType: "movie", Name: "电影/华语", OriginalLanguage: "zh,cn", ProductionCountries: "CN,HK,TW", Priority: 5},
			{ID: 12, MediaType: "movie", Name: "", IsDefault: true, Priority: 9}, // 空名兜底：落「电影」
		},
		"只有剧集规则、没有兜底": {
			{ID: 1, MediaType: "tv", Name: "日番", OriginCountry: "JP", GenreIds: "16", Priority: 1},
			{ID: 2, MediaType: "tv", Name: "其他剧", Unsupported: "foo", Priority: 2}, // 认不出的条件：永不匹配
		},
		"多个兜底、兜底带条件、库认不出": {
			{ID: 1, MediaType: "movie", Name: "电影/韩国", ProductionCountries: "KR", IsDefault: true, Priority: 1},
			{ID: 2, MediaType: "movie", Name: "电影/其他", IsDefault: true, Priority: 2},
			{ID: 3, MediaType: "tv", Name: "剧集/日韩", OriginCountry: "JP,KR", Library: "早就删了的库", Priority: 1},
			{ID: 4, MediaType: "tv", Name: "剧集/其他", IsDefault: true, Priority: 2},
		},
		"空规则表": nil,
	}
}

// G1：整理去处逐条等价——老的「分类规则 + library + 来源定库」与迁移后的目录表 pickDir
func TestPickDirEquivalence(t *testing.T) {
	ledgerTestDB(t, nil, nil)
	sets := g1RuleSets(t)
	corpus := g1Corpus()
	bound := []string{"200", "100"} // 两个库都被转存子目录绑定过
	for name, rules := range sets {
		seedCategoryRules(t, rules...)
		resetClassifyRules()
		mig, err := planLibraryDirMigration(rules, nil, testRoots, nil, bound)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if chk := validateLibraryDirs(mig.Rows, bound); len(chk.Errors) != 0 {
			t.Errorf("%s: 迁移结果应能通过保存校验: %v", name, chk.Errors)
		}
		for _, media := range corpus {
			for _, src := range []string{"", "100", "200"} {
				oldCat, val := classifyTarget(media)
				oldLib, oldErr := src, error(nil)
				if src == "" {
					oldLib, oldErr = pickRuleLib(val, testRoots)
				}
				row, newErr := pickDir(mig.Rows, media, src)
				tag := fmt.Sprintf("%s / %s %s / 来源=%q", name, media.MediaType, media.Title, src)
				if (oldErr != nil) != (newErr != nil) {
					t.Errorf("%s: 报错不一致 老=%v 新=%v", tag, oldErr, newErr)
					continue
				}
				// 比实际落进的目录：老规则名为空时分类记 ""、用时由 categoryDir 退回「电影」，迁移直接写成「电影」
				if eff := categoryDir(media.MediaType, oldCat); eff != row.Path {
					t.Errorf("%s: 分类目录 老=%q 新=%q", tag, eff, row.Path)
				}
				if oldErr == nil && oldLib != row.LibCid {
					t.Errorf("%s: 库 老=%q 新=%q", tag, oldLib, row.LibCid)
				}
			}
		}
	}
	resetClassifyRules()
}

// T1：来源定库只在那个库里挑；那个库没有可用行报错
func TestPickDirSource(t *testing.T) {
	rows := []model.LibraryDir{
		{Priority: 1, LibCid: "100", LibName: "影视", Path: "动画", MediaType: "movie", Organize: true, GenreIds: "16"},
		{Priority: 2, LibCid: "100", LibName: "影视", Path: "电影", MediaType: "movie", Organize: true, Fallback: true},
		{Priority: 3, LibCid: "200", LibName: "动漫库", Path: "剧场版", MediaType: "movie", Organize: true, Fallback: true},
	}
	anime := &TmdbMedia{MediaType: "movie", GenreIDs: []int{16}}
	if r, err := pickDir(rows, anime, ""); err != nil || r.Path != "动画" {
		t.Errorf("不看来源: %+v %v", r, err)
	}
	if r, err := pickDir(rows, anime, "200"); err != nil || r.Path != "剧场版" || r.LibCid != "200" {
		t.Errorf("来源定库只看那个库: %+v %v", r, err)
	}
	if _, err := pickDir(rows, anime, "300"); err == nil || !strings.Contains(err.Error(), "来源绑定") {
		t.Errorf("那个库没有行应报错: %v", err)
	}
	if _, err := pickDir(rows, &TmdbMedia{MediaType: "tv"}, ""); err == nil {
		t.Error("剧集一行都没有应报错")
	}
}

// T2 / T3：兜底行先按自己的条件参与；all 类型对两种都生效；仅识别的行不参与整理
func TestPickDirFallbackAndAll(t *testing.T) {
	rows := []model.LibraryDir{
		{Priority: 1, LibCid: "1", LibName: "L", Path: "收藏", MediaType: "all"}, // 仅识别
		{Priority: 2, LibCid: "1", LibName: "L", Path: "韩国", MediaType: "movie", Organize: true, Fallback: true, ProductionCountries: "KR"},
		{Priority: 3, LibCid: "1", LibName: "L", Path: "日本", MediaType: "all", Organize: true, OriginCountry: "JP"},
		{Priority: 4, LibCid: "1", LibName: "L", Path: "法国", MediaType: "movie", Organize: true, Fallback: true, ProductionCountries: "FR"},
		{Priority: 5, LibCid: "1", LibName: "L", Path: "其他", MediaType: "movie", Organize: true}, // 没条件：第一轮就命中
	}
	cases := []struct {
		m    *TmdbMedia
		want string
	}{
		{&TmdbMedia{MediaType: "movie", ProdCountry: []string{"KR"}}, "韩国"},
		{&TmdbMedia{MediaType: "movie", ProdCountry: []string{"FR"}}, "法国"}, // 兜底行先按自己的条件参与
		{&TmdbMedia{MediaType: "movie", ProdCountry: []string{"US"}}, "其他"}, // 没写条件的行命中一切（老规则同款）
		{&TmdbMedia{MediaType: "tv", OrigCountry: []string{"JP"}}, "日本"},    // all 对剧集也生效
	}
	for _, c := range cases {
		if r, err := pickDir(rows, c.m, ""); err != nil || r.Path != c.want {
			t.Errorf("%+v → %+v %v，预期 %s", c.m, r, err, c.want)
		}
	}
	if _, err := pickDir(rows, &TmdbMedia{MediaType: "tv", OrigCountry: []string{"US"}}, ""); err == nil {
		t.Error("剧集没有兜底应报错（不落表外目录）")
	}
	// 条件都不中：第一个兜底（不是最后一个）
	noCatchAll := rows[:4]
	if r, err := pickDir(noCatchAll, &TmdbMedia{MediaType: "movie", ProdCountry: []string{"US"}}, ""); err != nil || r.Path != "韩国" {
		t.Errorf("都没中取第一个兜底: %+v %v", r, err)
	}
}

// T4 / T5：库根行；以及不变量——pickDir 选中的目录，整理进去的片目一定认得出
func TestPickDirLibRootAndRecognizable(t *testing.T) {
	ledgerTestDB(t, nil, nil)
	rows := []model.LibraryDir{
		{Priority: 1, LibCid: "200", LibName: "动漫库", Path: "", MediaType: "tv", Organize: true, GenreIds: "16"},
		{Priority: 2, LibCid: "100", LibName: "影视", Path: "剧集", MediaType: "tv", Organize: true, Fallback: true},
		{Priority: 3, LibCid: "100", LibName: "影视", Path: "电影/外语", MediaType: "movie", Organize: true, Fallback: true},
	}
	r, err := pickDir(rows, &TmdbMedia{MediaType: "tv", GenreIDs: []int{16}}, "")
	if err != nil || r.Path != "" || r.LibCid != "200" {
		t.Fatalf("库根行: %+v %v", r, err)
	}
	if got := categoryDir("tv", ""); got != "剧集" {
		t.Errorf("没启用时分类为空仍退回媒体类型目录: %q", got)
	}
	setLibraryDirsActive(t, true)
	if got := categoryDir("tv", ""); got != "" {
		t.Errorf("启用后分类为空就是库根: %q", got)
	}

	layout := compileLibraryDirs(rows, libDirLibNames(rows))
	for _, m := range g1Corpus() {
		row, err := pickDir(rows, m, "")
		if err != nil {
			t.Fatalf("%s: %v", m.Title, err)
		}
		titleDir := libSubPath(row.LibName, row.Path, m.Title+" ("+m.Year+")")
		h, ok := layout.titleAt(titleDir + "/S01E01.strm")
		if !ok || h.Key != titleDir {
			t.Errorf("%s 整理进 %s 后认不出: %+v %v", m.Title, titleDir, h, ok)
		}
	}
}

// 分派：开关打开后识别、整理、只要路径的几处都走目录表
func TestLibraryDirsDispatch(t *testing.T) {
	ledgerTestDB(t, []model.CategoryRule{{MediaType: "movie", Name: "电影/老规则", IsDefault: true}, {MediaType: "tv", Name: "剧集/老规则", IsDefault: true}}, nil)
	resetClassifyRules()
	t.Cleanup(resetClassifyRules)
	model.DB.Create(&[]model.LibraryDir{
		{Priority: 1, LibCid: "100", LibName: "影视", Path: "新目录/电影", MediaType: "movie", Organize: true, Fallback: true},
		{Priority: 2, LibCid: "100", LibName: "影视", Path: "新目录/剧集", MediaType: "tv", Organize: true, Fallback: true},
	})
	movie := &TmdbMedia{MediaType: "movie"}
	cfg := &OrgConfig{Library: "100"}

	setLibraryDirsActive(t, false)
	if c := classifyMedia(movie); c != "电影/老规则" {
		t.Errorf("没启用走老规则: %q", c)
	}
	if c, cid, err := pickOrgTarget(cfg, nil, movie, ""); err != nil || c != "电影/老规则" || cid != "100" {
		t.Errorf("没启用: %q %q %v", c, cid, err)
	}
	if _, ok := loadLibCategoryLayout().titleAt("影视/新目录/电影/x/x.strm"); ok {
		t.Error("没启用时识别不该用目录表")
	}

	setLibraryDirsActive(t, true)
	if c := classifyMedia(movie); c != "新目录/电影" {
		t.Errorf("启用后走目录表: %q", c)
	}
	if c, cid, err := pickOrgTarget(cfg, nil, movie, ""); err != nil || c != "新目录/电影" || cid != "100" {
		t.Errorf("启用后: %q %q %v", c, cid, err)
	}
	if _, ok := loadLibCategoryLayout().titleAt("影视/新目录/电影/x/x.strm"); !ok {
		t.Error("启用后识别用目录表")
	}
	if _, ok := loadLibCategoryLayout().titleAt("影视/电影/老规则/x/x.strm"); ok {
		t.Error("启用后老规则的目录不再算分类")
	}
}

// T9：重新整理按来源定的记录留在原库，并只在原库的行里挑；其余按目录表
func TestRedoTargetLibraryDirs(t *testing.T) {
	ledgerTestDB(t, nil, nil)
	model.DB.Create(&[]model.LibraryDir{
		{Priority: 1, LibCid: "100", LibName: "影视", Path: "电影", MediaType: "movie", Organize: true, Fallback: true},
		{Priority: 2, LibCid: "200", LibName: "动漫库", Path: "电影", MediaType: "movie", Organize: true, Fallback: true},
	})
	setLibraryDirsActive(t, true)
	cfg := &OrgConfig{Library: "100", OtherLibs: []string{"200"}}
	movie := &TmdbMedia{MediaType: "movie"}

	c, cid, bySource, err := redoTarget(cfg, nil, &model.OrganizeRecord{LibBySource: true, LibCid: "200", TargetDir: "电影/x"}, movie)
	if err != nil || cid != "200" || !bySource || c != "电影" {
		t.Errorf("按来源定的留原库: %q %q %v %v", c, cid, bySource, err)
	}
	c, cid, bySource, err = redoTarget(cfg, nil, &model.OrganizeRecord{TargetDir: "电影/x"}, movie)
	if err != nil || cid != "100" || bySource {
		t.Errorf("其余按目录表: %q %q %v %v", c, cid, bySource, err)
	}
}

// T6：保存校验
func TestValidateLibraryDirs(t *testing.T) {
	ok := []model.LibraryDir{
		{Priority: 1, LibCid: "100", LibName: "影视", Path: "电影", MediaType: "movie", Organize: true, Fallback: true},
		{Priority: 2, LibCid: "100", LibName: "影视", Path: "剧集", MediaType: "tv", Organize: true, Fallback: true},
	}
	if chk := validateLibraryDirs(ok, nil); len(chk.Errors)+len(chk.Warnings) != 0 {
		t.Errorf("正常表: %+v", chk)
	}
	// 缺剧集兜底
	if chk := validateLibraryDirs(ok[:1], nil); len(chk.Errors) != 1 || !strings.Contains(chk.Errors[0], "剧集") {
		t.Errorf("缺兜底: %+v", chk)
	}
	// 来源绑定的库没有自己的兜底
	if chk := validateLibraryDirs(ok, []string{"200"}); len(chk.Errors) != 2 {
		t.Errorf("绑定库缺兜底: %+v", chk)
	}
	// 兜底所在的库认不出：不算
	bad := append([]model.LibraryDir(nil), ok...)
	bad[0].LibName = ""
	if chk := validateLibraryDirs(bad, nil); len(chk.Errors) != 1 || len(chk.Warnings) != 1 {
		t.Errorf("库认不出的兜底: %+v", chk)
	}
	// 完全相同的两行、参与整理没选库、没写条件吞掉后面的行
	rows := append(append([]model.LibraryDir(nil), ok...),
		model.LibraryDir{Priority: 3, LibCid: "100", LibName: "影视", Path: "剧集", MediaType: "tv", Organize: true, Fallback: true},
		model.LibraryDir{Priority: 0, LibCid: "", Path: "全吞", MediaType: "all", Organize: true},
	)
	chk := validateLibraryDirs(rows, nil)
	joined := strings.Join(chk.Errors, "；")
	if !strings.Contains(joined, "完全相同") || !strings.Contains(joined, "没选媒体库") {
		t.Errorf("错误: %v", chk.Errors)
	}
	if len(chk.Warnings) != 1 || !strings.Contains(chk.Warnings[0], "全吞") {
		t.Errorf("提示: %v", chk.Warnings)
	}
}
