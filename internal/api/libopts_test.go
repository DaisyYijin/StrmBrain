package api

import (
	"strings"
	"testing"
)

// 自己整理的库按声明的层级认片目，不套分类表；本站整理的库照旧
func TestSelfLibTitleOf(t *testing.T) {
	layout := catLayout(map[string]string{"电影": "movie", "剧集": "tv"})
	layout.self = map[string]int{"老库": 2, "平铺库": 1, "深库": 3}
	cases := []struct {
		rel                    string
		key, title, cat, media string
		ok                     bool
	}{
		// 库/分类/片目/文件
		{"老库/欧美电影/阿凡达 (2009)/阿凡达.strm", "老库/欧美电影/阿凡达 (2009)", "阿凡达 (2009)", "欧美电影", "", true},
		{"老库/美剧/绝命毒师/Season 1/绝命毒师 S01E01.strm", "老库/美剧/绝命毒师", "绝命毒师", "美剧", "", true},
		// 视频正好在片目层：平铺电影一部；带集号的不算
		{"老库/欧美电影/敦刻尔克 (2017).strm", "老库/欧美电影/敦刻尔克 (2017).strm", "敦刻尔克 (2017)", "欧美电影", "movie", true},
		{"老库/日剧/非自然死亡 E01.strm", "", "", "", "", false},
		// 比片目层还浅
		{"老库/散落.strm", "", "", "", "", false},
		// 「电影」不是分类表里的那个电影：自己整理的库不套分类表
		{"老库/电影/沙丘 (2021)/沙丘.strm", "老库/电影/沙丘 (2021)", "沙丘 (2021)", "电影", "", true},
		{"平铺库/沙丘 (2021)/沙丘.strm", "平铺库/沙丘 (2021)", "沙丘 (2021)", "", "", true},
		{"平铺库/沙丘 (2021).strm", "平铺库/沙丘 (2021).strm", "沙丘 (2021)", "", "movie", true},
		{"深库/纪录片/BBC/地球脉动 (2006)/x.strm", "深库/纪录片/BBC/地球脉动 (2006)", "地球脉动 (2006)", "纪录片/BBC", "", true},
		// 本站整理的库照旧按分类认
		{"影视/电影/沙丘 (2021)/沙丘.strm", "影视/电影/沙丘 (2021)", "沙丘 (2021)", "电影", "movie", true},
	}
	for _, c := range cases {
		key, title, cat, mt, ok := layout.titleOf(c.rel)
		if ok != c.ok || key != c.key || title != c.title || cat != c.cat || mt != c.media {
			t.Errorf("%s → (%q,%q,%q,%q,%v)，应为 (%q,%q,%q,%q,%v)", c.rel, key, title, cat, mt, ok, c.key, c.title, c.cat, c.media, c.ok)
		}
	}
}

func TestPreviewSelfTitles(t *testing.T) {
	rels := []string{
		"老库/欧美电影/阿凡达 (2009)/阿凡达.strm",
		"老库/美剧/绝命毒师/Season 1/绝命毒师 S01E01.strm",
		"老库/美剧/绝命毒师/Season 2/绝命毒师 S02E01.strm",
		"老库/纪录片/BBC/地球脉动 (2006)/地球脉动 S01E01.strm",
		"老库/纪录片/BBC/蓝色星球 (2001)/蓝色星球 S01E01.strm",
		"老库/散落.strm",
	}
	p := previewSelfTitles("老库", 2, rels)
	if p.Total != 3 || p.Loose != 1 || p.Odd != 1 {
		t.Fatalf("层级 2：%+v", p)
	}
	if p.Titles[0].Title != "BBC" || !strings.Contains(p.Titles[0].Odd, "选浅") {
		t.Errorf("BBC 下面还有带年份的子目录，应标出来并排第一：%+v", p.Titles[0])
	}
	for _, x := range p.Titles {
		if x.Title == "绝命毒师" && (x.MediaType != "tv" || x.Videos != 2) {
			t.Errorf("带集号的按剧集：%+v", x)
		}
	}
	// 层级选深了：季目录被当成片目
	deep := previewSelfTitles("老库", 3, rels)
	odd := 0
	for _, x := range deep.Titles {
		if strings.Contains(x.Odd, "选深") {
			odd++
		}
	}
	if odd != 2 {
		t.Errorf("Season 1 / Season 2 应标「选深了」：%+v", deep.Titles)
	}
}

