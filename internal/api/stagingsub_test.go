package api

import (
	"errors"
	"strings"
	"testing"

	"115-station/internal/model"
)

// 绑定子目录展开成它里面一层、各自以子目录为扫描根；子目录本身和工作区根都不是条目
func TestExpandStagingEntries(t *testing.T) {
	cfg := &OrgConfig{Pending: "stage", ShareCid: "stage", Library: "lib", Existing: "ex", Redundant: "red",
		SubLibs: map[string]stagingLib{"sub4k": {Cid: "sub4k", Name: "4K", Lib: "lib4k"}}}
	top := []dirEntry{
		{Fid: "a", Name: "普通片 (2020)", IsDir: true, Cid: "a"},
		{Fid: "sub4k", Name: "4K", IsDir: true, Cid: "sub4k"},
		{Fid: "ex", Name: "已存在", IsDir: true, Cid: "ex"},
		{Fid: "f1", Name: "散.mkv"},
		{Fid: "other", Name: "没绑定的子目录", IsDir: true, Cid: "other"},
	}
	listed := map[string]int{}
	list := func(cid string) ([]dirEntry, error) {
		listed[cid]++
		if cid == "sub4k" {
			return []dirEntry{{Fid: "b", Name: "沙丘 (2021)", IsDir: true, Cid: "b"}, {Fid: "f2", Name: "x.mkv"}}, nil
		}
		return nil, errors.New("不该列它")
	}
	var logs []string
	got := expandStagingEntries(list, cfg, top, func(m string) { logs = append(logs, m) })
	want := map[string]string{"a": "stage", "f1": "stage", "other": "stage", "b": "sub4k", "f2": "sub4k"}
	if len(got) != len(want) {
		t.Fatalf("条目数不对: %+v", got)
	}
	for _, e := range got {
		if want[e.Fid] != e.Pending {
			t.Fatalf("%s 的扫描根应为 %s，实得 %s", e.Name, want[e.Fid], e.Pending)
		}
		if e.Parent != "" {
			t.Fatalf("子目录名不能成为识别上下文: %+v", e)
		}
	}
	if listed["sub4k"] != 1 || len(listed) != 1 {
		t.Fatalf("只该列绑定子目录一次: %v", listed)
	}

	// 子目录列不出来：跳过它，别的照常
	bad := func(cid string) ([]dirEntry, error) { return nil, errors.New("风控") }
	got = expandStagingEntries(bad, cfg, top, func(m string) { logs = append(logs, m) })
	if len(got) != 3 {
		t.Fatalf("子目录读失败只跳过它: %+v", got)
	}
}

// stagingWork：同一个扫描根共用一个上下文，sink / held 与根共用
func TestStagingWorkContexts(t *testing.T) {
	cfg := &OrgConfig{Pending: "stage", SubLibs: map[string]stagingLib{"sub4k": {Cid: "sub4k", Lib: "lib4k"}}}
	sink := &orgSink{}
	ctx := &orgCtx{cfg: cfg, sink: sink, held: map[string]*awaitingRef{"held": {id: 1}}, handled: map[string]bool{}}
	cfg.ManualConfirm = true
	es, cs := stagingWork(ctx, []stagingEntry{
		{dirEntry: dirEntry{Fid: "a"}, Pending: "stage"},
		{dirEntry: dirEntry{Fid: "b"}, Pending: "sub4k"},
		{dirEntry: dirEntry{Fid: "held"}, Pending: "sub4k"},
		{dirEntry: dirEntry{Fid: "c"}, Pending: "sub4k"},
	})
	if len(es) != 3 || es[0].Fid != "a" || es[2].Fid != "c" {
		t.Fatalf("等人工确认的要去掉: %+v", es)
	}
	if cs[0] != ctx || cs[1] != cs[2] || cs[1] == ctx {
		t.Fatal("同一个扫描根应共用一个上下文，子目录的另起一个")
	}
	if cs[1].cfg.Pending != "sub4k" || cfg.Pending != "stage" || cs[1].sink != sink {
		t.Fatalf("子目录上下文换扫描根、不改原配置、共用 sink: %s %s", cs[1].cfg.Pending, cfg.Pending)
	}
}

