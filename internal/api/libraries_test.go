package api

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"115-station/internal/model"
)

// ---- Emby 覆盖（C6） ----

// 不分类、片目在第二层的库（爱优腾芒/电影、爱优腾芒/剧集）：拿本地子目录当分类，各自进了 Emby 就算覆盖
func TestLocalSubdirsCoverage(t *testing.T) {
	root := filepath.ToSlash(t.TempDir())
	lib := root + "/爱优腾芒"
	for _, d := range []string{"电影", "剧集", ".actors"} {
		if err := os.MkdirAll(lib+"/"+d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(lib+"/readme.txt", nil, 0o644); err != nil {
		t.Fatal(err)
	}
	cats := localSubdirs(lib)
	if len(cats) != 2 {
		t.Fatalf("子目录 = %v，要 电影 / 剧集（跳过 .actors 与文件）", cats)
	}
	st := libEmbyCoverage("", []string{lib + "/电影", lib + "/剧集"}, lib, cats)
	if !st.Covered || st.Partial {
		t.Fatalf("两个分类都进了 Emby 应算覆盖：%+v", st)
	}
	st = libEmbyCoverage("", []string{lib + "/电影"}, lib, cats)
	if st.Covered || len(st.Missing) != 1 || st.Missing[0] != lib+"/剧集" {
		t.Fatalf("只进了电影应报缺剧集：%+v", st)
	}
}

func TestLibEmbyCoverage(t *testing.T) {
	cases := []struct {
		name, mapping, lib string
		locs               []string
		cats               []string
		want               libEmbyState
	}{
		{"没配映射，Emby 库就是本地根", "", "/media/动漫", []string{"/media"}, nil,
			libEmbyState{Covered: true}},
		{"没配映射，没有覆盖", "", "/media/动漫", []string{"/media/影视"}, nil,
			libEmbyState{Suggest: "/media/动漫"}},
		{"只覆盖了下面一部分", "", "/media/动漫", []string{"/media/动漫/电视剧/日番"}, nil,
			libEmbyState{Partial: true, Suggest: "/media/动漫"}},
		{"映射后覆盖", "/media#/mnt/emby", "/media/动漫", []string{"/mnt/emby"}, nil,
			libEmbyState{Covered: true, Suggest: "/mnt/emby/动漫"}},
		{"映射后没覆盖，给出 Emby 路径", "/media#/mnt/emby", "/media/动漫", []string{"/mnt/emby/影视"}, nil,
			libEmbyState{Suggest: "/mnt/emby/动漫"}},
		{"映射本地一侧只填了一个库：别的库换算不回来", "/media/影视#/mnt/emby", "/media/动漫", []string{"/mnt/emby"}, nil,
			libEmbyState{Unmapped: true}},
		{"前缀相同不算覆盖", "", "/media/动漫2", []string{"/media/动漫"}, nil,
			libEmbyState{Suggest: "/media/动漫2"}},
		{"按分类各建一个 Emby 库，分类全覆盖", "", "/media/影视", []string{"/media/影视/电影", "/media/影视/剧集", "/media/影视/动漫番剧"},
			[]string{"电影", "剧集", "动漫番剧"}, libEmbyState{Covered: true}},
		{"多级分类被上一层覆盖", "", "/media/影视", []string{"/media/影视/电影"},
			[]string{"电影/华语", "电影/外语"}, libEmbyState{Covered: true}},
		{"按分类建库，漏了一个分类", "", "/media/影视", []string{"/media/影视/电影", "/media/影视/剧集"},
			[]string{"电影", "剧集", "动漫番剧"}, libEmbyState{Partial: true, Missing: []string{"/media/影视/动漫番剧"}, Suggest: "/media/影视"}},
		{"映射后按分类建库，缺的给 Emby 路径", "/media#/mnt/emby", "/media/影视", []string{"/mnt/emby/影视/电影"},
			[]string{"电影", "剧集"}, libEmbyState{Partial: true, Missing: []string{"/mnt/emby/影视/剧集"}, Suggest: "/mnt/emby/影视"}},
		{"Emby 库比分类还深，分类不算覆盖", "", "/media/影视", []string{"/media/影视/电影/华语"},
			[]string{"电影"}, libEmbyState{Partial: true, Missing: []string{"/media/影视/电影"}, Suggest: "/media/影视"}},
	}
	for _, c := range cases {
		if got := libEmbyCoverage(c.mapping, c.locs, c.lib, c.cats); !reflect.DeepEqual(got, c.want) {
			t.Errorf("%s：得到 %+v，应为 %+v", c.name, got, c.want)
		}
	}
}

// ---- 名字与改名（R4） ----

func TestLibNameSourceAndRenameFix(t *testing.T) {
	renamed := libNameSource{Cid: "200", Shown: "/动画", CachedOK: true, Cached: model.PathCache{Name: "动漫", Path: "/动漫"}}
	if r := renamed.libRootOf(); r.Name != "动漫" || r.Abs != "/动漫" {
		t.Errorf("缓存优先：%+v", r)
	}
	if renamed.staleName() != "动画" {
		t.Errorf("显示路径的目录名与 115 现在的不同，应报旧名")
	}
	noCache := libNameSource{Cid: "300", Shown: "/4K/"}
	if r := noCache.libRootOf(); r.Name != "4K" || r.Abs != "/4K" {
		t.Errorf("缓存没有时退回配置里的显示路径：%+v", r)
	}
	if noCache.staleName() != "" {
		t.Error("没有缓存时下不了「改过名」的结论")
	}
	if (libNameSource{Cid: "400"}).libRootOf().Name != "" {
		t.Error("都没有就空着")
	}
	fixes := libRenameFixes([]ruleLibRef{{Library: "动画"}, {Library: "动画"}, {Library: "不存在"}}, []libNameSource{renamed, noCache})
	if len(fixes) != 1 || fixes[0] != (libRenameFix{From: "动画", To: "动漫", Lib: "200"}) {
		t.Errorf("改名修复：%+v", fixes)
	}
}

// ---- 汇总视图（C1） ----

func TestBuildLibrariesView(t *testing.T) {
	sources := []libNameSource{
		{Cid: "100", Shown: "/影视"},
		{Cid: "200", Shown: "/动漫"},
		{Cid: "300", Shown: "/4K"},
	}
	rows := []model.CategoryRule{
		{MediaType: "movie", Name: "电影/动画电影", Library: "动漫"},
		{MediaType: "movie", Name: "电影/外语电影"},
		{MediaType: "tv", Name: "电视剧/国漫", Library: "动画"},
	}
	bindings := []stagingLib{{Cid: "s1", Name: "4K", Lib: "300"}, {Cid: "s2", Name: "旧", Lib: "999"}}
	v := buildLibrariesView(sources, rows, bindings, "root", "/media", "", []string{"/media/影视"}, libOpts{})
	if !v.NamesKnown || len(v.Libs) != 3 {
		t.Fatalf("视图：%+v", v)
	}
	if !v.Libs[0].Default || len(v.Libs[0].Rules) != 1 || v.Libs[0].Rules[0].Name != "电影/外语电影" {
		t.Errorf("默认库：%+v", v.Libs[0])
	}
	if len(v.Libs[1].Rules) != 1 || v.Libs[1].Local != "/media/动漫" {
		t.Errorf("动漫库：%+v", v.Libs[1])
	}
	if len(v.Libs[2].Inboxes) != 1 || v.Libs[2].Inboxes[0].Name != "4K" {
		t.Errorf("4K 库的入口：%+v", v.Libs[2].Inboxes)
	}
	if len(v.OrphanRules) != 1 || v.OrphanRules[0].Library != "动画" {
		t.Errorf("认不出的规则：%+v", v.OrphanRules)
	}
	if v.Libs[0].Emby == nil || !v.Libs[0].Emby.Covered || v.Libs[1].Emby.Covered {
		t.Errorf("Emby 覆盖：%+v %+v", v.Libs[0].Emby, v.Libs[1].Emby)
	}
	// 名字认不全：按名字写的规则不报「认不出」
	v2 := buildLibrariesView([]libNameSource{{Cid: "100", Shown: "/影视"}, {Cid: "200"}}, rows, nil, "", "/media", "", nil, libOpts{})
	if v2.NamesKnown || len(v2.OrphanRules) != 0 || v2.Libs[0].Emby != nil {
		t.Errorf("名字认不全时不下结论、没读 Emby 时不给覆盖：%+v", v2)
	}
}

// ---- 保存规划（C2 / C4 / C5） ----

const planFull = `{"cid":"100","cid_path":"/影视","local_path":"/media","mode":"fast","extra_libs":[{"cid":"200","path":"/动漫"},{"cid":"300","path":"/4K"}]}`

func planInput() libSaveInput {
	return libSaveInput{
		FullRaw:      planFull,
		OldLibs:      []libRoot{{Cid: "100", Name: "影视"}, {Cid: "200", Name: "动漫"}, {Cid: "300", Name: "4K"}},
		StagingRoot:  "root",
		OldBindings:  []stagingLib{{Cid: "s4k", Name: "4K", Lib: "300"}},
		CategoryYAML: "movie:\n  电影/动画电影:\n    genre_ids: '16'\n    library: 动漫\n  电影/外语电影:\ntv:\n  电视剧/日番:\n    library: '200'\n",
	}
}

// fakeDeps 转存目录下有「4K」（已绑定）、「纪录片」（5 项）与「临时」（空）三个子目录
func fakeDeps(t *testing.T) (libSaveDeps, *int) {
	calls := 0
	return libSaveDeps{
		listRoot: func() ([]dirEntry, error) {
			calls++
			return []dirEntry{{Name: "4K", IsDir: true, Cid: "s4k"}, {Name: "纪录片", IsDir: true, Cid: "sdoc"}, {Name: "临时", IsDir: true, Cid: "stmp"}, {Name: "a.mkv"}}, nil
		},
		countItems: func(cid string) (int, error) {
			return map[string]int{"sdoc": 5, "stmp": 0}[cid], nil
		},
		dirOf: func(cid string) (dirInfo, error) {
			names := map[string]string{"sdoc": "纪录片", "stmp": "临时", "elsewhere": "别处"}
			pid := "root"
			if cid == "elsewhere" {
				pid = "x"
			}
			if n, ok := names[cid]; ok {
				return dirInfo{cid: cid, pid: pid, n: n}, nil
			}
			t.Fatalf("不该查 %s", cid)
			return dirInfo{}, nil
		},
	}, &calls
}

func keepAll() []libRootEntry {
	return []libRootEntry{{Cid: "200", Path: "/动漫"}, {Cid: "300", Path: "/4K"}}
}

func TestPlanLibrariesSaveNoChange(t *testing.T) {
	deps, calls := fakeDeps(t)
	plan, confirms, err := planLibrariesSave(planInput(), libSaveReq{ExtraLibs: keepAll(),
		Inboxes: []libInboxReq{{Cid: "s4k", Name: "4K", Lib: "300"}}}, deps)
	if err != nil || confirms != nil {
		t.Fatalf("err=%v confirms=%v", err, confirms)
	}
	if plan.FullChanged || plan.YAMLChanged || len(plan.Removed) != 0 || len(plan.Unbound) != 0 || *calls != 0 {
		t.Errorf("什么都没改：%+v，列转存目录 %d 次", plan, *calls)
	}
	var full map[string]any
	_ = json.Unmarshal([]byte(plan.FullJSON), &full)
	if full["mode"] != "fast" || full["cid_path"] != "/影视" {
		t.Errorf("full 的其他字段要原样保留：%s", plan.FullJSON)
	}
}

func TestPlanLibrariesSaveInboxes(t *testing.T) {
	deps, calls := fakeDeps(t)
	extras := append(keepAll(), libRootEntry{Cid: "400", Path: "/纪录片"})
	// 新库 + 同名已存在且不空的子目录 → 要确认
	_, confirms, err := planLibrariesSave(planInput(), libSaveReq{ExtraLibs: extras,
		Inboxes: []libInboxReq{{Cid: "s4k", Name: "4K", Lib: "300"}, {Name: "纪录片", Lib: "400"}}}, deps)
	if err != nil || len(confirms) != 1 || confirms[0].Items != 5 {
		t.Fatalf("同名非空要确认：err=%v confirms=%+v", err, confirms)
	}
	// 确认后复用它；另一个新名字要新建；空的同名目录直接复用
	plan, confirms, err := planLibrariesSave(planInput(), libSaveReq{ExtraLibs: extras,
		Inboxes: []libInboxReq{
			{Cid: "s4k", Name: "4K", Lib: "300"},
			{Name: "纪录片", Lib: "400", Confirm: true},
			{Name: "临时", Lib: "200"},
			{Name: "动漫", Lib: "200"},
		}}, deps)
	if err != nil || confirms != nil {
		t.Fatalf("err=%v confirms=%v", err, confirms)
	}
	if !plan.FullChanged || *calls != 2 {
		t.Errorf("加了库应算改动；转存目录每次规划只列一次（两次规划共 2 次），实际 %d", *calls)
	}
	got := map[string]string{}
	for _, b := range plan.Bindings {
		got[b.Name] = b.Cid + "@" + b.Lib
	}
	want := map[string]string{"4K": "s4k@300", "纪录片": "sdoc@400", "临时": "stmp@200", "动漫": "@200"}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("入口 %s：得到 %q，应为 %q（全部：%v）", k, got[k], v, got)
		}
	}
}