// 分类规则、转存入口都不能指向自己整理的库
func TestPickRuleLibSelf(t *testing.T) {
	libs := []libRoot{{Cid: "100", Name: "影视"}, {Cid: "200", Name: "老库", Self: true}}
	for _, v := range []string{"老库", "200"} {
		if _, err := pickRuleLib(v, libs); err == nil || !strings.Contains(err.Error(), "自己整理") {
			t.Errorf("library: %s 应报自己整理的库：%v", v, err)
		}
	}
	if cid, err := pickRuleLib("", libs); err != nil || cid != "100" {
		t.Errorf("没写 library 照旧进默认库：%s %v", cid, err)
	}
	if err := inboxSelfLibErr([]stagingLib{{Cid: "s", Name: "老库", Lib: "200"}}, map[string]bool{"200": true}); err == nil {
		t.Error("转存入口绑到自己整理的库应拒绝")
	}
	cfg := &OrgConfig{Library: "100", OtherLibs: []string{"200"}, SelfLibs: map[string]bool{"200": true}}
	if recordLibOK(cfg, "200") {
		t.Error("自己整理的库里的记录不能重新整理")
	}
	cfg.SubLibs = map[string]stagingLib{"s": {Cid: "s", Name: "老库", Lib: "200"}}
	cfg.Pending = "s"
	if _, bound, err := sourceLibOf(cfg); !bound || err == nil {
		t.Error("绑到自己整理的库的入口要报错停下，不退回规则")
	}
}

func TestPlanLibrariesSaveSelf(t *testing.T) {
	deps, _ := fakeDeps(t)
	self := map[string]libOpt{"200": {Mode: libModeSelf, Depth: 2}}

	// 动漫改成自己整理：指向它的规则回默认库、它的入口不能带
	plan, _, err := planLibrariesSave(planInput(), libSaveReq{ExtraLibs: keepAll(), Opts: self}, deps)
	if err != nil {
		t.Fatal(err)
	}
	if len(plan.ToSelf) != 1 || plan.ToSelf[0].Cid != "200" || !plan.YAMLChanged || !plan.OptsChanged {
		t.Fatalf("改成自己整理：%+v", plan)
	}
	for _, r := range parseOrFail(t, plan.YAML) {
		if r.Library != "" {
			t.Errorf("指向它的 library 应删掉：%+v", r)
		}
	}
	if o := parseLibOpts(plan.OptsJSON); !o.of("200").self() || o.of("200").depth() != 2 {
		t.Errorf("lib_opts：%s", plan.OptsJSON)
	}

	// 已经是自己整理的：再存不动 YAML，也不算改动
	in := planInput()
	in.OptsRaw = plan.OptsJSON
	again, _, err := planLibrariesSave(in, libSaveReq{ExtraLibs: keepAll(), Opts: self}, deps)
	if err != nil || len(again.ToSelf) != 0 || again.OptsChanged {
		t.Errorf("没改：%+v %v", again, err)
	}
	// 老客户端不带 opts：沿用
	keep, _, _ := planLibrariesSave(in, libSaveReq{ExtraLibs: keepAll()}, deps)
	if keep.OptsChanged {
		t.Errorf("不带 opts 应沿用：%s", keep.OptsJSON)
	}
	// 改回本站整理
	back, _, _ := planLibrariesSave(in, libSaveReq{ExtraLibs: keepAll(), Opts: map[string]libOpt{}}, deps)
	if !back.OptsChanged || back.OptsJSON != "{}" {
		t.Errorf("改回本站整理：%s", back.OptsJSON)
	}
	// 移除库时它的选项一起丢
	rm, _, _ := planLibrariesSave(in, libSaveReq{ExtraLibs: []libRootEntry{{Cid: "300", Path: "/4K"}}, Opts: self}, deps)
	if rm.OptsJSON != "{}" {
		t.Errorf("移除的库不该留在 lib_opts 里：%s", rm.OptsJSON)
	}

	bad := []struct {
		name string
		req  libSaveReq
	}{
		{"首个媒体库改成自己整理", libSaveReq{ExtraLibs: keepAll(), Opts: map[string]libOpt{"100": {Mode: libModeSelf, Depth: 2}}}},
		{"没选层级", libSaveReq{ExtraLibs: keepAll(), Opts: map[string]libOpt{"200": {Mode: libModeSelf}}}},
		{"层级越界", libSaveReq{ExtraLibs: keepAll(), Opts: map[string]libOpt{"200": {Mode: libModeSelf, Depth: 4}}}},
		{"不认识的方式", libSaveReq{ExtraLibs: keepAll(), Opts: map[string]libOpt{"200": {Mode: "x"}}}},
		{"入口指向自己整理的库", libSaveReq{ExtraLibs: keepAll(), Opts: self, Inboxes: []libInboxReq{{Name: "新", Lib: "200"}}}},
	}
	for _, b := range bad {
		if _, _, err := planLibrariesSave(planInput(), b.req, deps); err == nil {
			t.Errorf("%s：应拒绝", b.name)
		}
	}
}

