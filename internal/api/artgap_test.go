package api

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"115-station/internal/model"
)

// TMDB 没有背景图：刮削记下结论，海报墙不再算缺失；编号对不上、后来有图了都不算数

func artGapRun(t *testing.T, dir, key string, tmdbID int) (*titleRun, map[string]int) {
	t.Helper()
	fetches := map[string]int{}
	w := newFileScrapeWriter(nil, false, false)
	w.localRoot = filepath.Dir(dir)
	sess := newScrapeSession(&TmdbClient{}, fileScrapeOpts{WriteImages: true}, w, &fakeScrapeReporter{})
	sess.fetch = func(p, size string) ([]byte, string, error) {
		fetches[p]++
		return []byte("img" + p), "https://img.test" + p, nil
	}
	r := &titleRun{s: sess, st: &titleScrapeStat{}, tmdbID: tmdbID,
		t: scrapeTitle{Key: key, Kind: "movie", Title: "片", Dir: metaDest{Local: dir},
			Videos: []scrapeVideo{{Name: "片", Dir: metaDest{Local: dir}}}}}
	return r, fetches
}

func artGapOf(key string) (model.ScrapeArtGap, bool) {
	var g model.ScrapeArtGap
	err := model.DB.Where("key = ?", key).First(&g).Error
	return g, err == nil
}

func TestArtGapRecordedAndCleared(t *testing.T) {
	newTestDB(t, "artgap.db")
	dir := filepath.Join(t.TempDir(), "片 (2020)")
	os.MkdirAll(dir, 0o755)
	key := "影视/电影/片 (2020)"

	r, _ := artGapRun(t, dir, key, 7)
	r.images([]byte(`{"poster_path":"/p.jpg"}`))
	if g, ok := artGapOf(key); !ok || !g.NoBackdrop || g.TmdbID != 7 {
		t.Fatalf("TMDB 没有背景图要记下来：%+v %v", g, ok)
	}

	// TMDB 后来补了图：删行
	r, _ = artGapRun(t, dir, key, 7)
	r.images([]byte(`{"poster_path":"/p.jpg","backdrop_path":"/f.jpg"}`))
	if _, ok := artGapOf(key); ok {
		t.Fatal("有背景图了还留着记录")
	}
}

func TestArtGapFallbackFromImageList(t *testing.T) {
	newTestDB(t, "artgap2.db")
	dir := filepath.Join(t.TempDir(), "片 (2020)")
	os.MkdirAll(dir, 0o755)
	key := "影视/电影/片 (2020)"

	// 详情没有主背景，images 里有：无语言的优先，横版图仍取带字的那张
	r, fetches := artGapRun(t, dir, key, 7)
	r.images([]byte(`{"poster_path":"/p.jpg","images":{"backdrops":[
		{"file_path":"/zh.jpg","iso_639_1":"zh","vote_average":9},
		{"file_path":"/null.jpg","iso_639_1":null,"vote_average":5}]}}`))
	if got := readFile(t, filepath.Join(dir, "fanart.jpg")); got != "img/null.jpg" {
		t.Fatalf("fanart 兜底挑错：%q", got)
	}
	if got := readFile(t, filepath.Join(dir, "landscape.jpg")); got != "img/zh.jpg" {
		t.Fatalf("landscape：%q", got)
	}
	if _, ok := artGapOf(key); ok || fetches["/null.jpg"] != 1 {
		t.Fatalf("兜底拿到图就不该记「TMDB 没有」：%v", fetches)
	}

	// 只有带字的：fanart 用它，landscape 不再复制一份
	dir2 := filepath.Join(t.TempDir(), "片2")
	os.MkdirAll(dir2, 0o755)
	r, _ = artGapRun(t, dir2, "影视/电影/片2", 8)
	r.images([]byte(`{"images":{"backdrops":[{"file_path":"/zh.jpg","iso_639_1":"zh"}]}}`))
	if !fileExists(filepath.Join(dir2, "fanart.jpg")) || fileExists(filepath.Join(dir2, "landscape.jpg")) {
		t.Fatal("只有带字背景时：fanart 用它、landscape 不写")
	}
}

func TestArtGapGrade(t *testing.T) {
	newTestDB(t, "artgap3.db")
	root := t.TempDir()
	mk := "影视/电影/老片 (2001)"
	touch(t, filepath.Join(root, mk, "老片.strm"), "u")
	touch(t, filepath.Join(root, mk, "老片.nfo"), "x")
	touch(t, filepath.Join(root, mk, "poster.jpg"), "x")
	rows := []model.SyncedFile{{Kind: "video", RelPath: mk + "/老片.strm"}}
	model.DB.Create(&model.ScrapeArtGap{Key: mk, TmdbID: 11, NoBackdrop: true})

	d := inspectLocalTitleDetail(root, &ledgerTitleEntry{Key: mk, MediaType: "movie", TmdbID: 11}, rows)
	if d.Status != "ok" || d.Lack != nil || !reflect.DeepEqual(d.Soft, []string{"背景图"}) {
		t.Fatalf("TMDB 没有的背景图不算缺：%s %v %v", d.Status, d.Lack, d.Soft)
	}
	if f := d.Files[1]; f.Label != "背景图" || !f.Optional || f.Note == "" {
		t.Fatalf("详情里要说明原因：%+v", f)
	}
	// 目录名没有编号的也认
	if d = inspectLocalTitleDetail(root, &ledgerTitleEntry{Key: mk, MediaType: "movie"}, rows); d.Status != "ok" {
		t.Fatalf("无编号：%s %v", d.Status, d.Lack)
	}
	// 后来指定了别的条目：结论不算数
	d = inspectLocalTitleDetail(root, &ledgerTitleEntry{Key: mk, MediaType: "movie", TmdbID: 12}, rows)
	if d.Status != "partial" || !reflect.DeepEqual(d.Lack, []string{"背景图"}) {
		t.Fatalf("编号对不上仍算缺：%s %v", d.Status, d.Lack)
	}
}