func TestPlanLibrariesSaveRemove(t *testing.T) {
	deps, _ := fakeDeps(t)
	// 移除动漫库：两条指向它的规则（写名字的、写 cid 的）回默认库；4K 入口解绑；清本地
	plan, _, err := planLibrariesSave(planInput(), libSaveReq{ExtraLibs: []libRootEntry{{Cid: "300", Path: "/4K"}},
		CleanLocal: []string{"200"}}, deps)
	if err != nil {
		t.Fatal(err)
	}
	if len(plan.Removed) != 1 || plan.Removed[0].Cid != "200" || !plan.YAMLChanged {
		t.Fatalf("移除：%+v", plan)
	}
	rows := parseOrFail(t, plan.YAML)
	for _, r := range rows {
		if r.Library != "" {
			t.Errorf("指向已移除库的 library 应删掉：%+v", r)
		}
	}
	if !plan.Unbound["s4k"] {
		t.Error("请求里没带的入口算解绑")
	}
	if len(plan.Clean) != 1 || plan.Clean[0] != "动漫" {
		t.Errorf("清本地：%v", plan.Clean)
	}
	// 只能清这次移除的
	if _, _, err := planLibrariesSave(planInput(), libSaveReq{ExtraLibs: keepAll(), CleanLocal: []string{"200"}}, deps); err == nil {
		t.Error("没移除的库不能清本地")
	}
	// 移除的库名字读不出来：不知道 YAML 里哪些指向它，不保存
	in := planInput()
	in.OldLibs[1].Name = ""
	if _, _, err := planLibrariesSave(in, libSaveReq{ExtraLibs: []libRootEntry{{Cid: "300"}}}, deps); err == nil {
		t.Error("移除的库名字读不出时应拒绝")
	}
}

