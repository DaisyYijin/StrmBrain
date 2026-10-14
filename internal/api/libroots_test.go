package api

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

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
		"影视/电影/A":  "/影视/电影/A",
		"影视2/剧集/B": "/盘2/影视2/剧集/B", // 原来按字符串前缀剥，会剥成「2/剧集/B」
		"影视2":      "/盘2/影视2",
		"别的/电影":    "/影视/别的/电影", // 认不出库名：退回主库、整段照用（老行为）
		".":        "/影视",
		`影视\电影\A`:  "/影视/电影/A",
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

// 附加库里的片目和主库一样：能认、能整理、能移（整理落进哪个库由分类规则决定，第 2 期）
func TestRowActionsOtherLib(t *testing.T) {
	roles := map[string]string{"100": "library", "500": "library", "300": "redundant"}
	layout := layoutOf(map[string]string{"电影/华语电影": "movie", "剧集/国产剧": "tv"})

	other := []browseCrumb{{"500", "动漫库"}, {"501", "电影"}, {"502", "华语电影"}}
	a := rowActionsOf(other, roles, layout, fileEntry{ID: "503", Name: "某片 (2020)", IsDir: true})
	if !a.Title || !a.Move || !a.Organize || a.TitleKey != "动漫库/电影/华语电影/某片 (2020)" {
		t.Fatalf("附加库的片目：能认、能整理、能移，实得 %+v", a)
	}
	inTitle := []browseCrumb{{"500", "动漫库"}, {"501", "剧集"}, {"502", "国产剧"}, {"503", "某剧"}}
	if a := rowActionsOf(inTitle, roles, layout, fileEntry{ID: "f", Name: "某剧 S01E01.mkv", Video: true}); !a.Episode {
		t.Fatalf("附加库片目里的视频能指定季集，实得 %+v", a)
	}
}

// ==================== 第 2 期：分类规则指定媒体库 ====================

func resetClassifyCache() {
	classifyRuleMu.Lock()
	classifyRuleCache = map[string][]model.CategoryRule{}
	classifyRuleAt = map[string]time.Time{}
	classifyRuleMu.Unlock()
}

// library 不是匹配条件：只写了它的规则仍是兜底；值原样存下
func TestParseCategoryYAMLLibrary(t *testing.T) {
	rows, err := parseCategoryYAML(`
tv:
  动漫番剧:
    genre_ids: '16'
    library: 动漫库
  剧集:
    library: '剧集库'
`)
	if err != nil || len(rows) != 2 {
		t.Fatalf("解析失败: %v %+v", err, rows)
	}
	if rows[0].Library != "动漫库" || rows[0].IsDefault || rows[0].Unsupported != "" {
		t.Fatalf("带条件的规则：库照存、不是兜底，实得 %+v", rows[0])
	}
	if rows[1].Library != "剧集库" || !rows[1].IsDefault {
		t.Fatalf("只写了 library 的规则仍是兜底，实得 %+v", rows[1])
	}
}

func TestClassifyTargetLibrary(t *testing.T) {
	newTestDB(t, "libroots_classify.db")
	resetClassifyCache()
	t.Cleanup(resetClassifyCache)
	seedCategoryRules(t,
		model.CategoryRule{MediaType: "tv", Name: "动漫番剧", GenreIds: "16", Library: "动漫库", Priority: 1},
		model.CategoryRule{MediaType: "tv", Name: "剧集", IsDefault: true, Priority: 2},
	)
	if c, lib := classifyTarget(&TmdbMedia{MediaType: "tv", GenreIDs: []int{16}}); c != "动漫番剧" || lib != "动漫库" {
		t.Fatalf("命中规则应带回它的库，实得 %q %q", c, lib)
	}
	if c, lib := classifyTarget(&TmdbMedia{MediaType: "tv", GenreIDs: []int{18}}); c != "剧集" || lib != "" {
		t.Fatalf("兜底没写库 = 主库，实得 %q %q", c, lib)
	}
}

func TestPickRuleLib(t *testing.T) {
	libs := []libRoot{{Cid: "1", Name: "影视"}, {Cid: "2", Name: "动漫库"}}
	cases := map[string]string{"": "1", "动漫库": "2", "/动漫库/": "2", "2": "2", "影视": "1"}
	for in, want := range cases {
		if got, err := pickRuleLib(in, libs); err != nil || got != want {
			t.Errorf("pickRuleLib(%q)=%q,%v want %q", in, got, err, want)
		}
	}
	if _, err := pickRuleLib("不存在的库", libs); err == nil {
		t.Fatal("认不出的库必须报错，不能退回主库")
	}
	if _, err := pickRuleLib("影视", []libRoot{{Cid: "1", Name: "影视"}, {Cid: "3", Name: "影视"}}); err == nil {
		t.Fatal("同名的两个库认不出是哪个，必须报错")
	}
}

