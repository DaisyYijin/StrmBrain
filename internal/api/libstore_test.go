package api

import (
	"strings"
	"testing"

	"115-station/internal/config"
	"115-station/internal/model"
)

// 媒体库表与迁移（libstore.go）、分类方案（catscheme.go）

func TestPlanLibrariesV2(t *testing.T) {
	in := libsV2In{
		FullRaw:    `{"cid":"100","cid_path":"/影视","extra_libs":[{"cid":"200","path":"/动漫"}]}`,
		OptsRaw:    `{"libs":{"100":{"local":"影视","scrape":"auto"},"200":{"mode":"self","depth":3,"local":"动漫"},"a2:9":{"scrape":"off"}}}`,
		AcctRaw:    `[{"account":2,"cid":"9","path":"/仓库","local":"副号"}]`,
		StagingRaw: `[{"cid":"s1","name":"4K","lib":"200"}]`,
		ShareRaw:   `{"folder":"301"}`,
		YAML:       "movie:\n  电影/动画电影:\n    genre_ids: '16'\n    library: 动漫\n",
		Legacy:     "manual",
		SchemeID:   7,
		shown:      func(cid string) string { return map[string]string{"301": "/转存"}[cid] },
	}
	libs, lines, unresolved := planLibrariesV2(in)
	if len(unresolved) != 0 || len(libs) != 3 {
		t.Fatalf("应换算出 3 个库：%+v %v", libs, unresolved)
	}
	main, anime, extra := libs[0], libs[1], libs[2]
	if main.Local != "影视" || main.Cid != "100" || main.Path != "/影视" || main.SchemeID != 7 || main.Scrape != "auto" || main.Account != 0 {
		t.Errorf("主库：%+v", main)
	}
	if anime.Local != "动漫" || anime.SchemeID != 0 || anime.Depth != 3 || anime.Scrape != "manual" {
		t.Errorf("原来自己整理的库：不分类、层级照旧、刮削按老开关：%+v", anime)
	}
	if extra.Account != 2 || extra.Local != "副号" || extra.Scrape != "off" {
		t.Errorf("附加账号的库：%+v", extra)
	}
	for _, l := range libs {
		if l.StagingCid != "" {
			t.Errorf("转存文件夹不自动设（要用户重设）：%+v", l)
		}
	}
	text := strings.Join(lines, "\n")
	for _, want := range []string{"/转存", "「4K」整理进「动漫」", "library: 动漫", "方案「默认」"} {
		if !strings.Contains(text, want) {
			t.Errorf("迁移提示里应提到 %q：\n%s", want, text)
		}
	}

	// 名字认不出的库这次不迁
	in.OptsRaw = `{}`
	in.name = func(cid string) string {
		if cid == "100" {
			return "影视"
		}
		return ""
	}
	libs, _, unresolved = planLibrariesV2(in)
	if len(unresolved) != 1 || unresolved[0] != "200" || len(libs) != 2 {
		t.Fatalf("认不出名字的不迁：%+v %v", libs, unresolved)
	}
}

func TestValidateLibraries(t *testing.T) {
	ok := []model.Library{
		{Name: "电影", Cid: "1", Path: "/影视/电影", Local: "电影/主号", StagingCid: "s1", StagingPath: "/转存/电影"},
		{Name: "电影·副号", Account: 2, Cid: "1", Path: "/影视/电影", Local: "电影/副号"},
		{Name: "剧集", Cid: "2", Path: "/影视/剧集", Local: "剧集", StagingCid: "s2", StagingPath: "/转存/剧集"},
	}
	if err := validateLibraries(ok); err != nil {
		t.Fatalf("合法的一组：%v", err)
	}
	bad := map[string][]model.Library{
		"没名称":     {{Cid: "1", Local: "a"}},
		"重名":      {{Name: "电影", Cid: "1", Local: "a"}, {Name: "电影", Cid: "2", Local: "b"}},
		"网盘根":     {{Name: "a", Cid: "0", Local: "a"}},
		"同一目录":    {{Name: "a", Cid: "1", Local: "a"}, {Name: "b", Cid: "1", Local: "b"}},
		"本地包含":    {{Name: "a", Cid: "1", Local: "电影"}, {Name: "b", Cid: "2", Local: "电影/副号"}},
		"本地大小写":   {{Name: "a", Cid: "1", Local: "Movies"}, {Name: "b", Cid: "2", Local: "movies"}},
		"本地非法":    {{Name: "a", Cid: "1", Local: "a:b"}},
		"网盘包含":    {{Name: "a", Cid: "1", Path: "/影视", Local: "a"}, {Name: "b", Cid: "2", Path: "/影视/电影", Local: "b"}},
		"转存在库里":   {{Name: "a", Cid: "1", Path: "/影视", Local: "a", StagingCid: "s", StagingPath: "/影视/转存"}},
		"转存在别的库里": {{Name: "a", Cid: "1", Path: "/影视", Local: "a"}, {Name: "b", Cid: "2", Path: "/动漫", Local: "b", StagingCid: "s", StagingPath: "/影视/转存"}},
		"两库同一转存":  {{Name: "a", Cid: "1", Local: "a", StagingCid: "s"}, {Name: "b", Cid: "2", Local: "b", StagingCid: "s"}},
		"转存是库本身":  {{Name: "a", Cid: "1", Local: "a", StagingCid: "1"}},
		"层级越界":    {{Name: "a", Cid: "1", Local: "a", Depth: 4}},
	}
	for name, libs := range bad {
		if err := validateLibraries(libs); err == nil {
			t.Errorf("%s 应该拒绝", name)
		}
	}
	// 不同账号的同名网盘路径不算包含
	cross := []model.Library{{Name: "a", Cid: "1", Path: "/影视", Local: "a"}, {Name: "b", Account: 3, Cid: "1", Path: "/影视/电影", Local: "b"}}
	if err := validateLibraries(cross); err != nil {
		t.Fatalf("不同账号的网盘路径互不相干：%v", err)
	}
}

