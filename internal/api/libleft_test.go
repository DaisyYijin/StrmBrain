package api

import (
	"os"
	"path/filepath"
	"testing"

	"115-station/internal/model"
)

// 残留 = 台账第一段既不是配置中的库名，也不是分类目录的第一段（老台账不带库名）
func TestFindLibLeftovers(t *testing.T) {
	counts := map[string][2]int64{
		"影视":  {10, 4},
		"动漫库": {5, 2},
		"旧库":  {3, 1},
		"电影":  {7, 3}, // 老台账：不带库名，第一段是分类目录
		"剧集":  {2, 1},
	}
	layout := catLayout(map[string]string{"电影/华语电影": "movie", "剧集": "tv"})
	got := findLibLeftovers(counts, []string{"影视", "动漫库"}, layout)
	if len(got) != 1 || got[0].Name != "旧库" || got[0].Files != 3 || got[0].Videos != 1 {
		t.Fatalf("只有「旧库」是残留，实得 %+v", got)
	}
}

func TestLedgerTopCounts(t *testing.T) {
	newTestDB(t, "libleft_counts.db")
	model.DB.Create(&model.SyncedFile{FileID: "1", RelPath: "影视/电影/A/a.strm", Kind: "video"})
	model.DB.Create(&model.SyncedFile{FileID: "2", RelPath: "影视/电影/A/a.nfo", Kind: "asset"})
	model.DB.Create(&model.SyncedFile{FileID: "3", RelPath: "旧库/剧集/B/b.strm", Kind: "video"})
	model.DB.Create(&model.SyncedFile{FileID: "4", RelPath: "孤零零.strm", Kind: "video"})
	c, err := ledgerTopCounts()
	if err != nil {
		t.Fatal(err)
	}
	if c["影视"] != [2]int64{2, 1} || c["旧库"] != [2]int64{1, 1} || len(c) != 2 {
		t.Fatalf("按第一段计数不对: %+v", c)
	}
}

// 清理只删这个库：库名里的 _ 不能当通配符顺带删掉别的库的台账
func TestCleanLibLocal(t *testing.T) {
	newTestDB(t, "libleft_clean.db")
	root := t.TempDir()
	for _, rel := range []string{"a_b/电影/X/x.strm", "acb/电影/Y/y.strm"} {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
		model.DB.Create(&model.SyncedFile{FileID: rel, RelPath: rel, Kind: "video"})
	}
	done, err := cleanLibLocal(root, "a_b")
	if err != nil || !done {
		t.Fatalf("应删掉本地目录: done=%v err=%v", done, err)
	}
	if _, err := os.Stat(filepath.Join(root, "a_b")); !os.IsNotExist(err) {
		t.Fatal("a_b 本地目录应已删除")
	}
	if _, err := os.Stat(filepath.Join(root, "acb", "电影", "Y", "y.strm")); err != nil {
		t.Fatal("别的库的本地文件不能动")
	}
	var n int64
	model.DB.Model(&model.SyncedFile{}).Where("rel_path LIKE ?", "acb/%").Count(&n)
	if n != 1 {
		t.Fatal("别的库的台账行不能被 _ 通配删掉")
	}
	model.DB.Model(&model.SyncedFile{}).Where("rel_path = ?", "a_b/电影/X/x.strm").Count(&n)
	if n != 0 {
		t.Fatal("a_b 的台账行应已删除")
	}
}