// 选库优先级：来源子目录 > 规则 library > 主库；绑定的库失效时报错，不退回
func TestClassifyBySourceSubdir(t *testing.T) {
	newTestDB(t, "stagingsub_classify.db")
	resetClassifyCache()
	t.Cleanup(resetClassifyCache)
	seedCategoryRules(t,
		model.CategoryRule{MediaType: "tv", Name: "动漫番剧", GenreIds: "16", Library: "动漫库", Priority: 1},
		model.CategoryRule{MediaType: "tv", Name: "剧集", IsDefault: true, Priority: 2},
	)
	sink := &orgSink{libName: "影视", libResolved: true, curNames: map[string]string{"lib2": "动漫库", "lib4k": "4K库"}, jobs: map[string]scrapeJob{}}
	cfg := &OrgConfig{Library: "lib", OtherLibs: []string{"lib2", "lib4k"}, Pending: "stage",
		SubLibs: map[string]stagingLib{
			"sub4k":   {Cid: "sub4k", Name: "4K", Lib: "lib4k"},
			"submain": {Cid: "submain", Name: "普通", Lib: "lib"},
			"subgone": {Cid: "subgone", Name: "旧", Lib: "libgone"},
		}}
	root := &orgCtx{cfg: cfg, sink: sink, libAbs: "/影视"}
	root.absOf = map[string]string{"lib": "/影视", "lib2": "/动漫库", "lib4k": "/4K库"}
	sub := func(p string) *orgCtx {
		c := *cfg
		c.Pending = p
		x := root.withPending(&c)
		x.absOf = root.absOf
		return x
	}
	anime := &TmdbMedia{MediaType: "tv", GenreIDs: []int{16}}

	// 根目录：按规则进动漫库，不是按来源
	if cat, err := root.classify(anime); err != nil || cat != "动漫番剧" || root.libCid() != "lib2" || sink.bySource {
		t.Fatalf("根目录按规则: %q %v %s %v", cat, err, root.libCid(), sink.bySource)
	}
	// 4K 子目录：分类照规则，库按来源（盖过规则的动漫库）
	c4k := sub("sub4k")
	c4k.enterLib("")
	if cat, err := c4k.classify(anime); err != nil || cat != "动漫番剧" || c4k.libCid() != "lib4k" || c4k.libAbs != "/4K库" {
		t.Fatalf("4K 子目录应进 4K库: %q %v %s %s", cat, err, c4k.libCid(), c4k.libAbs)
	}
	rec := &model.OrganizeRecord{Source: "x", TargetDir: "动漫番剧/X", Status: "success"}
	sink.note(rec)
	if rec.LibCid != "lib4k" || !rec.LibBySource {
		t.Fatalf("记录要带上来源库与标记: %+v", rec)
	}
	// 子目录绑主库：规则写了动漫库也进主库，记录 LibCid 空但标记来源
	cm := sub("submain")
	cm.enterLib("")
	if _, err := cm.classify(anime); err != nil || cm.libCid() != "lib" {
		t.Fatalf("绑主库: %v %s", err, cm.libCid())
	}
	rec2 := &model.OrganizeRecord{Source: "y", TargetDir: "动漫番剧/Y", Status: "success"}
	sink.note(rec2)
	if rec2.LibCid != "" || !rec2.LibBySource {
		t.Fatalf("主库来源记录: %+v", rec2)
	}
	// 下一个条目复位：根目录的条目不能沿用来源标记
	root.enterLib("")
	if sink.bySource {
		t.Fatal("enterLib 复位时要清掉来源标记")
	}
	// 绑定的库已移出配置：报错
	cg := sub("subgone")
	cg.enterLib("")
	if _, err := cg.classify(anime); err == nil || !strings.Contains(err.Error(), "旧") {
		t.Fatalf("失效绑定必须报错: %v", err)
	}
	if cg.libCid() != "lib" {
		t.Fatal("报错时不该切库")
	}
}

