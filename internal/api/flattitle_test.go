package api

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"115-station/internal/model"
)

// 平铺片目：视频直接放在电影分类目录里，一个视频算一部（ledger.go 的 isSoloTitleKey）

func TestTitleOfSolo(t *testing.T) {
	layout := buildLibCategoryLayout([]model.CategoryRule{
		// 2026-10-12 用户现场：分类名带着库名那一层
		{MediaType: "movie", Name: "影视/LGNB电影/LGNB中国香港电影"},
		{MediaType: "movie", Name: "电影"},
		{MediaType: "tv", Name: "剧集"},
		{MediaType: "movie", Name: "纪录片"},
		{MediaType: "tv", Name: "纪录片"},
	})
	cases := []struct {
		rel                string
		key, titleDir, cat string
		kind               string
		ok                 bool
	}{
		{"影视/LGNB电影/LGNB中国香港电影/无间道.2002.1080p.mkv.strm",
			"影视/LGNB电影/LGNB中国香港电影/无间道.2002.1080p.mkv.strm", "无间道.2002.1080p.mkv", "影视/LGNB电影/LGNB中国香港电影", "movie", true},
		// 带库名的普通写法
		{"影视/电影/无间道.2002.strm", "影视/电影/无间道.2002.strm", "无间道.2002", "电影", "movie", true},
		// 同一个分类里有目录的照旧按目录算
		{"影视/电影/流浪地球 (2019)/流浪地球.strm", "影视/电影/流浪地球 (2019)", "流浪地球 (2019)", "电影", "movie", true},
		// 剧集分类里平铺的集不算
		{"影视/剧集/狂飙.S01E01.strm", "", "", "", "", false},
		// 电影剧集同名的分类：带集号的不算，不带的算电影
		{"影视/纪录片/地球脉动.S01E01.strm", "", "", "", "", false},
		{"影视/纪录片/徒手攀岩.2018.strm", "影视/纪录片/徒手攀岩.2018.strm", "徒手攀岩.2018", "纪录片", "movie", true},
		// 调用方拿目录探片目时补的占位段不是 STRM，不当平铺
		{"影视/电影/_", "", "", "", "", false},
	}
	for _, c := range cases {
		key, dir, cat, kind, ok := layout.titleOf(c.rel)
		if key != c.key || dir != c.titleDir || cat != c.cat || kind != c.kind || ok != c.ok {
			t.Errorf("%s → %q %q %q %q %v", c.rel, key, dir, cat, kind, ok)
		}
	}
}

func TestScanLedgerTitlesSolo(t *testing.T) {
	ledgerTestDB(t, []model.CategoryRule{{MediaType: "movie", Name: "电影/香港"}}, []model.SyncedFile{
		{FileID: "a", Kind: "video", RelPath: "影视/电影/香港/无间道.2002.1080p.BluRay.x264.mkv.strm"},
		{FileID: "b", Kind: "video", RelPath: "影视/电影/香港/英雄本色 [tmdbid=11134].strm"},
		{FileID: "c", Kind: "video", RelPath: "影视/电影/香港/喋血双雄 (1989)/喋血双雄.strm"},
	})
	got := scanLedgerTitles()
	checkLedgerTitles(t, got, []wantTitle{
		{"影视/电影/香港/无间道.2002.1080p.BluRay.x264.mkv.strm", "无间道", "2002", "movie", 0},
		{"影视/电影/香港/英雄本色 [tmdbid=11134].strm", "英雄本色", "", "movie", 11134},
		{"影视/电影/香港/喋血双雄 (1989)", "喋血双雄", "1989", "movie", 0},
	})
	e := got["影视/电影/香港/无间道.2002.1080p.BluRay.x264.mkv.strm"]
	if e.LibName != "影视" || e.Videos != 1 || e.Category != "电影/香港" || e.dirKey() != "影视/电影/香港" || !e.solo() {
		t.Fatalf("平铺片目: %+v dir=%s", e, e.dirKey())
	}
	rows := scrapeDirVideoRows(e.Key)
	if len(rows) != 0 { // 没 pickcode 的行刮削不取（scrapeVideoRow）
		t.Fatalf("没 pickcode 不该取到: %+v", rows)
	}
	model.DB.Model(&model.SyncedFile{}).Where("file_id IN ?", []string{"a", "b"}).Update("pick_code", "p")
	if rows := scrapeDirVideoRows(e.Key); len(rows) != 1 || rows[0].FileID != "a" {
		t.Fatalf("平铺片目只有它自己这一行: %+v", rows)
	}
	if rows := ledgerVideoRowsByTitle()[e.Key]; len(rows) != 1 || rows[0].FileID != "a" {
		t.Fatalf("按片目分组: %+v", rows)
	}
}