func TestPlanLibrariesSaveRejects(t *testing.T) {
	deps, _ := fakeDeps(t)
	bad := []struct {
		name string
		in   libSaveInput
		req  libSaveReq
	}{
		{"附加库选了默认库", planInput(), libSaveReq{ExtraLibs: []libRootEntry{{Cid: "100"}}}},
		{"附加库选了网盘根", planInput(), libSaveReq{ExtraLibs: []libRootEntry{{Cid: "0"}}}},
		{"附加库没选目录", planInput(), libSaveReq{ExtraLibs: []libRootEntry{{Cid: " "}}}},
		{"入口指向不在这次保存里的库", planInput(), libSaveReq{ExtraLibs: keepAll(), Inboxes: []libInboxReq{{Name: "x", Lib: "999"}}}},
		{"入口名带斜杠", planInput(), libSaveReq{ExtraLibs: keepAll(), Inboxes: []libInboxReq{{Name: "a/b", Lib: "200"}}}},
		{"两个入口同名", planInput(), libSaveReq{ExtraLibs: keepAll(), Inboxes: []libInboxReq{{Name: "新", Lib: "200"}, {Name: "新", Lib: "300"}}}},
		{"同一个子目录绑两次", planInput(), libSaveReq{ExtraLibs: keepAll(), Inboxes: []libInboxReq{{Cid: "s4k", Lib: "300"}, {Name: "4K", Lib: "200"}}}},
		{"已有子目录不在转存目录下", planInput(), libSaveReq{ExtraLibs: keepAll(), Inboxes: []libInboxReq{{Cid: "elsewhere", Lib: "200"}}}},
		{"没配转存目录", func() libSaveInput { in := planInput(); in.StagingRoot = ""; return in }(),
			libSaveReq{ExtraLibs: keepAll(), Inboxes: []libInboxReq{{Name: "x", Lib: "200"}}}},
		{"没配默认库", func() libSaveInput { in := planInput(); in.FullRaw = `{}`; return in }(), libSaveReq{}},
	}
	for _, b := range bad {
		if _, _, err := planLibrariesSave(b.in, b.req, deps); err == nil {
			t.Errorf("%s：应拒绝", b.name)
		}
	}
}