// 重新整理选库：按来源定的留在原库；没入过库的看来源子目录；其余按规则
func TestRedoLibOf(t *testing.T) {
	newTestDB(t, "stagingsub_redo.db")
	resetClassifyCache()
	t.Cleanup(resetClassifyCache)
	sink := &orgSink{libName: "影视", libResolved: true, curNames: map[string]string{"lib2": "动漫库", "lib4k": "4K库"}, jobs: map[string]scrapeJob{}}
	cfg := &OrgConfig{Library: "lib", OtherLibs: []string{"lib2", "lib4k"},
		SubLibs: map[string]stagingLib{"sub4k": {Cid: "sub4k", Lib: "lib4k"}}}
	cases := []struct {
		name   string
		rec    model.OrganizeRecord
		rule   string
		want   string
		source bool
	}{
		{"来源库留原处（子目录已解绑也一样）", model.OrganizeRecord{TargetDir: "a", LibCid: "lib4k", LibBySource: true, SourceCid: "gone"}, "动漫库", "lib4k", true},
		{"来源是主库也留原处", model.OrganizeRecord{TargetDir: "a", LibBySource: true}, "动漫库", "lib", true},
		{"按规则入库的照规则改库", model.OrganizeRecord{TargetDir: "a", LibCid: "lib4k"}, "动漫库", "lib2", false},
		{"没入过库、来自绑定子目录", model.OrganizeRecord{SourceCid: "sub4k"}, "动漫库", "lib4k", true},
		{"没入过库、子目录已解绑", model.OrganizeRecord{SourceCid: "gone"}, "", "lib", false},
	}
	for _, c := range cases {
		cid, bySource, err := redoLibOf(cfg, sink, &c.rec, c.rule)
		if err != nil || cid != c.want || bySource != c.source {
			t.Fatalf("%s: %s %v %v", c.name, cid, bySource, err)
		}
	}
}

// 子目录是工作区根：不当条目、不被空目录清理收掉
func TestStagingSubdirProtected(t *testing.T) {
	cfg := &OrgConfig{Library: "lib", Pending: "stage", SubLibs: map[string]stagingLib{"sub4k": {Cid: "sub4k"}}}
	if !cfg.workspaceRootCids()["sub4k"] {
		t.Fatal("绑定子目录应是工作区根")
	}
	found := false
	for _, c := range orgProtectedCids(cfg) {
		found = found || c == "sub4k"
	}
	if !found {
		t.Fatal("绑定子目录应受空目录清理保护")
	}
}

// 守望者：绑定子目录本身不算条目，算它里面的（空子目录不能让守望者一直以为没清空）
func TestCountStagingUnheld(t *testing.T) {
	newTestDB(t, "stagingsub_count.db")
	subs := map[string]stagingLib{"sub4k": {Cid: "sub4k"}}
	entries := []map[string]interface{}{
		{"f": "0", "cid": "sub4k", "n": "4K"},
		{"f": "1", "fid": "f1", "n": "a.mkv"},
	}
	inner := map[string][]map[string]interface{}{"sub4k": {}}
	list := func(cid string) ([]map[string]interface{}, error) { return inner[cid], nil }
	if n, err := countStagingUnheld(list, entries, subs); err != nil || n != 1 {
		t.Fatalf("空子目录不计数: %d %v", n, err)
	}
	inner["sub4k"] = []map[string]interface{}{{"f": "0", "cid": "d1", "n": "沙丘"}, {"f": "1", "fid": "f2"}}
	if n, _ := countStagingUnheld(list, entries, subs); n != 3 {
		t.Fatalf("子目录里的条目要计数: %d", n)
	}
	if n := stagingTopCount(entries, subs); n != 1 {
		t.Fatalf("秒传试探不算子目录本身: %d", n)
	}
}

