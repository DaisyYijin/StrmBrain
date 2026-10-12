package api

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"115-station/internal/model"
)

// ==================== 多个 115 媒体库（libroots.go） ====================

func TestLibCidsOf(t *testing.T) {
	got := libCidsOf("1", []libRootEntry{{Cid: "2"}, {Cid: " 1 "}, {Cid: "0"}, {Cid: ""}, {Cid: "3"}, {Cid: "2"}})
	if strings.Join(got, ",") != "1,2,3" {
		t.Fatalf("主库在前、去空去根去重，实得 %v", got)
	}
	cfg := fullSyncCfg{Cid: "1", ExtraLibs: []libRootEntry{{Cid: "1"}, {Cid: "2"}}}
	if o := cfg.otherLibCids(); strings.Join(o, ",") != "2" {
		t.Fatalf("附加库不含主库，实得 %v", o)
	}
	if o := (fullSyncCfg{ExtraLibs: []libRootEntry{{Cid: "2"}}}).otherLibCids(); strings.Join(o, ",") != "2" {
		t.Fatalf("没有主库时附加库照列，实得 %v", o)
	}
}

func TestResolveLibRoots(t *testing.T) {
	names := map[string]string{"1": "影视", "2": "动漫", "3": "影视"}
	abs := map[string]string{"1": "/影视", "2": "/盘2/动漫", "3": "/盘3/影视"}
	name := func(c string) string { return names[c] }
	ab := func(c string) string { return abs[c] }

	// 只有一个库：名字取不到也照常（老部署）
	if r, err := resolveLibRoots(func(string) string { return "" }, ab, []string{"1"}); err != nil || len(r) != 1 {
		t.Fatalf("单库应照常返回: %v %v", r, err)
	}
	if r, err := resolveLibRoots(name, ab, []string{"1", "2"}); err != nil || r[1].Name != "动漫" || r[1].Abs != "/盘2/动漫" {
		t.Fatalf("两个库应各自解析: %v %v", r, err)
	}
	if _, err := resolveLibRoots(name, ab, []string{"1", "3"}); err == nil || !strings.Contains(err.Error(), "同名") {
		t.Fatalf("同名的两个库必须拒绝（本地会混进一个目录），实得 %v", err)
	}
	if _, err := resolveLibRoots(name, ab, []string{"1", "9"}); err == nil {
		t.Fatal("多个库时名字取不到必须报错")
	}
}

func TestPanDirOfLocal(t *testing.T) {
	roots := []libRoot{{Cid: "1", Name: "影视", Abs: "/影视"}, {Cid: "2", Name: "影视2", Abs: "/盘2/影视2"}}
	cases := map[string]string{
		"影视/电影/A":   "/影视/电影/A",
		"影视2/剧集/B":  "/盘2/影视2/剧集/B", // 原来按字符串前缀剥，会剥成「2/剧集/B」
		"影视2":       "/盘2/影视2",
		"别的/电影":     "/影视/别的/电影", // 认不出库名：退回主库、整段照用（老行为）
		".":         "/影视",
		`影视\电影\A`: "/影视/电影/A",
	}
	for in, want := range cases {
		if got := panDirOfLocal(roots, in); got != want {
			t.Errorf("panDirOfLocal(%q)=%q want %q", in, got, want)
		}
	}
	// 单库：库名对得上就剥掉
	if got := panDirOfLocal(roots[:1], "影视/电影"); got != "/影视/电影" {
		t.Fatalf("单库实得 %q", got)
	}
}

func TestGroupRowsByLib(t *testing.T) {
	roots := []libRoot{{Cid: "1", Name: "影视"}, {Cid: "2", Name: "动漫"}}
	rows := []model.SyncedFile{
		{FileID: "a", RelPath: "影视/电影/A/a.strm"},
		{FileID: "b", RelPath: "动漫/番剧/B/b.strm"},
		{FileID: "c", RelPath: "影视/电影/C/c.strm"},
		{FileID: "d", RelPath: "旧库/电影/D/d.strm"},
	}
	groups, unknown := groupRowsByLib(roots, rows)
	if len(groups) != 2 || groups[0].cid != "1" || len(groups[0].rows) != 2 || groups[1].cid != "2" || len(groups[1].rows) != 1 {
		t.Fatalf("应按库名分成两组，实得 %+v", groups)
	}
	if strings.Join(unknown, ",") != "旧库" {
		t.Fatalf("认不出的库名不能交给任何库，实得 %v", unknown)
	}
	// 单库：不看名字，全交给它（老台账可能不带库名）
	if g, u := groupRowsByLib(roots[:1], rows); len(g) != 1 || len(g[0].rows) != 4 || len(u) != 0 {
		t.Fatalf("单库应整批交给主库，实得 %+v %v", g, u)
	}
}

// ---- 工作区校验 ----