// 整理上下文：按规则切库 → 落点、库名前缀、记录的 LibCid 都跟着换；下一个条目复位回主库
func TestOrgCtxClassifySwitchesLibrary(t *testing.T) {
	newTestDB(t, "libroots_ctx.db")
	resetClassifyCache()
	t.Cleanup(resetClassifyCache)
	seedCategoryRules(t,
		model.CategoryRule{MediaType: "tv", Name: "动漫番剧", GenreIds: "16", Library: "动漫库", Priority: 1},
		model.CategoryRule{MediaType: "tv", Name: "剧集", IsDefault: true, Priority: 2},
		model.CategoryRule{MediaType: "movie", Name: "电影", IsDefault: true, Library: "没有这个库", Priority: 1},
	)
	sink := &orgSink{libName: "影视", libResolved: true, curNames: map[string]string{"lib2": "动漫库"}, jobs: map[string]scrapeJob{}}
	ctx := &orgCtx{cfg: &OrgConfig{Library: "lib", OtherLibs: []string{"lib2"}}, sink: sink, libAbs: "/影视"}
	ctx.absOf = map[string]string{"lib": "/影视", "lib2": "/盘2/动漫库"}

	cat, err := ctx.classify(&TmdbMedia{MediaType: "tv", GenreIDs: []int{16}})
	if err != nil || cat != "动漫番剧" {
		t.Fatalf("classify: %q %v", cat, err)
	}
	if ctx.libCid() != "lib2" || ctx.libAbs != "/盘2/动漫库" || sink.libRel("动漫番剧/X") != "动漫库/动漫番剧/X" {
		t.Fatalf("应切到附加库: cid=%s abs=%s rel=%s", ctx.libCid(), ctx.libAbs, sink.libRel("动漫番剧/X"))
	}
	rec := &model.OrganizeRecord{Source: "x", TargetDir: "动漫番剧/X", Status: "success"}
	sink.note(rec)
	if rec.LibCid != "lib2" {
		t.Fatalf("记录应带上附加库，实得 %q", rec.LibCid)
	}

	// 下一个条目：复位回主库，没写库的规则就在主库
	ctx.enterLib("")
	if _, err := ctx.classify(&TmdbMedia{MediaType: "tv", GenreIDs: []int{18}}); err != nil {
		t.Fatal(err)
	}
	if ctx.libCid() != "lib" || sink.libRel("剧集/Y") != "影视/剧集/Y" {
		t.Fatalf("没写库的规则应回主库: cid=%s rel=%s", ctx.libCid(), sink.libRel("剧集/Y"))
	}
	rec2 := &model.OrganizeRecord{Source: "y", TargetDir: "剧集/Y", Status: "success"}
	sink.note(rec2)
	if rec2.LibCid != "" {
		t.Fatalf("主库的记录 LibCid 记空（与老记录同口径），实得 %q", rec2.LibCid)
	}

	// 规则写了认不出的库：报错，且不切库
	if _, err := ctx.classify(&TmdbMedia{MediaType: "movie"}); err == nil {
		t.Fatal("认不出的库必须报错")
	}
	if ctx.libCid() != "lib" {
		t.Fatalf("报错时不该切库，实得 %s", ctx.libCid())
	}
}

// Emby 刷新按库各刷一个最浅目录
func TestOrgSinkRefreshPerLibrary(t *testing.T) {
	s := &orgSink{localRoot: "/m", refreshDirs: []string{"影视/剧集/A", "动漫库/番剧/B", "影视/电影/C/D", "动漫库/番剧"}}
	dirs, _ := s.refreshTarget()
	want := []string{filepath.Join("/m", "影视", "剧集", "A"), filepath.Join("/m", "动漫库", "番剧")}
	if strings.Join(dirs, "|") != strings.Join(want, "|") {
		t.Fatalf("每个库各刷最浅的一个，实得 %v", dirs)
	}
}

// 洗版只比目标库里的版本：别的库同一路径下的不算
func TestWashScannerInLib(t *testing.T) {
	newTestDB(t, "libroots_wash.db")
	model.DB.Create(&model.SyncedFile{FileID: "a", RelPath: "影视/剧集/X/Season 01/X.S01E01.strm", Kind: "video"})
	model.DB.Create(&model.SyncedFile{FileID: "b", RelPath: "动漫库/剧集/X/Season 01/X.S01E01.strm", Kind: "video"})
	rows := newWashScanner(nil, nil).inLib("动漫库").libFiles("剧集/X/Season 01")
	if len(rows) != 1 || rows[0].FileID != "b" {
		t.Fatalf("只该拿到动漫库那一份，实得 %+v", rows)
	}
}

func TestRecordLibOK(t *testing.T) {
	cfg := &OrgConfig{Library: "1", OtherLibs: []string{"2"}}
	if !recordLibOK(cfg, "") || !recordLibOK(cfg, "1") || !recordLibOK(cfg, "2") || recordLibOK(cfg, "9") {
		t.Fatal("记录所在库的判定不对")
	}
}