// 保存校验：直接子目录、库在配置里、不重复、不能是转存目录本身；没变的不问 115
func TestNormStagingLibs(t *testing.T) {
	asked := map[string]int{}
	dirs := map[string]dirInfo{
		"sub4k": {cid: "sub4k", pid: "stage", n: "4K"},
		"deep":  {cid: "deep", pid: "sub4k", n: "杜比"},
	}
	dirOf := func(cid string) (dirInfo, error) {
		asked[cid]++
		if d, ok := dirs[cid]; ok {
			return d, nil
		}
		return dirInfo{}, errDirGone
	}
	libs := []string{"lib", "lib4k"}
	out, err := normStagingLibs([]stagingLib{{Cid: "sub4k", Lib: "lib4k"}}, "stage", libs, nil, dirOf)
	if err != nil || len(out) != 1 || out[0].Name != "4K" {
		t.Fatalf("正常保存: %+v %v", out, err)
	}
	// 没变的子目录不再问 115
	if _, err := normStagingLibs(out, "stage", libs, stagingLibMap(out), dirOf); err != nil || asked["sub4k"] != 1 {
		t.Fatalf("没变的不该再查: %v %v", err, asked)
	}
	bad := []struct {
		name string
		in   []stagingLib
	}{
		{"多层", []stagingLib{{Cid: "deep", Lib: "lib"}}},
		{"库不在配置", []stagingLib{{Cid: "sub4k", Lib: "nope"}}},
		{"重复", []stagingLib{{Cid: "sub4k", Lib: "lib"}, {Cid: "sub4k", Lib: "lib4k"}}},
		{"转存目录本身", []stagingLib{{Cid: "stage", Lib: "lib"}}},
		{"读不到", []stagingLib{{Cid: "ghost", Lib: "lib"}}},
	}
	for _, b := range bad {
		if _, err := normStagingLibs(b.in, "stage", libs, nil, dirOf); err == nil {
			t.Fatalf("%s 应拒绝", b.name)
		}
	}
	if _, err := normStagingLibs(nil, "", libs, nil, dirOf); err == nil {
		t.Fatal("没配转存目录应拒绝")
	}
}

// 订阅的包装目录：订阅自己选的 > 订阅设置默认 > 转存目录本身；解绑的退回根
func TestSubRunnerTargetOf(t *testing.T) {
	r := &subRunner{target: "stage", subs: map[string]stagingLib{"sub4k": {Cid: "sub4k"}}}
	cases := []struct {
		sub, def, want string
	}{
		{"", "", "stage"},
		{"sub4k", "", "sub4k"},
		{"", "sub4k", "sub4k"},
		{"root", "sub4k", "stage"},
		{"gone", "", "stage"},
	}
	for _, c := range cases {
		r.cfg.Subdir = c.def
		if got := r.targetOf(&model.Subscription{Subdir: c.sub}); got != c.want {
			t.Fatalf("sub=%q def=%q: 实得 %s，预期 %s", c.sub, c.def, got, c.want)
		}
	}
}

// 提交时选子目录：只认绑定表里的；空 / root 交给转存核心函数回落转存目录
func TestStagingTarget(t *testing.T) {
	newTestDB(t, "stagingsub_target.db")
	model.DB.Create(&model.Setting{Key: stagingLibsKey, Value: `[{"cid":"sub4k","name":"4K","lib":"lib4k"}]`})
	h := &Handler{}
	for in, want := range map[string]string{"": "", "root": "", "sub4k": "sub4k"} {
		if got, err := h.stagingTarget(in); err != nil || got != want {
			t.Fatalf("%q → %q %v，预期 %q", in, got, err, want)
		}
	}
	if _, err := h.stagingTarget("gone"); err == nil {
		t.Fatal("不在绑定表里的子目录要报错（RE0 解锁之前就得拦下）")
	}
}
