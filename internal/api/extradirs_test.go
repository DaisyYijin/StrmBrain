package api

import (
	"testing"

	"115-station/internal/config"
	"115-station/internal/model"
)

func TestLayoutWithExtraDirs(t *testing.T) {
	rules := []model.CategoryRule{
		{MediaType: "movie", Name: "电影"},
		{MediaType: "tv", Name: "电视剧/日番"},
	}
	l := buildLibCategoryLayout(rules).withExtraDirs([]libExtraDir{
		{Dir: "LGNB电影/LGNB中国香港电影", Type: "movie"},
		{Dir: "纪录片", Type: "auto"},
		{Dir: "电影", Type: "tv"}, // 和规则同名：规则为准
		{Dir: "电影/漫威合集", Type: "movie"},
		{Dir: "电视剧", Type: "tv"}, // 是规则 电视剧/日番 的上级
	})
	want := map[string]string{
		"电影": "movie", "电视剧/日番": "tv", "LGNB电影/LGNB中国香港电影": "movie",
		"纪录片": "", "电影/漫威合集": "movie", "电视剧": "tv",
	}
	for dir, mt := range want {
		if got, ok := l.lookup("", dir); !ok || got.Type != mt {
			t.Errorf("%s: got %q (%v) want %q", dir, got, ok, mt)
		}
	}

	cases := []struct {
		rel, key, cat, mt string
		ok                bool
	}{
		// 手动建的目录：普通片目
		{"115/LGNB电影/LGNB中国香港电影/无间道 (2002)/无间道.strm", "115/LGNB电影/LGNB中国香港电影/无间道 (2002)", "LGNB电影/LGNB中国香港电影", "movie", true},
		// 同一个目录里平铺的电影
		{"115/LGNB电影/LGNB中国香港电影/英雄本色.1986.1080p.strm", "115/LGNB电影/LGNB中国香港电影/英雄本色.1986.1080p.strm", "LGNB电影/LGNB中国香港电影", "movie", true},
		// 自动类型：片目层照认，类型留给 scanLedgerTitles 按集号判
		{"115/纪录片/地球脉动/S01E01.strm", "115/纪录片/地球脉动", "纪录片", "", true},
		// 嵌套在分类里的合集目录：合集目录是分类，下一层才是片目
		{"115/电影/漫威合集/钢铁侠 (2008)/钢铁侠.strm", "115/电影/漫威合集/钢铁侠 (2008)", "电影/漫威合集", "movie", true},
		// 上级是其他目录、下级是规则：规则那层照旧
		{"115/电视剧/日番/葬送的芙莉莲/S01E01.strm", "115/电视剧/日番/葬送的芙莉莲", "电视剧/日番", "tv", true},
		{"115/电视剧/漫长的季节/S01E01.strm", "115/电视剧/漫长的季节", "电视剧", "tv", true},
		// 两处都没有：仍然不认
		{"115/随手放的/某片/某片.strm", "", "", "", false},
	}
	for _, c := range cases {
		key, _, cat, mt, ok := l.titleOf(c.rel)
		if ok != c.ok || key != c.key || cat != c.cat || mt != c.mt {
			t.Errorf("titleOf(%s) = %q %q %q %v", c.rel, key, cat, mt, ok)
		}
	}

	if !l.isTitleRel("LGNB电影/LGNB中国香港电影/无间道 (2002)") {
		t.Error("其他目录下的片目应能整理 / 移动")
	}
	if l.isTitleRel("电视剧/日番") || l.isTitleRel("电影/漫威合集") {
		t.Error("分类目录本身不是片目")
	}
}

