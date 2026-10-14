package api

import (
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"

	"115-station/internal/config"
	"115-station/internal/model"
)

// 媒体库布局（liblayout.go）：限定库的条目、库根分类、新旧存储格式。
// 用例编号对应 docs/115-station-notes/LIBRARY-LAYOUT-PLAN.md §5

// T8（纯函数部分）：库根本身是分类目录时，片目就是库根下一层；库名照样带回来
func TestTitleAtLibRoot(t *testing.T) {
	l := buildLibCategoryLayout([]model.CategoryRule{{MediaType: "movie", Name: "电影"}, {MediaType: "tv", Name: "剧集"}}).
		withExtraDirs([]libExtraDir{{Lib: "电影库", Dir: "", Type: "movie"}})
	cases := []struct {
		rel string
		hit layoutHit
		ok  bool
	}{
		{"电影库/流浪地球 (2019)/流浪地球.strm", layoutHit{Key: "电影库/流浪地球 (2019)", TitleDir: "流浪地球 (2019)", Category: "", MediaType: "movie", Lib: "电影库"}, true},
		// 库根下直接放的视频：平铺
		{"电影库/无间道.2002.strm", layoutHit{Key: "电影库/无间道.2002.strm", TitleDir: "无间道.2002", Category: "", MediaType: "movie", Lib: "电影库"}, true},
		// 库根下另有规则分类：更深的分类优先
		{"电影库/电影/阿凡达 (2009)/阿凡达.strm", layoutHit{Key: "电影库/电影/阿凡达 (2009)", TitleDir: "阿凡达 (2009)", Category: "电影", MediaType: "movie", Lib: "电影库"}, true},
		// 别的库不受影响：库根不是分类
		{"影视/流浪地球 (2019)/流浪地球.strm", layoutHit{}, false},
		{"影视/电影/阿凡达 (2009)/阿凡达.strm", layoutHit{Key: "影视/电影/阿凡达 (2009)", TitleDir: "阿凡达 (2009)", Category: "电影", MediaType: "movie", Lib: "影视"}, true},
		// 库根下的文件不是片目（库根下至少要隔一层目录或是 STRM）
		{"电影库/说明.txt", layoutHit{}, false},
	}
	for _, c := range cases {
		got, ok := l.titleAt(c.rel)
		if ok != c.ok || got != c.hit {
			t.Errorf("titleAt(%s) = %+v %v，预期 %+v %v", c.rel, got, ok, c.hit, c.ok)
		}
	}
}

// T12：两个库下同名目录，只登记给 A 库的不影响 B 库
func TestTitleAtLibScoped(t *testing.T) {
	l := buildLibCategoryLayout(nil).withExtraDirs([]libExtraDir{
		{Lib: "A", Dir: "收藏", Type: "movie"},
		{Dir: "纪录片", Type: "auto"}, // 不限定库：两个库都认
	})
	if h, ok := l.titleAt("A/收藏/某片/某片.strm"); !ok || h.Key != "A/收藏/某片" || h.Lib != "A" {
		t.Errorf("A 库应认出: %+v %v", h, ok)
	}
	if _, ok := l.titleAt("B/收藏/某片/某片.strm"); ok {
		t.Error("B 库的同名目录不该认")
	}
	for _, rel := range []string{"A/纪录片/地球/E01.strm", "B/纪录片/地球/E01.strm"} {
		if _, ok := l.titleAt(rel); !ok {
			t.Errorf("不限定库的目录两个库都该认: %s", rel)
		}
	}
	// 老台账不带库名：限定库的条目用不上
	if _, ok := l.titleAt("收藏/某片/某片.strm"); ok {
		t.Error("不带库名的路径不该命中限定库的条目")
	}
}