// 列转存目录失败：原因原样报给用户，不当成「没有同名目录」去新建
func TestPlanLibrariesSaveListErr(t *testing.T) {
	deps, _ := fakeDeps(t)
	deps.listRoot = func() ([]dirEntry, error) { return nil, errors.New("风控") }
	if _, _, err := planLibrariesSave(planInput(), libSaveReq{ExtraLibs: keepAll(), Inboxes: []libInboxReq{{Name: "新", Lib: "200"}}}, deps); err == nil || !strings.Contains(err.Error(), "风控") {
		t.Errorf("列转存目录失败要报出来：%v", err)
	}
}

func TestSubscribeCfgWithoutSubdir(t *testing.T) {
	out, ok := subscribeCfgWithoutSubdir(`{"subdir":"s4k","interval":60}`, map[string]bool{"s4k": true})
	if !ok || !strings.Contains(out, `"subdir":""`) || !strings.Contains(out, `"interval":60`) {
		t.Errorf("解绑的默认子目录清空、其余字段保留：%s", out)
	}
	if _, ok := subscribeCfgWithoutSubdir(`{"subdir":"other"}`, map[string]bool{"s4k": true}); ok {
		t.Error("没解绑的不动")
	}
}

// ---- 保存二级分类的改库确认（C7） ----