func TestRowActionsSelfLib(t *testing.T) {
	roles := map[string]string{"100": "library", "200": "library"}
	layout := catLayout(map[string]string{"电影": "movie"})
	layout.self = map[string]int{"老库": 2}
	chain := []browseCrumb{{"200", "老库"}, {"201", "欧美电影"}}
	a := rowActionsOf(chain, roles, layout, fileEntry{ID: "9", Name: "阿凡达 (2009)", IsDir: true})
	if a.Organize || a.Move || !strings.Contains(a.Block, "自己整理") {
		t.Errorf("自己整理的库里只能看：%+v", a)
	}
	// 本站整理的库不受影响
	b := rowActionsOf([]browseCrumb{{"100", "影视"}, {"101", "电影"}}, roles, layout, fileEntry{ID: "9", Name: "沙丘 (2021)", IsDir: true})
	if !b.Organize || !b.Move {
		t.Errorf("本站整理的库片目照旧能整理：%+v", b)
	}
}

// 没设过刮削的库按老的全局开关换算，升级后行为不变
func TestLegacyLibScrape(t *testing.T) {
	cases := []struct {
		c    scrapeCfg
		meta bool
		want string
	}{
		{scrapeCfg{Provider: "emby", AutoAfterOrganize: true}, true, libScrapeOff},
		{scrapeCfg{AutoAfterOrganize: true}, false, libScrapeAuto},
		{scrapeCfg{AutoAfterSync: true}, false, libScrapeAuto},
		{scrapeCfg{}, true, libScrapeAuto}, // 靠定时补全兜底的
		{scrapeCfg{}, false, libScrapeManual},
	}
	for _, c := range cases {
		if got := legacyLibScrape(c.c, c.meta); got != c.want {
			t.Errorf("%+v meta=%v → %s，应为 %s", c.c, c.meta, got, c.want)
		}
	}
}