// 规则为准：限定库的手动条目不能盖过同名规则的类型；不限定 + 限定都有时限定的优先
func TestWithExtraDirsPriority(t *testing.T) {
	l := buildLibCategoryLayout([]model.CategoryRule{{MediaType: "movie", Name: "电影"}}).withExtraDirs([]libExtraDir{
		{Lib: "A", Dir: "电影", Type: "tv"},
		{Dir: "动画", Type: "tv"},
		{Lib: "A", Dir: "动画", Type: "movie"},
	})
	if e, _ := l.lookup("A", "电影"); e.Type != "movie" || e.Source != layoutSrcRule {
		t.Errorf("规则为准: %+v", e)
	}
	if e, _ := l.lookup("A", "动画"); e.Type != "movie" || e.Source != layoutSrcManual {
		t.Errorf("A 库限定的优先: %+v", e)
	}
	if e, _ := l.lookup("B", "动画"); e.Type != "tv" {
		t.Errorf("B 库用不限定的: %+v", e)
	}
	if _, ok := l.lookup("", ""); ok {
		t.Error("不带库名时库根永远不是分类")
	}
}

// T16：网盘文件页，库根分类下的片目能整理 / 移动，库根本身与库根分类的上级不行
func TestIsTitleRelInLibRoot(t *testing.T) {
	l := buildLibCategoryLayout([]model.CategoryRule{{MediaType: "movie", Name: "电影/华语"}}).
		withExtraDirs([]libExtraDir{{Lib: "电影库", Dir: "", Type: "movie"}})
	for _, c := range []struct {
		lib, rel string
		want     bool
	}{
		{"电影库", "流浪地球 (2019)", true},
		{"电影库", "", false},
		{"电影库", "电影", false},      // 是 电影/华语 的上级
		{"电影库", "电影/华语", false},   // 是分类
		{"电影库", "电影/华语/某片", true}, // 规则分类照旧
		{"电影库", "流浪地球 (2019)/Season 01", false},
		{"影视", "流浪地球 (2019)", false}, // 别的库库根不是分类
	} {
		if got := l.isTitleRelIn(c.lib, c.rel); got != c.want {
			t.Errorf("isTitleRelIn(%q, %q) = %v，预期 %v", c.lib, c.rel, got, c.want)
		}
	}

	roles := map[string]string{"100": "library", "300": "redundant"}
	lib := []browseCrumb{{"100", "电影库"}}
	a := rowActionsOf(lib, roles, l, fileEntry{ID: "101", Name: "流浪地球 (2019)", IsDir: true})
	if !a.Title || !a.Organize || !a.Move || a.TitleKey != "电影库/流浪地球 (2019)" || a.TitleRel != "流浪地球 (2019)" {
		t.Errorf("库根下的片目: %+v", a)
	}
	other := []browseCrumb{{"100", "影视"}}
	if a := rowActionsOf(other, roles, l, fileEntry{ID: "101", Name: "流浪地球 (2019)", IsDir: true}); a.Title {
		t.Errorf("别的库库根下不是片目: %+v", a)
	}
}

// T14 / T15：存储格式前后兼容
func TestLibExtraDirCompat(t *testing.T) {
	// 老数据：没有 lib / source，原样读出，来源记作手动、对所有库生效
	var old []libExtraDir
	if err := json.Unmarshal([]byte(`[{"dir":"LGNB电影","type":"movie"}]`), &old); err != nil {
		t.Fatal(err)
	}
	l := buildLibCategoryLayout(nil).withExtraDirs(old)
	if e, ok := l.lookup("任意库", "LGNB电影"); !ok || e.Source != layoutSrcManual || e.Type != "movie" {
		t.Errorf("老数据: %+v %v", e, ok)
	}

	// 新数据给老版本：老版本的结构只有 dir / type。库根条目被丢掉，限定库的退化成不限定，都不报错
	raw, _ := json.Marshal([]libExtraDir{{Lib: "电影库", Dir: "", Type: "movie"}, {Lib: "A", Dir: "收藏", Type: "movie"}})
	var legacy []struct {
		Dir  string `json:"dir"`
		Type string `json:"type"`
	}
	if err := json.Unmarshal(raw, &legacy); err != nil {
		t.Fatal(err)
	}
	stripped := make([]libExtraDir, 0, len(legacy))
	for _, d := range legacy {
		stripped = append(stripped, libExtraDir{Dir: d.Dir, Type: d.Type})
	}
	l = buildLibCategoryLayout(nil).withExtraDirs(stripped)
	if _, ok := l.titleAt("电影库/某片/某片.strm"); ok {
		t.Error("老版本不该认库根分类")
	}
	if _, ok := l.titleAt("B/收藏/某片/某片.strm"); !ok {
		t.Error("老版本把限定库的条目当成对所有库生效")
	}
}