// 同一个分类目录里的别的东西（目录级 poster.jpg / movie.nfo、别的片的图）都不算这一部的
func TestInspectSoloTitle(t *testing.T) {
	root := t.TempDir()
	dir := "影视/电影/香港"
	for _, n := range []string{"poster.jpg", "fanart.jpg", "movie.nfo", "A.strm", "A.nfo", "A-poster.jpg", "A-fanart.jpg", "B.strm", "C-poster.jpg"} {
		touch(t, filepath.Join(root, dir, n), "x")
	}
	insp := func(name string) localTitleDetail {
		e := &ledgerTitleEntry{Key: dir + "/" + name + ".strm", MediaType: "movie"}
		return inspectLocalTitleDetail(root, e, []model.SyncedFile{{Kind: "video", RelPath: e.Key}})
	}
	a := insp("A")
	if a.Status != "ok" || !a.HasNFO || !a.HasPoster || a.Missing || a.Dir != filepath.Join(root, dir) {
		t.Fatalf("A: %s %v nfo=%v poster=%v missing=%v", a.Status, a.Lack, a.HasNFO, a.HasPoster, a.Missing)
	}
	if a.Files[0].Name != "A-poster.jpg" || a.Files[1].Name != "A-fanart.jpg" {
		t.Fatalf("A 的图: %+v", a.Files)
	}
	b := insp("B")
	if b.HasNFO || b.HasPoster || b.Status != "miss" || !reflect.DeepEqual(b.Lack, []string{"海报", "背景图", "NFO"}) {
		t.Fatalf("B 不该借用目录级的 / 别的片的: %s %v nfo=%v poster=%v", b.Status, b.Lack, b.HasNFO, b.HasPoster)
	}
	// STRM 不在了：本地被删 / 挂载没就绪
	if c := insp("C"); !c.Missing || c.Status != "miss" {
		t.Fatalf("C: missing=%v %s", c.Missing, c.Status)
	}
	// 缩略图同口径
	if f, _ := findSoloImage(filepath.Join(root, dir), soloImageNames("B", "poster")); f != "" {
		t.Fatalf("B 不该有海报: %s", f)
	}
	if f, _ := findSoloImage(filepath.Join(root, dir), soloImageNames("A", "fanart")); filepath.Base(f) != "A-fanart.jpg" {
		t.Fatalf("A 背景图: %s", f)
	}
}

// 刮削：图按视频名写；分类目录里已有的 poster.jpg 不拿来用、不收掉
func TestScrapeSoloArt(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "香港")
	touch(t, filepath.Join(dir, "poster.jpg"), "category-poster")
	touch(t, filepath.Join(dir, "A.strm"), "u")
	w := newFileScrapeWriter(nil, false, false)
	w.localRoot = filepath.Dir(dir)
	sess := newScrapeSession(&TmdbClient{}, fileScrapeOpts{WriteImages: true}, w, &fakeScrapeReporter{})
	fetches := 0
	sess.fetch = func(p, size string) ([]byte, string, error) {
		fetches++
		return []byte("new" + p), "https://img.test" + p, nil
	}
	v := scrapeVideo{Name: "A", Dir: metaDest{Local: dir}}
	r := &titleRun{s: sess, t: scrapeTitle{Kind: "movie", Title: "A", Dir: metaDest{Local: dir}, Videos: []scrapeVideo{v}, Solo: true},
		st: &titleScrapeStat{}, tmdbID: 1}
	r.images([]byte(`{"poster_path":"/p.jpg","backdrop_path":"/f.jpg"}`))
	if got := readFile(t, filepath.Join(dir, "A-poster.jpg")); got != "new/p.jpg" {
		t.Fatalf("A-poster.jpg 应下载 TMDB 的，不该沿用分类目录的 poster.jpg：%q", got)
	}
	if got := readFile(t, filepath.Join(dir, "A-fanart.jpg")); got != "new/f.jpg" || fetches != 2 {
		t.Fatalf("A-fanart.jpg %q，下载 %d 次", got, fetches)
	}
	if got := readFile(t, filepath.Join(dir, "poster.jpg")); got != "category-poster" {
		t.Fatal("分类目录的 poster.jpg 不归这一部管，不能收掉或改写")
	}
	if fileExists(filepath.Join(dir, "fanart.jpg")) {
		t.Fatal("平铺片目不写目录级的图")
	}

	// NFO 与视频同名，不写 movie.nfo
	r.movieNFO([]byte(`{"title":"A"}`))
	if !fileExists(filepath.Join(dir, "A.nfo")) || fileExists(filepath.Join(dir, "movie.nfo")) {
		t.Fatal("平铺片目的 NFO 应写成 A.nfo")
	}
}

// 片目还在不在只看它自己的 STRM：同目录别的片还在不算；收回的只是它自己的
func TestScrapeCompensateSolo(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "影视", "电影")
	touch(t, filepath.Join(dir, "B.strm"), "u")
	touch(t, filepath.Join(dir, "A.nfo"), "x")
	touch(t, filepath.Join(dir, "A-poster.jpg"), "x")
	st := scrapeTitle{Kind: "movie", Dir: metaDest{Local: dir}, Solo: true,
		Videos: []scrapeVideo{{Name: "A", Dir: metaDest{Local: dir}}}}
	if st.alive() {
		t.Fatal("A.strm 不在，片目不算还在")
	}
	if p := st.localPath(); p != filepath.Join(dir, "A.strm") {
		t.Fatalf("探测路径应是 STRM 本身: %s", p)
	}
	n := scrapeCompensate(st, []string{filepath.Join(dir, "A.nfo"), filepath.Join(dir, "A-poster.jpg")}, root)
	if n != 2 || fileExists(filepath.Join(dir, "A.nfo")) || !fileExists(filepath.Join(dir, "B.strm")) {
		t.Fatalf("收回 %d 个", n)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatal("分类目录里还有别的片，不能收掉")
	}
}

func TestLocalEmbyTitleKeySolo(t *testing.T) {
	ledger := map[string]*ledgerTitleEntry{
		"影视/电影/A.strm":      {Key: "影视/电影/A.strm"},
		"影视/电影/流浪地球 (2019)": {Key: "影视/电影/流浪地球 (2019)"},
	}
	cases := map[string]string{
		"/m/影视/电影/A.strm":                "影视/电影/A.strm",
		"/m/影视/电影/B.strm":                "", // 台账没有
		"/m/影视/电影/流浪地球 (2019)/流浪地球.strm": "影视/电影/流浪地球 (2019)",
	}
	for p, want := range cases {
		if got := localEmbyTitleKeyOf(p, "/m", ledger, false); got != want {
			t.Errorf("%s → %q，预期 %q", p, got, want)
		}
	}
}