func TestWorkspaceSlotsIncludeExtraLibs(t *testing.T) {
	vals := map[string]string{
		"full":  `{"cid":"1","extra_libs":[{"cid":"7","path":"/动漫"},{"cid":"1"}]}`,
		"share": `{"folder":"2"}`,
	}
	s := workspaceSlots(func(k string) string { return vals[k] })
	if len(s) != 5 || s[4].key != "library:7" || !strings.Contains(s[4].label, "/动漫") {
		t.Fatalf("附加库应排在四个固定槽位后面（主库重复的不算），实得 %+v", s)
	}
}

func TestCheckWorkspace_ExtraLibs(t *testing.T) {
	f := &fakeResolve{paths: map[string]string{
		"1": "/影视", "2": "/转存", "7": "/盘2/动漫", "8": "/影视/动漫", "9": "/盘3/影视", "10": "/转存/库",
	}}
	slots := func(extra string) []wsSlot {
		return append(wsSlotsOf("1", "2", "", ""), wsSlot{key: wsLibKeyPrefix + extra, label: "附加媒体库", cid: extra})
	}
	changed := func(extra string) map[string]bool { return map[string]bool{wsLibKeyPrefix + extra: true} }

	if err := checkWorkspaceSlots(slots("7"), changed("7"), f.fn); err != nil {
		t.Fatalf("互不包含、不同名的附加库应放行: %v", err)
	}
	if err := checkWorkspaceSlots(slots("8"), changed("8"), f.fn); err == nil {
		t.Fatal("附加库在主库里面必须拒绝")
	}
	if err := checkWorkspaceSlots(slots("9"), changed("9"), f.fn); err == nil || !strings.Contains(err.Error(), "同名") {
		t.Fatalf("和主库同名的附加库必须拒绝，实得 %v", err)
	}
	if err := checkWorkspaceSlots(slots("10"), changed("10"), f.fn); err == nil {
		t.Fatal("附加库在转存目录里面必须拒绝")
	}
	if err := checkWorkspaceSlots(slots("1"), changed("1"), f.fn); err == nil {
		t.Fatal("附加库和主库是同一个目录必须拒绝")
	}
}

// ---- 增量同步 ----

// newMultiLibEnv 两个库：主库 lib=/影视（库名 媒体库），附加库 lib2=/盘2/动漫（库名 动漫库）
func newMultiLibEnv(t *testing.T, dbName string) (*Handler, *stubIncrDeps, incrParams) {
	h, d, p := newIncrTestEnv(t, dbName)
	d.settings["full"] = `{"cid":"lib","extra_libs":[{"cid":"lib2"}]}`
	d.names["lib2"] = "动漫库"
	d.abs["lib2"] = "/盘2/动漫"
	d.abs["d2"] = "/盘2/动漫/番剧/Y"
	d.rel["d2"] = "番剧/Y"
	d.relRoot["d1"] = "lib"
	d.relRoot["d2"] = "lib2"
	return h, d, p
}

// 附加库里上传的视频落在它自己的库名下，Emby 刷新也刷它那一棵
func TestIncrMultiLibPreciseWrite(t *testing.T) {
	h, d, p := newMultiLibEnv(t, "incr_multilib_precise.db")
	d.pages = [][]lifeEvent{{
		{ID: "e1", Type: evUpload, FileID: "f1", Cid: "d2", FileName: "番.mkv", PickCode: "pc1", Time: "100"},
		{ID: "e2", Type: evUpload, FileID: "f2", Cid: "d1", FileName: "片.mkv", PickCode: "pc2", Time: "101"},
	}}
	sum, err := h.executeIncrementalSyncWith(d, p)
	if err != nil {
		t.Fatal(err)
	}
	if sum.StrmCreated != 2 {
		t.Fatalf("两个库各写一个 STRM，实得 %d", sum.StrmCreated)
	}
	for _, rel := range []string{"动漫库/番剧/Y/番.strm", "媒体库/剧集/X/片.strm"} {
		if _, err := os.Stat(filepath.Join(p.LocalPath, filepath.FromSlash(rel))); err != nil {
			t.Fatalf("应生成 %s: %v", rel, err)
		}
	}
	want := map[string]bool{
		filepath.Join(p.LocalPath, "动漫库", "番剧", "Y"): true,
		filepath.Join(p.LocalPath, "媒体库", "剧集", "X"): true,
	}
	if len(d.refreshed) != 2 || !want[d.refreshed[0]] || !want[d.refreshed[1]] {
		t.Fatalf("两个库各刷一次，实得 %v", d.refreshed)
	}
}

