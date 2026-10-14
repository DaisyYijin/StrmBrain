package api

import (
	"strings"
	"testing"

	"115-station/internal/model"
)

// 媒体库目录表（libdirs.go）。用例编号对应 docs/115-station-notes/LIBRARY-DIRS-PLAN.md §6

var testRoots = []libRoot{{Cid: "100", Name: "影视"}, {Cid: "200", Name: "动漫库"}}

// G2：同一份台账，老布局（分类规则 + 其他媒体库目录）与迁移后的目录表认出的片目逐条相同
func TestLibraryDirsRecognitionEquivalence(t *testing.T) {
	rules := []model.CategoryRule{
		{ID: 1, MediaType: "movie", Name: "电影/华语电影", OriginalLanguage: "zh", Priority: 2},
		{ID: 2, MediaType: "movie", Name: "电影/动画电影", GenreIds: "16", Priority: 1},
		{ID: 3, MediaType: "movie", Name: "电影/外语电影", IsDefault: true, Priority: 3},
		{ID: 4, MediaType: "tv", Name: "动漫", GenreIds: "16", Library: "动漫库", Priority: 1}, // 整理进附加库
		{ID: 5, MediaType: "tv", Name: "电视剧/日番", OriginCountry: "JP", Priority: 2},
		{ID: 6, MediaType: "tv", Name: "电视剧", IsDefault: true, Priority: 3},
		{ID: 7, MediaType: "tv", Name: "纪录片", GenreIds: "99", Priority: 4},
		{ID: 8, MediaType: "movie", Name: "纪录片", GenreIds: "99", Priority: 4}, // 电影剧集同名分类
		{ID: 9, MediaType: "tv", Name: "旧库专区", Library: "早就删了的库", Priority: 5},
	}
	extras := []libExtraDir{
		{Dir: "LGNB电影/香港", Type: "movie"},     // 不限定库，只有主库有片
		{Dir: "收藏", Type: "auto"},             // 两个库都有片
		{Dir: "还没片的目录", Type: "movie"},        // 哪个库都没片
		{Lib: "动漫库", Dir: "", Type: "tv"},     // 阶段 1 起的库根条目
		{Lib: "影视", Dir: "自建/港剧", Type: "tv"}, // 阶段 1 起的限定库条目
		{Dir: "电视剧/日番", Type: "movie"},        // 和规则同名：规则为准
	}
	videos := []string{
		"影视/电影/华语电影/流浪地球 (2019)/流浪地球.strm",
		"影视/电影/动画电影/哪吒 (2019)/哪吒.strm",
		"影视/电影/外语电影/阿凡达.2009.strm", // 平铺
		"影视/电视剧/繁花 (2023)/Season 01/E01.strm",
		"影视/电视剧/日番/孤独摇滚 (2022)/S01E01.strm",
		"影视/纪录片/地球脉动 (2006)/S01E01.strm", // 同名分类：按集号认剧集
		"影视/纪录片/徒手攀岩 (2018)/徒手攀岩.strm",
		"影视/LGNB电影/香港/无间道 (2002)/无间道.strm",
		"影视/LGNB电影/香港/英雄本色.1986.strm",
		"影视/收藏/某片 (2001)/某片.strm",
		"影视/自建/港剧/大时代 (1992)/E01.strm",
		"影视/动漫/在主库里的老番 (2000)/E01.strm", // 规则 library=动漫库，但主库里也有这个目录
		"影视/旧库专区/某剧 (2010)/E01.strm",    // 规则的库认不出：老布局照样认
		"影视/随手放的/某片/某片.strm",            // 两边都不认
		"动漫库/动漫/葬送的芙莉莲 (2023)/S01E01.strm",
		"动漫库/收藏/另一部 (2005)/E01.strm",
		"动漫库/某番 (2015)/E01.strm", // 库根分类
		"动漫库/电影/华语电影/放错库的片 (2020)/x.strm",
		// 老台账：第一段不是库名
		"电视剧/日番/老番 (1999)/E01.strm",
		"剧集/动漫/更老的番 (1998)/E01.strm",
		"电影/华语电影/老片 (1990)/老片.strm",
	}
	old := buildLibCategoryLayout(rules).withExtraDirs(extras)
	mig, err := planLibraryDirMigration(rules, extras, testRoots, videos)
	if err != nil {
		t.Fatal(err)
	}
	neu := compileLibraryDirs(mig.Rows, []string{"影视", "动漫库"})
	recognized := 0
	for _, v := range videos {
		o, okO := old.titleAt(v)
		n, okN := neu.titleAt(v)
		if okO != okN || o != n {
			t.Errorf("%s\n  老: %+v %v\n  新: %+v %v", v, o, okO, n, okN)
		}
		if okO {
			recognized++
		}
	}
	// 夹具里只有「随手放的」那一个认不出：防止两边都认不出也算「相同」
	if recognized != len(videos)-1 {
		t.Errorf("老布局应认出 %d 个，实得 %d：夹具没起作用", len(videos)-1, recognized)
	}
	if len(mig.Notes) != 1 || !strings.Contains(mig.Notes[0], "早就删了的库") {
		t.Errorf("认不出的库要说明: %v", mig.Notes)
	}
}

