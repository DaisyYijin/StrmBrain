package api

import (
	"strings"
	"testing"

	"115-station/internal/model"
)

// 库的本地目录名（liblocal.go）：固化、取名、保存时的规则

func TestLibRootOfPrefersLocal(t *testing.T) {
	s := libNameSource{Cid: "1", CachedOK: true, Cached: model.PathCache{Name: "新名", Path: "/盘/新名"}, Local: "旧名"}
	r := s.libRootOf()
	if r.Name != "旧名" || r.PanName != "新名" || r.Abs != "/盘/新名" {
		t.Fatalf("定过本地名：本地第一层用本地名，115 名只做显示：%+v", r)
	}
	if s.staleName() != "" {
		t.Error("定过本地名的库不该再报「115 上改了名」")
	}
	s.Local = ""
	if r := s.libRootOf(); r.Name != "新名" {
		t.Fatalf("没定过本地名：沿用 115 目录名：%+v", r)
	}
}

func TestPlanLibLocalFreeze(t *testing.T) {
	src := func(cid, cached, shown string) libNameSource {
		s := libNameSource{Cid: cid, Shown: shown}
		if cached != "" {
			s.CachedOK, s.Cached = true, model.PathCache{Name: cached, Path: "/x/" + cached}
		}
		return s
	}
	none := func(string) bool { return false }

	// 缓存里的现名优先；没缓存用显示路径；都没有的不定
	add, warns := planLibLocalFreeze([]libNameSource{src("1", "影视", "/影视"), src("2", "", "/盘2/动漫"), src("3", "", "")}, libOpts{}, none)
	if add["1"] != "影视" || add["2"] != "动漫" || len(add) != 2 || len(warns) != 0 {
		t.Fatalf("取名：%v %v", add, warns)
	}

	// 库在 115 上改过名、改名后还没同步：本地树还在旧名下，定旧名，别挪到一棵空树上
	ledger := func(n string) bool { return n == "电影库" }
	add, _ = planLibLocalFreeze([]libNameSource{src("1", "Movies", "/电影库")}, libOpts{}, ledger)
	if add["1"] != "电影库" {
		t.Fatalf("应挑台账里在用的旧名：%v", add)
	}

	// 定过的不动；新定的和定过的撞名（大小写不敏感）就不定，留老行为
	opts := libOpts{Libs: map[string]libOpt{"1": {Local: "Movies"}}}
	add, warns = planLibLocalFreeze([]libNameSource{src("1", "影视", ""), src("2", "movies", "")}, opts, none)
	if len(add) != 0 || len(warns) != 1 {
		t.Fatalf("撞名不定：%v %v", add, warns)
	}
}

func TestCheckLibLocalName(t *testing.T) {
	for _, ok := range []string{"影视", "4K 电影", "Movies-115"} {
		if err := checkLibLocalName(ok); err != nil {
			t.Errorf("%q 应该合法：%v", ok, err)
		}
	}
	for _, bad := range []string{"", ".", "..", "a/b", `a\b`, "a:b", "a?", "名字."} {
		if checkLibLocalName(bad) == nil {
			t.Errorf("%q 应该拒绝", bad)
		}
	}
}

func TestPlanLibrariesSaveLocals(t *testing.T) {
	deps, _ := fakeDeps(t)
	inLedger := map[string]bool{"影视": true, "动漫": true, "旧库": true}
	deps.ledgerHas = func(n string) bool { return inLedger[n] }
	opts := func(p libSavePlan) libOpts { return parseLibOpts(p.OptsJSON) }

	// 不带 locals：各库按现在在用的名字定下来（老客户端也能把名字固化进去）
	plan, _, err := planLibrariesSave(planInput(), libSaveReq{ExtraLibs: keepAll()}, deps)
	if err != nil {
		t.Fatal(err)
	}
	if o := opts(plan); o.of("100").Local != "影视" || o.of("200").Local != "动漫" || o.of("300").Local != "4K" {
		t.Fatalf("应按现名定下来：%s", plan.OptsJSON)
	}

	// 定过的本地名：带 Opts 整份替换也冲不掉
	in := planInput()
	in.OptsRaw = plan.OptsJSON
	in.OldLibs[1].Name = "动漫" // libRootOf 已经按本地名给出
	again, _, err := planLibrariesSave(in, libSaveReq{ExtraLibs: keepAll(), Opts: map[string]libOpt{}}, deps)
	if err != nil || opts(again).of("200").Local != "动漫" {
		t.Fatalf("Opts 整份替换不该丢本地名：%s %v", again.OptsJSON, err)
	}

	// 台账里没内容的库能改；有内容的不能改
	if p, _, err := planLibrariesSave(in, libSaveReq{ExtraLibs: keepAll(), Locals: map[string]string{"300": "UHD"}}, deps); err != nil || opts(p).of("300").Local != "UHD" {
		t.Fatalf("空库应能改名：%v %s", err, p.OptsJSON)
	}
	if _, _, err := planLibrariesSave(in, libSaveReq{ExtraLibs: keepAll(), Locals: map[string]string{"200": "番剧"}}, deps); err == nil || !strings.Contains(err.Error(), "不能再改") {
		t.Fatalf("有内容的库不能改本地名：%v", err)
	}
	// 和别的库重名（大小写不敏感）、和台账里的残留撞名、名字不合法，都拒绝
	cases := map[string]map[string]string{
		"重名":   {"300": "影视"},
		"残留":   {"300": "旧库"},
		"非法字符": {"300": "a/b"},
	}
	for name, locals := range cases {
		if _, _, err := planLibrariesSave(in, libSaveReq{ExtraLibs: keepAll(), Locals: locals}, deps); err == nil {
			t.Errorf("%s 应该拒绝", name)
		}
	}

	// 新库：两个 115 目录同名也能加，只要本地目录名不同
	extras := append(keepAll(), libRootEntry{Cid: "500", Path: "/盘3/动漫"})
	if _, _, err := planLibrariesSave(in, libSaveReq{ExtraLibs: extras}, deps); err == nil {
		t.Fatal("新库没填本地名、115 目录名又和动漫库撞：应拒绝")
	}
	p, _, err := planLibrariesSave(in, libSaveReq{ExtraLibs: extras, Locals: map[string]string{"500": "动漫-备份"}}, deps)
	if err != nil || opts(p).of("500").Local != "动漫-备份" {
		t.Fatalf("填了不同的本地名应能加：%v %s", err, p.OptsJSON)
	}
}