func TestNormExtraDirsLibScoped(t *testing.T) {
	rules := layoutOf(map[string]string{"电影": "movie"})
	out, skipped, err := normExtraDirs([]libExtraDir{
		{Lib: " 电影库 ", Dir: "", Type: "movie"},         // 库根
		{Lib: "电影库", Dir: "/", Type: "tv"},             // 同一个：类型覆盖
		{Lib: "电影库", Dir: "电影库/收藏", Type: "movie"},     // 限定库时不剥库名（库内真有同名子目录）
		{Lib: "电影库", Dir: "电影", Type: "movie"},         // 和规则同名
		{Dir: "电影库/收藏", Type: "movie", Source: "emby"}, // 不限定：剥库名，和上面限定的不是同一条
	}, rules, []string{"电影库"}, []string{"电影库", "影视"})
	if err != nil {
		t.Fatal(err)
	}
	want := []libExtraDir{
		{Lib: "电影库", Dir: "", Type: "tv"},
		{Lib: "电影库", Dir: "电影库/收藏", Type: "movie"},
		{Dir: "收藏", Type: "movie", Source: "emby"},
	}
	if len(out) != len(want) {
		t.Fatalf("out = %+v", out)
	}
	for i := range want {
		if out[i] != want[i] {
			t.Errorf("out[%d] = %+v，预期 %+v", i, out[i], want[i])
		}
	}
	if len(skipped) != 1 {
		t.Errorf("skipped = %v", skipped)
	}
	for _, bad := range []libExtraDir{
		{Lib: "没这个库", Dir: "x"},
		{Lib: "a/b", Dir: "x"},
		{Lib: "..", Dir: "x"},
		{Dir: ""}, // 不限定库时仍不许是库根
	} {
		if _, _, err := normExtraDirs([]libExtraDir{bad}, rules, nil, []string{"电影库"}); err == nil {
			t.Errorf("%+v 应拒收", bad)
		}
	}
}

// T8（端到端）：库根分类的片目 LibName 对、刮削的网盘落点不带库名
func TestLibRootLedgerAndCloudRel(t *testing.T) {
	newTestDB(t, "liblayout.db")
	notifyConfigSource = &config.Config{DataDir: t.TempDir(), ConfigDir: t.TempDir()}
	t.Cleanup(func() { notifyConfigSource = nil; forgetLibLayoutCaches() })
	h := &Handler{DB: model.DB, Config: notifyConfigSource}
	seedCategoryRules(t, model.CategoryRule{MediaType: "movie", Name: "电影", IsDefault: true})
	for _, rel := range []string{
		"电影库/流浪地球 (2019) {tmdbid=535167}/流浪地球.strm",
		"电影库/无间道.2002.strm",
		"影视/电影/阿凡达 (2009)/阿凡达.strm",
	} {
		model.DB.Create(&model.SyncedFile{FileID: rel, RelPath: rel, Kind: "video", PickCode: "pc" + rel})
	}
	forgetLibLayoutCaches()
	if n := len(scanLedgerTitlesCached()); n != 1 {
		t.Fatalf("登记前只认规则里的 1 部, got %d", n)
	}
	if _, _, err := h.addLibExtraDirs([]libExtraDir{{Lib: "电影库", Dir: "", Type: "movie"}}); err != nil {
		t.Fatal(err)
	}
	got := scanLedgerTitlesCached()
	if len(got) != 3 {
		t.Fatalf("应认出 3 部, got %d: %v", len(got), got)
	}
	e := got["电影库/流浪地球 (2019) {tmdbid=535167}"]
	if e == nil || e.LibName != "电影库" || e.Category != "" || e.TmdbID != 535167 {
		t.Fatalf("库根下的片目: %+v", e)
	}
	if s := got["电影库/无间道.2002.strm"]; s == nil || !s.solo() || s.LibName != "电影库" {
		t.Errorf("库根下平铺: %+v", s)
	}

	root := t.TempDir()
	st, err := ledgerScrapeTitle(nil, e, root, "999", nil, false)
	if err != nil {
		t.Fatal(err)
	}
	if st.Dir.CloudBase != "999" || st.Dir.CloudRel != "流浪地球 (2019) {tmdbid=535167}" {
		t.Errorf("网盘落点应是库根下的片目目录: %+v", st.Dir)
	}
	if !strings.HasSuffix(filepath.ToSlash(st.Dir.Local), "/电影库/流浪地球 (2019) {tmdbid=535167}") {
		t.Errorf("本地落点: %s", st.Dir.Local)
	}
	if len(st.Videos) != 1 || st.Videos[0].Dir.CloudRel != "流浪地球 (2019) {tmdbid=535167}" {
		t.Errorf("视频落点: %+v", st.Videos)
	}
}