func TestEnsureDefaultSchemeFromSeed(t *testing.T) {
	newTestDB(t, "scheme_seed.db")
	if err := model.InitDefaultCategories(model.DB); err != nil {
		t.Fatal(err)
	}
	id, err := ensureDefaultScheme(model.DB)
	if err != nil || id == 0 {
		t.Fatalf("应建出方案「默认」：%d %v", id, err)
	}
	var left int64
	model.DB.Model(&model.CategoryRule{}).Where("scheme_id = 0").Count(&left)
	if left != 0 {
		t.Fatalf("规则行都要归到方案名下，还剩 %d 行", left)
	}
	s, _ := schemeByID(id)
	rows, err := parseCategoryYAML(s.Config)
	var seed []model.CategoryRule
	model.DB.Where("scheme_id = ?", id).Order("media_type, priority").Find(&seed)
	if err != nil || len(rows) != len(seed) {
		t.Fatalf("没存过 YAML 时按种子规则生成，规则条数应一致：%d / %d %v\n%s", len(rows), len(seed), err, s.Config)
	}
	got := map[string]string{}
	for _, r := range rows {
		got[r.MediaType+r.Name] = r.GenreIds + "|" + r.OriginCountry + "|" + r.OriginalLanguage
	}
	for _, r := range seed {
		if got[r.MediaType+r.Name] != r.GenreIds+"|"+r.OriginCountry+"|"+r.OriginalLanguage {
			t.Errorf("规则 %s 的条件没写对：%q", r.Name, got[r.MediaType+r.Name])
		}
	}
	if again, _ := ensureDefaultScheme(model.DB); again != id {
		t.Fatal("幂等")
	}
}

func TestEnsureDefaultSchemeStripsLibrary(t *testing.T) {
	newTestDB(t, "scheme_yaml.db")
	src := "movie:\n  电影/动画电影:\n    genre_ids: '16'\n    library: 动漫\n  电影/外语电影:\n"
	model.DB.Create(&model.ScrapeRule{Type: "category_config", Enabled: true, Config: src})
	id, err := ensureDefaultScheme(model.DB)
	if err != nil {
		t.Fatal(err)
	}
	s, _ := schemeByID(id)
	if strings.Contains(s.Config, "library") || !strings.Contains(s.Config, "genre_ids") {
		t.Fatalf("方案里去掉 library、其余照旧：\n%s", s.Config)
	}
	var rule model.ScrapeRule
	model.DB.Where("type = ?", "category_config").First(&rule)
	if rule.Config != src {
		t.Fatal("老的 category_config 原样留着（回退用）")
	}
}

func TestReplaceSchemeRulesIsolated(t *testing.T) {
	newTestDB(t, "scheme_iso.db")
	a := model.CategoryScheme{Name: "A"}
	b := model.CategoryScheme{Name: "B"}
	model.DB.Create(&a)
	model.DB.Create(&b)
	_ = replaceSchemeRules(model.DB, a.ID, []model.CategoryRule{{MediaType: "movie", Name: "电影"}})
	_ = replaceSchemeRules(model.DB, b.ID, []model.CategoryRule{{MediaType: "tv", Name: "剧集"}, {MediaType: "movie", Name: "电影2"}})
	_ = replaceSchemeRules(model.DB, a.ID, []model.CategoryRule{{MediaType: "movie", Name: "电影新"}})
	var nb int64
	model.DB.Model(&model.CategoryRule{}).Where("scheme_id = ?", b.ID).Count(&nb)
	if nb != 2 {
		t.Fatalf("改方案 A 不该动方案 B 的规则：B 还剩 %d 行", nb)
	}
	var ra []model.CategoryRule
	model.DB.Where("scheme_id = ?", a.ID).Find(&ra)
	if len(ra) != 1 || ra[0].Name != "电影新" {
		t.Fatalf("方案 A 整套替换：%+v", ra)
	}
}

func TestMigrateLibrariesV2(t *testing.T) {
	newTestDB(t, "libmig.db")
	cfg := &config.Config{DataDir: t.TempDir(), ConfigDir: t.TempDir()}
	prev := notifyConfigSource
	notifyConfigSource = cfg
	t.Cleanup(func() { notifyConfigSource = prev; forgetLibraries(); forgetSchemes() })
	h := &Handler{DB: model.DB, Config: cfg}
	_ = model.InitDefaultCategories(model.DB)
	_ = cfg.SaveSetting("full", `{"cid":"100","cid_path":"/影视","extra_libs":[{"cid":"200","path":"/动漫"}]}`)
	_ = cfg.SaveSetting(libOptsKey, `{"libs":{"100":{"local":"影视"},"200":{"local":"动漫"}}}`)

	h.MigrateLibrariesV2()
	libs := loadLibraries()
	if len(libs) != 2 || libs[0].Local != "影视" || libs[1].Local != "动漫" || libs[0].SchemeID == 0 {
		t.Fatalf("迁移结果：%+v", libs)
	}
	if !strings.Contains(cfg.GetSetting(libsV2NoticeKey), "转存") {
		t.Fatal("应留下给用户看的提示")
	}
	// 幂等：再跑一次不重复
	h.MigrateLibrariesV2()
	forgetLibraries()
	if n := len(loadLibraries()); n != 2 {
		t.Fatalf("重复迁移：%d 个库", n)
	}
}