// T7：迁移规则
func TestPlanLibraryDirMigration(t *testing.T) {
	rules := []model.CategoryRule{
		{ID: 3, MediaType: "movie", Name: "电影/B", Priority: 2},
		{ID: 1, MediaType: "movie", Name: "电影/A", Priority: 1},
		{ID: 2, MediaType: "movie", Name: "电影/A2", Priority: 1}, // 同优先级按 id
		{ID: 4, MediaType: "movie", Name: "", IsDefault: true, Priority: 9},
		{ID: 5, MediaType: "movie", Name: "光盘", Ext: "iso", Priority: 5},
		{ID: 6, MediaType: "movie", Name: "动画", GenreIds: "16", Library: "200", Priority: 6}, // 按 cid 指库
	}
	mig, err := planLibraryDirMigration(rules, nil, testRoots, nil)
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, r := range mig.Rows {
		got = append(got, r.MediaType+":"+r.LibName+"/"+r.Path)
	}
	want := []string{
		"movie:影视/电影/A", "movie:影视/电影/A2", "movie:影视/电影/B", "movie:影视/光盘", "movie:动漫库/动画",
		"movie:影视/电影",  // 空名的兜底规则退回媒体类型目录
		"tv:影视/剧集/未分类", // 剧集一条规则都没有：补兜底
	}
	if strings.Join(got, " | ") != strings.Join(want, " | ") {
		t.Fatalf("顺序 / 落点:\n got  %v\n want %v", got, want)
	}
	for i, r := range mig.Rows {
		if r.Priority != i+1 || !r.Organize {
			t.Errorf("第 %d 行 priority / organize: %+v", i, r)
		}
	}
	if r := mig.Rows[3]; r.Unsupported != "ext" {
		t.Errorf("只写了 ext 的规则原来不匹配任何条目，迁移后也不能匹配: %+v", r)
	}
	if r := mig.Rows[4]; r.LibCid != "200" || r.GenreIds != "16" {
		t.Errorf("按 cid 指库、条件原样: %+v", r)
	}
	if r := mig.Rows[5]; !r.Fallback || r.LibCid != "100" {
		t.Errorf("is_default → Fallback: %+v", r)
	}
	if r := mig.Rows[6]; !r.Fallback || r.MediaType != "tv" {
		t.Errorf("补的剧集兜底: %+v", r)
	}

	// 电影有规则但没有兜底：组末补「电影/未分类」（老 classifyTarget 的最后一档）
	mig, _ = planLibraryDirMigration([]model.CategoryRule{{ID: 1, MediaType: "movie", Name: "电影/A", GenreIds: "16"}}, nil, testRoots, nil)
	if r := mig.Rows[1]; r.Path != "电影/未分类" || !r.Fallback || r.MediaType != "movie" {
		t.Errorf("没有兜底规则时补未分类: %+v", mig.Rows)
	}
	// 这一行老布局里不认（有规则时只认规则名），新表认：整理落进去的片从此认得出（设计文档 §4 R3）
	l := compileLibraryDirs(mig.Rows, []string{"影视", "动漫库"})
	if _, ok := l.titleAt("影视/电影/未分类/某片 (2000)/x.strm"); !ok {
		t.Error("兜底目录应能识别")
	}

	// 库名拿不到：不迁移
	if _, err := planLibraryDirMigration(rules, nil, []libRoot{{Cid: "100"}}, nil); err == nil {
		t.Error("库名为空时应拒绝迁移")
	}
	if _, err := planLibraryDirMigration(rules, nil, nil, nil); err == nil {
		t.Error("没配媒体库时应拒绝迁移")
	}
}