// 两个库根上各有一条要回退遍历的事件：base 都是 ""，不能被当成同一个目录合并掉
func TestIncrMultiLibRootsNotMerged(t *testing.T) {
	h, d, p := newMultiLibEnv(t, "incr_multilib_roots.db")
	d.pages = [][]lifeEvent{{
		{ID: "e1", Type: evUpload, FileID: "f1", Cid: "lib", FileName: "a.mkv", Time: "100"},
		{ID: "e2", Type: evUpload, FileID: "f2", Cid: "lib2", FileName: "b.mkv", Time: "101"},
	}}
	if _, err := h.executeIncrementalSyncWith(d, p); err != nil {
		t.Fatal(err)
	}
	if len(d.walked) != 2 {
		t.Fatalf("两个库根各遍历一次，实得 %+v", d.walked)
	}
	bases := map[string]bool{}
	for _, w := range d.walked {
		bases[w.base] = true
		if w.deep {
			t.Fatalf("文件级事件只该浅遍历: %+v", w)
		}
	}
	if !bases["媒体库"] || !bases["动漫库"] {
		t.Fatalf("遍历的路径要带各自的库名，实得 %+v", d.walked)
	}
}

// 附加库里删文件：按它自己的库名推路径删本地 STRM
func TestIncrMultiLibDelete(t *testing.T) {
	h, d, p := newMultiLibEnv(t, "incr_multilib_delete.db")
	rel := "动漫库/番剧/Y/番.strm"
	abs := filepath.Join(p.LocalPath, filepath.FromSlash(rel))
	if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(abs, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	// 台账里没有这一行：走路径推导那一级
	d.pages = [][]lifeEvent{{
		{ID: "e1", Type: evDelete, FileID: "f-x", Cid: "d2", FileName: "番.mkv", Time: "100"},
	}}
	sum, err := h.executeIncrementalSyncWith(d, p)
	if err != nil {
		t.Fatal(err)
	}
	if sum.Deleted != 1 {
		t.Fatalf("应按附加库的库名推出路径删掉，实得 Deleted=%d", sum.Deleted)
	}
	if _, err := os.Stat(abs); !os.IsNotExist(err) {
		t.Fatalf("本地 STRM 应已删除: %v", err)
	}
}

// 附加库解析不出 / 同名：和主库无效一样熔断，一条事件都不拉
func TestIncrMultiLibAbortsOnBadExtra(t *testing.T) {
	h, d, p := newMultiLibEnv(t, "incr_multilib_bad.db")
	delete(d.abs, "lib2")
	if _, err := h.executeIncrementalSyncWith(d, p); err == nil || d.fetchCalls != 0 {
		t.Fatalf("附加库解析不出时必须熔断且不拉事件: err=%v fetch=%d", err, d.fetchCalls)
	}

	h, d, p = newMultiLibEnv(t, "incr_multilib_dup.db")
	d.names["lib2"] = "媒体库"
	if _, err := h.executeIncrementalSyncWith(d, p); err == nil || !strings.Contains(err.Error(), "同名") || d.fetchCalls != 0 {
		t.Fatalf("同名的两个库必须熔断: err=%v fetch=%d", err, d.fetchCalls)
	}
}

// ---- 网盘文件页 ----

// 附加库里的片目能认出、能移出，但第 1 期不许整理 / 指定季集（落盘还只认主库，会被搬进主库）
func TestRowActionsOtherLibBlocked(t *testing.T) {
	newTestDB(t, "libroots_rowactions.db")
	model.DB.Create(&model.Setting{Key: "full", Value: `{"cid":"100","extra_libs":[{"cid":"500"}]}`})
	roles := map[string]string{"100": "library", "500": "library", "300": "redundant"}
	layout := libCategoryLayout{"电影/华语电影": "movie", "剧集/国产剧": "tv"}

	other := []browseCrumb{{"500", "动漫库"}, {"501", "电影"}, {"502", "华语电影"}}
	a := rowActionsOf(other, roles, layout, fileEntry{ID: "503", Name: "某片 (2020)", IsDir: true})
	if !a.Title || !a.Move || a.Organize || a.OrganizeBlock == "" || a.TitleKey != "动漫库/电影/华语电影/某片 (2020)" {
		t.Fatalf("附加库的片目：能认、能移、不能整理，实得 %+v", a)
	}
	inTitle := []browseCrumb{{"500", "动漫库"}, {"501", "剧集"}, {"502", "国产剧"}, {"503", "某剧"}}
	if a := rowActionsOf(inTitle, roles, layout, fileEntry{ID: "f", Name: "某剧 S01E01.mkv"}); a.Episode || a.Block == "" {
		t.Fatalf("附加库片目里的视频不能指定季集，实得 %+v", a)
	}
	main := []browseCrumb{{"100", "影视"}, {"101", "电影"}, {"102", "华语电影"}}
	if a := rowActionsOf(main, roles, layout, fileEntry{ID: "103", Name: "某片 (2020)", IsDir: true}); !a.Organize {
		t.Fatalf("主库的片目照常能整理，实得 %+v", a)
	}
}