func TestLibScrapePolicy(t *testing.T) {
	src := []libNameSource{{Cid: "100", Shown: "/影视"}, {Cid: "200", Shown: "/动漫"}, {Cid: "300", Shown: "/老库"}}
	opts := libOpts{Libs: map[string]libOpt{"200": {Scrape: libScrapeManual}, "300": {Mode: libModeSelf, Depth: 2, Scrape: libScrapeOff}}}
	p := buildLibScrapePolicy(src, opts, libScrapeAuto)
	cases := map[string]string{
		"影视/电影/沙丘 (2021)":    libScrapeAuto, // 没设过：按老开关
		"动漫/动漫番剧/芙莉莲":        libScrapeManual,
		"老库/欧美电影/阿凡达 (2009)": libScrapeOff,
		"电影/沙丘 (2021)":       libScrapeAuto, // 老台账不带库名：按首个库
	}
	for k, want := range cases {
		if got := p.of(k); got != want {
			t.Errorf("%s → %s，应为 %s", k, got, want)
		}
	}
	if !p.stationScrapes("动漫/x") || p.autoScrapes("动漫/x") || p.stationScrapes("老库/x") || !p.embyGrades("老库/x") {
		t.Error("手动的库能手动刮、不自动刮；不刮的库都不能刮、按 Emby 分级")
	}
	if !p.any(libScrapeOff) || !p.any(libScrapeAuto) {
		t.Error("any")
	}
	if off := p.offLibsOf([]string{"影视/a", "老库/b", "老库/c"}); len(off) != 1 || off[0] != "老库" {
		t.Errorf("offLibsOf：%v", off)
	}
	// 零值策略（测试与没加载的地方）：都按自动
	var zero libScrapePolicy
	if !zero.autoScrapes("任何/东西") {
		t.Error("零值策略应按自动")
	}
}

func TestPlanLibrariesSaveScrape(t *testing.T) {
	deps, _ := fakeDeps(t)
	plan, _, err := planLibrariesSave(planInput(), libSaveReq{ExtraLibs: keepAll(), Opts: map[string]libOpt{
		"100": {Scrape: libScrapeAuto}, "200": {Scrape: libScrapeOff}, "300": {Mode: "organize", Scrape: libScrapeManual},
	}}, deps)
	if err != nil {
		t.Fatal(err)
	}
	o := parseLibOpts(plan.OptsJSON)
	if o.of("100").Scrape != libScrapeAuto || o.of("200").Scrape != libScrapeOff || o.of("300").Scrape != libScrapeManual || o.of("300").self() {
		t.Errorf("每个库的刮削都要存下：%s", plan.OptsJSON)
	}
	if len(plan.ToSelf) != 0 || plan.YAMLChanged {
		t.Error("只改刮削不碰分类")
	}
	if _, _, err := planLibrariesSave(planInput(), libSaveReq{ExtraLibs: keepAll(), Opts: map[string]libOpt{"200": {Scrape: "sometimes"}}}, deps); err == nil {
		t.Error("不认识的刮削方式应拒绝")
	}
}

func TestEmbyFolderScrapeModes(t *testing.T) {
	p := libScrapePolicy{byName: map[string]string{"影视": libScrapeAuto, "老库": libScrapeOff}, primary: libScrapeAuto}
	got := func(locs ...string) string { return strings.Join(embyFolderScrapeModes("", locs, "/media", p), ",") }
	if s := got("/media/老库"); s != "off" {
		t.Errorf("只覆盖老库：%s", s)
	}
	if s := got("/media/影视/电影", "/media/老库/欧美电影"); s != "auto,off" {
		t.Errorf("两个库各一部分：%s", s)
	}
	if s := got("/media"); s != "auto,off" {
		t.Errorf("本地根 = 全部库：%s", s)
	}
	if s := got("/elsewhere"); s != "" {
		t.Errorf("不在本地根下：%s", s)
	}
	// 路径映射：Emby 看到的是 /mnt/emby
	if s := strings.Join(embyFolderScrapeModes("/media#/mnt/emby", []string{"/mnt/emby/老库"}, "/media", p), ","); s != "off" {
		t.Errorf("按路径映射换回本地：%s", s)
	}
}