// 残留判定：限定库的条目，库名本身算认得
func TestKnownTopsLibScoped(t *testing.T) {
	l := layoutOf(map[string]string{"电影/华语": "movie"}).withExtraDirs([]libExtraDir{{Lib: "电影库", Dir: ""}})
	got := findLibLeftovers(map[string][2]int64{"电影库": {1, 1}, "电影": {1, 1}, "旧库": {1, 1}}, nil, l)
	if len(got) != 1 || got[0].Name != "旧库" {
		t.Errorf("got %+v", got)
	}
}

// 布局总表：按作用范围计片目数；规则条目的两种提示；台账里没片的规则目录排到末尾
func TestBuildLayoutRows(t *testing.T) {
	l := buildLibCategoryLayout([]model.CategoryRule{
		{MediaType: "movie", Name: "电影"}, {MediaType: "tv", Name: "剧集"}, {MediaType: "movie", Name: "纪录片"},
	}).withExtraDirs([]libExtraDir{
		{Dir: "港片", Type: "movie", At: "2026-10-14T10:00:00+08:00"},
		{Lib: "B", Dir: "港片", Type: "movie", Source: "emby"},
		{Lib: "电影库", Dir: "", Type: "auto"},
	})
	titles := map[string]*ledgerTitleEntry{
		"A/电影/x":     {LibName: "A", Category: "电影"},
		"A/剧集/y":     {LibName: "A", Category: "剧集"},
		"A/港片/z":     {LibName: "A", Category: "港片"},
		"B/港片/w":     {LibName: "B", Category: "港片"},
		"B/港片/v":     {LibName: "B", Category: "港片"},
		"电影库/u":      {LibName: "电影库", Category: ""},
		"老台账/电影/old": {LibName: "", Category: "电影"},
	}
	rows := buildLayoutRows(l, []libExtraDir{{Dir: "港片", At: "2026-10-14T10:00:00+08:00"}}, titles, []string{"剧集/y/Season 01"})
	got := map[string]layoutRow{}
	for _, r := range rows {
		got[layoutCatKey(r.Lib, r.Dir)] = r
	}
	if r := got[layoutCatKey("", "电影")]; r.Titles != 2 || r.Hint != "never_organized" || r.Source != layoutSrcRule {
		t.Errorf("电影: %+v", r)
	}
	if r := got[layoutCatKey("", "剧集")]; r.Titles != 1 || r.Hint != "" {
		t.Errorf("剧集（整理进来过）: %+v", r)
	}
	if r := got[layoutCatKey("", "纪录片")]; r.Titles != 0 || r.Hint != "empty" {
		t.Errorf("纪录片（没片）: %+v", r)
	}
	if r := got[layoutCatKey("", "港片")]; r.Titles != 1 || r.Source != layoutSrcManual || r.At == "" || r.Hint != "" {
		t.Errorf("不限定的港片只算 A 库那部（B 库的归限定条目）: %+v", r)
	}
	if r := got[layoutCatKey("B", "港片")]; r.Titles != 2 || r.Source != "emby" {
		t.Errorf("B 库限定: %+v", r)
	}
	if r := got[layoutCatKey("电影库", "")]; r.Titles != 1 || r.Type != "auto" {
		t.Errorf("库根: %+v", r)
	}
	if rows[len(rows)-1].Dir != "纪录片" {
		t.Errorf("没片的规则目录应在末尾: %+v", rows)
	}
}