func TestNormExtraDirs(t *testing.T) {
	rules := layoutOf(map[string]string{"电影": "movie"})
	out, skipped, err := normExtraDirs([]libExtraDir{
		{Dir: ` /115\LGNB电影/ `, Type: "movie"}, // 带库名、反斜杠、空白
		{Dir: "LGNB电影", Type: "tv"},             // 重复：后来的类型覆盖
		{Dir: "电影", Type: "movie"},              // 已是分类规则
		{Dir: "纪录片", Type: "奇怪"},               // 类型不认识 → auto
		{Dir: "115", Type: "movie"},             // 只有一段时不当库名剥
	}, rules, []string{"115"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(out) != 3 || out[0] != (libExtraDir{Dir: "LGNB电影", Type: "tv"}) || out[1] != (libExtraDir{Dir: "纪录片", Type: "auto"}) || out[2].Dir != "115" {
		t.Errorf("out = %+v", out)
	}
	if len(skipped) != 1 {
		t.Errorf("skipped = %v", skipped)
	}
	for _, bad := range []string{"", "/", "a/../b", "./a"} {
		if _, _, err := normExtraDirs([]libExtraDir{{Dir: bad}}, rules, nil, nil); err == nil {
			t.Errorf("%q 应拒收", bad)
		}
	}
}

func TestCategoryDrops(t *testing.T) {
	oldL := layoutOf(map[string]string{"电影": "movie", "电视剧/日番": "tv", "综艺": ""})
	ledger := map[string]*ledgerTitleEntry{
		"115/电影/A":     {Key: "115/电影/A", Title: "A", Category: "电影"},
		"115/电视剧/日番/B": {Key: "115/电视剧/日番/B", Title: "B", Category: "电视剧/日番"},
		"115/电视剧/日番/C": {Key: "115/电视剧/日番/C", Title: "C", Category: "电视剧/日番"},
		"115/综艺/D":     {Key: "115/综艺/D", Title: "D", Category: "综艺"},
		"115/手动/E":     {Key: "115/手动/E", Title: "E", Category: "手动"}, // 其他目录里的，不归这次管
	}
	// 新规则去掉了 电视剧/日番 和 综艺；综艺已经在其他目录里
	newL := layoutOf(map[string]string{"电影": "movie", "综艺": ""})
	got := categoryDrops(oldL, newL, ledger)
	if len(got) != 1 || got[0].Dir != "电视剧/日番" || got[0].Titles != 2 || got[0].Type != "tv" || len(got[0].Sample) != 2 {
		t.Fatalf("got %+v", got)
	}
	// 没去掉任何规则
	if got := categoryDrops(oldL, oldL, ledger); len(got) != 0 {
		t.Errorf("没有变化却报了 %+v", got)
	}
}

// 端到端：存进配置后台账聚合认得出，整理分类不受影响
func TestExtraDirsLedgerAndClassify(t *testing.T) {
	newTestDB(t, "extradirs.db")
	notifyConfigSource = &config.Config{DataDir: t.TempDir(), ConfigDir: t.TempDir()}
	t.Cleanup(func() { notifyConfigSource = nil; forgetLibLayoutCaches() })
	h := &Handler{DB: model.DB, Config: notifyConfigSource}

	rules := []model.CategoryRule{
		{MediaType: "movie", Name: "电影", IsDefault: true},
		{MediaType: "tv", Name: "剧集", IsDefault: true},
	}
	if err := replaceCategoryRules(model.DB, rules); err != nil {
		t.Fatal(err)
	}
	for _, rel := range []string{
		"115/电影/阿凡达 (2009)/阿凡达.strm",
		"115/LGNB电影/无间道 (2002)/无间道.strm",
		"115/LGNB电影/英雄本色.1986.strm",
		"115/纪录片/地球脉动/S01E01.strm",
	} {
		model.DB.Create(&model.SyncedFile{FileID: rel, RelPath: rel, Kind: "video"})
	}
	forgetLibLayoutCaches()
	if n := len(scanLedgerTitlesCached()); n != 1 {
		t.Fatalf("没配其他目录时应只认 1 部, got %d", n)
	}

	if _, _, err := h.addLibExtraDirs([]libExtraDir{{Dir: "115/LGNB电影", Type: "movie"}, {Dir: "纪录片", Type: "auto"}}); err != nil {
		t.Fatal(err)
	}
	got := scanLedgerTitlesCached() // addLibExtraDirs 清了缓存，这里是新布局
	if len(got) != 4 {
		t.Fatalf("应认出 4 部, got %d: %v", len(got), got)
	}
	if e := got["115/LGNB电影/英雄本色.1986.strm"]; e == nil || !e.solo() || e.LibName != "115" {
		t.Errorf("平铺片目: %+v", e)
	}
	if e := got["115/纪录片/地球脉动"]; e == nil || e.MediaType != "tv" || e.LibName != "115" {
		t.Errorf("自动类型按集号认成剧集: %+v", e)
	}
	if dirs := loadLibExtraDirs(); len(dirs) != 2 || dirs[0].Dir != "LGNB电影" {
		t.Errorf("库名应被剥掉（ledgerLibNames 认得 115）: %+v", dirs)
	}

	// 整理分类不读其他目录
	for _, m := range []*TmdbMedia{{Title: "x", MediaType: "movie"}, {Title: "y", MediaType: "tv"}} {
		if c := classifyMedia(m); c == "LGNB电影" || c == "纪录片" {
			t.Errorf("整理分类落进了其他目录: %s", c)
		}
	}
}