func TestCategoryLibChanges(t *testing.T) {
	libs := []libRoot{{Cid: "100", Name: "影视"}, {Cid: "200", Name: "动漫"}}
	entries := map[string]*ledgerTitleEntry{
		"影视/电视剧/日番/A":  {Category: "电视剧/日番", MediaType: "tv", LibName: "影视"},
		"影视/电视剧/日番/B":  {Category: "电视剧/日番", MediaType: "tv", LibName: "影视"},
		"电视剧/日番/C":     {Category: "电视剧/日番", MediaType: "tv"}, // 老台账不带库名，算主库
		"动漫/电影/动画电影/D": {Category: "电影/动画电影", MediaType: "movie", LibName: "动漫"},
	}
	titles := ledgerCategoryTitles(entries, libs)
	old := []model.CategoryRule{
		{MediaType: "tv", Name: "电视剧/日番"},
		{MediaType: "movie", Name: "电影/动画电影", Library: "动漫"},
		{MediaType: "tv", Name: "电视剧/国漫"},
		{MediaType: "tv", Name: "电视剧/欧美剧", Library: "200"},
	}
	next := []model.CategoryRule{
		{MediaType: "tv", Name: "电视剧/日番", Library: "动漫"},      // 换库，有 3 部 → 提示
		{MediaType: "movie", Name: "电影/动画电影", Library: "200"}, // 写法变了、库没变 → 不提示
		{MediaType: "tv", Name: "电视剧/国漫", Library: "动漫"},      // 换库但原库没有片 → 不提示
		{MediaType: "tv", Name: "电视剧/新分类", Library: "动漫"},     // 新规则 → 不提示
		{MediaType: "tv", Name: "电视剧/欧美剧", Library: "动漫"},     // cid 换成名字，同一个库 → 不提示
	}
	got := categoryLibChanges(old, next, libs, titles)
	if len(got) != 1 || got[0] != (categoryLibChange{MediaType: "tv", Name: "电视剧/日番", From: "影视", To: "动漫", Titles: 3}) {
		t.Errorf("改库确认：%+v", got)
	}
}