// 其他媒体库目录：展开到有片的库；哪都没片落主库；限定库原样；和规则同名的不重复
func TestPlanLibraryDirMigrationExtras(t *testing.T) {
	rules := []model.CategoryRule{{ID: 1, MediaType: "movie", Name: "电影", IsDefault: true}, {ID: 2, MediaType: "tv", Name: "剧集", IsDefault: true}}
	extras := []libExtraDir{
		{Dir: "收藏", Type: "auto"},
		{Dir: "空目录", Type: "movie"},
		{Lib: "动漫库", Dir: "", Type: "tv"},
		{Lib: "没配的库", Dir: "x", Type: "tv"},
		{Dir: "电影", Type: "tv"},
	}
	videos := []string{"影视/收藏/a/a.strm", "动漫库/收藏/b/b.strm"}
	mig, err := planLibraryDirMigration(rules, extras, testRoots, videos)
	if err != nil {
		t.Fatal(err)
	}
	type k struct{ lib, path string }
	got := map[k]model.LibraryDir{}
	for _, r := range mig.Rows {
		if _, dup := got[k{r.LibName, r.Path}]; dup {
			t.Errorf("重复行: %+v", r)
		}
		got[k{r.LibName, r.Path}] = r
	}
	for _, c := range []struct {
		lib, path, mt string
	}{{"影视", "收藏", "all"}, {"动漫库", "收藏", "all"}, {"影视", "空目录", "movie"}, {"动漫库", "", "tv"}, {"没配的库", "x", "tv"}} {
		r, ok := got[k{c.lib, c.path}]
		if !ok || r.MediaType != c.mt || r.Organize {
			t.Errorf("%s/%s: %+v %v", c.lib, c.path, r, ok)
		}
	}
	if r := got[k{"影视", "电影"}]; r.MediaType != "movie" || !r.Organize {
		t.Errorf("和规则同名的其他目录不另起一行，规则为准: %+v", r)
	}
	if r := got[k{"没配的库", "x"}]; r.LibCid != "" {
		t.Errorf("认不出的库没有 cid: %+v", r)
	}
}

// 编译：同一目录几行类型不同 → 空；库名认不出的行不参与识别；配置了的库不查 legacy
func TestCompileLibraryDirs(t *testing.T) {
	rows := []model.LibraryDir{
		{LibName: "影视", Path: "纪录片", MediaType: "movie", Organize: true},
		{LibName: "影视", Path: "纪录片", MediaType: "tv", Organize: true},
		{LibName: "影视", Path: "收藏", MediaType: "all"},
		{LibName: "", LibCid: "坏库", Path: "坏", MediaType: "tv", Organize: true},
		{LibName: "动漫库", Path: "", MediaType: "tv"},
	}
	l := compileLibraryDirs(rows, []string{"影视", "动漫库", "空库"})
	if e, ok := l.lookup("影视", "纪录片"); !ok || e.Type != "" || e.Source != layoutSrcRule {
		t.Errorf("同名两类: %+v %v", e, ok)
	}
	if e, ok := l.lookup("影视", "收藏"); !ok || e.Type != "" || e.Source != layoutSrcManual {
		t.Errorf("all → 空类型、仅识别: %+v %v", e, ok)
	}
	if _, ok := l.titleAt("影视/坏/x/x.strm"); ok {
		t.Error("库认不出的行不参与识别")
	}
	if h, ok := l.titleAt("动漫库/某番/E01.strm"); !ok || h.Key != "动漫库/某番" || h.Lib != "动漫库" {
		t.Errorf("库根: %+v %v", h, ok)
	}
	// 「空库」是配置了的库、一行都没有：不能借 legacy 认出别的库的目录
	if _, ok := l.titleAt("空库/纪录片/x/E01.strm"); ok {
		t.Error("配置了的库不查 legacy")
	}
	// 不是库的第一段（老台账）：查 legacy
	if h, ok := l.titleAt("电视剧/纪录片/x/E01.strm"); !ok || h.Category != "纪录片" {
		t.Errorf("老台账: %+v %v", h, ok)
	}
}
