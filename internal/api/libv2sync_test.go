package api

import (
	"os"
	"path/filepath"
	"testing"

	"115-station/internal/model"
)

// V2 阶段 2：同步与本地路径按媒体库表（libstore.go / ledger.go / mediaroot.go）

func TestLibOfLocalRelMultiLevel(t *testing.T) {
	libs := []model.Library{
		{ID: 1, Local: "电影/主号"},
		{ID: 2, Local: "电影/副号"},
		{ID: 3, Local: "剧集"},
	}
	cases := map[string][2]any{
		"电影/主号/阿凡达 (2009)/阿凡达.strm": {uint(1), "阿凡达 (2009)/阿凡达.strm"},
		"电影/副号/阿凡达 (2009)/阿凡达.strm": {uint(2), "阿凡达 (2009)/阿凡达.strm"},
		"剧集/三体/Season 1/e1.strm":    {uint(3), "三体/Season 1/e1.strm"},
		`电影\主号\x.strm`:              {uint(1), "x.strm"},
	}
	for rel, want := range cases {
		l, rest, ok := libOfLocalRel(libs, rel)
		if !ok || l.ID != want[0].(uint) || rest != want[1].(string) {
			t.Errorf("%s → %d %q %v，期望 %v", rel, l.ID, rest, ok, want)
		}
	}
	for _, rel := range []string{"电影/别的/x.strm", "电影主号/x.strm", "动漫/x.strm"} {
		if _, _, ok := libOfLocalRel(libs, rel); ok {
			t.Errorf("%s 不在任何库里", rel)
		}
	}
}

func TestSplitLibRelUsesLibraryTable(t *testing.T) {
	newTestDB(t, "split_multi.db")
	model.DB.Create(&model.Library{Name: "电影·副号", Account: 2, Cid: "9", Local: "电影/副号"})
	forgetLibraries()
	t.Cleanup(forgetLibraries)
	if lib, rest := splitLibRel("电影/副号/阿凡达/a.strm"); lib != "电影/副号" || rest != "阿凡达/a.strm" {
		t.Fatalf("多级本地目录：%q %q", lib, rest)
	}
	if lib, rest := splitLibRel("影视/电影/x/a.strm"); lib != "影视" || rest != "电影/x/a.strm" {
		t.Fatalf("不在表里的退回第一段：%q %q", lib, rest)
	}
}

func TestLayoutPerLibrary(t *testing.T) {
	movieRules := []model.CategoryRule{{MediaType: "movie", Name: "华语电影"}, {MediaType: "movie", Name: "外语电影", IsDefault: true}}
	l := libCategoryLayout{libs: layoutLibsOf([]model.Library{
		{Local: "电影/主号", SchemeID: 1},
		{Local: "电影/副号"},             // 不分类：片目直接在库下
		{Local: "备份", Depth: 2},      // 只同步、用户声明两层
		{Local: "电影/旧", SchemeID: 9}, // 方案被删了：按不分类认
	}, map[uint][]model.CategoryRule{1: movieRules})}

	check := func(rel, wantKey, wantTitle, wantCat string) {
		t.Helper()
		key, title, cat, _, ok := l.titleOf(rel)
		if wantKey == "" {
			if ok {
				t.Errorf("%s 不该认成片目：%s", rel, key)
			}
			return
		}
		if !ok || key != wantKey || title != wantTitle || cat != wantCat {
			t.Errorf("%s → %q %q %q %v，期望 %q %q %q", rel, key, title, cat, ok, wantKey, wantTitle, wantCat)
		}
	}
	check("电影/主号/华语电影/流浪地球 (2019)/流浪地球.strm", "电影/主号/华语电影/流浪地球 (2019)", "流浪地球 (2019)", "华语电影")
	check("电影/副号/阿凡达 (2009)/阿凡达.strm", "电影/副号/阿凡达 (2009)", "阿凡达 (2009)", "")
	check("备份/欧美/敦刻尔克 (2017)/a.strm", "备份/欧美/敦刻尔克 (2017)", "敦刻尔克 (2017)", "欧美")
	check("电影/旧/某片/a.strm", "电影/旧/某片", "某片", "")
	check("电影/主号/不是分类/某片/a.strm", "", "", "")
	if lib, n, ok := l.libOf("电影/副号/阿凡达 (2009)"); !ok || lib.local != "电影/副号" || n != 2 {
		t.Fatalf("libOf：%+v %d %v", lib, n, ok)
	}
}

func TestSwapRoot(t *testing.T) {
	if p, ok := swapRoot("/data/strm/影视/a.nfo", "/data/strm", "/media"); !ok || filepath.ToSlash(p) != "/media/影视/a.nfo" {
		t.Fatalf("换根：%q %v", p, ok)
	}
	if _, ok := swapRoot("/data/strm2/a.nfo", "/data/strm", "/media"); ok {
		t.Fatal("按整段比，strm2 不在 strm 下")
	}
	if p, ok := swapRoot(`D:\strm\影视\a.nfo`, `D:\strm`, "/media"); !ok || filepath.ToSlash(p) != "/media/影视/a.nfo" {
		t.Fatalf("反斜杠也认：%q %v", p, ok)
	}
}

func TestCheckMediaRoot(t *testing.T) {
	newTestDB(t, "rootcheck.db")
	root := t.TempDir()
	libs := []model.Library{{Local: "影视"}, {Local: "电影/副号"}}
	if msg := checkMediaRoot(root, libs); msg != "" {
		t.Fatalf("没有台账时不拦：%s", msg)
	}
	model.DB.Create(&model.SyncedFile{FileID: "1", RelPath: "影视/电影/a/a.strm", Kind: "video"})
	if msg := checkMediaRoot(root, libs); msg == "" {
		t.Fatal("台账有东西、根下一个库目录都没有：要拦")
	}
	_ = os.MkdirAll(filepath.Join(root, "电影", "副号"), 0o755)
	if msg := checkMediaRoot(root, libs); msg != "" {
		t.Fatalf("有一个库目录在就算挂上了：%s", msg)
	}
}

func TestMediaRootEnv(t *testing.T) {
	t.Setenv("MEDIA_ROOT", filepath.Join("x", "media"))
	if mediaRoot() != filepath.Join("x", "media") {
		t.Fatalf("MEDIA_ROOT：%s", mediaRoot())
	}
}
