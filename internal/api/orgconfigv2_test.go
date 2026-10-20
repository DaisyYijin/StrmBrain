package api

import (
	"strings"
	"testing"

	"115-station/internal/model"
)

// V2 阶段 3：一个库一套整理配置（orgconfigv2.go）

func TestOrgConfigForLibrary(t *testing.T) {
	newTestDB(t, "orgcfg_v2.db")
	model.DB.Create(&model.Setting{Key: "org-basic", Value: `{"existing":"ex1","redundant":"re1","manual_confirm":true}`})
	sch := model.CategoryScheme{Name: "电影分类", Config: "movie: []"}
	model.DB.Create(&sch)
	forgetSchemes()
	t.Cleanup(forgetSchemes)
	h := &Handler{DB: model.DB}
	libs := []model.Library{
		{ID: 1, Name: "电影", Cid: "100", Local: "电影/主号", StagingCid: "s1", SchemeID: sch.ID},
		{ID: 2, Name: "剧集", Cid: "200", Local: "剧集", StagingCid: "s2"},
		{ID: 3, Name: "备份", Cid: "300", Local: "备份"},
		{ID: 4, Name: "旧", Cid: "400", Local: "旧", StagingCid: "s4", SchemeID: 999},
	}

	cfg, err := h.orgConfigFor(libs[0], libs)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Pending != "s1" || cfg.Library != "100" || !cfg.PinLib || cfg.Flat || cfg.SchemeID != sch.ID ||
		cfg.LibLocal != "电影/主号" || cfg.Existing != "ex1" || cfg.Redundant != "re1" || !cfg.ManualConfirm {
		t.Fatalf("库 1 的配置不对: %+v", cfg)
	}
	// 同账号别的库与它们的转存文件夹都是保护子树
	other := strings.Join(cfg.OtherLibs, ",")
	for _, want := range []string{"200", "s2", "300"} {
		if !strings.Contains(other, want) {
			t.Fatalf("保护子树少了 %s: %v", want, cfg.OtherLibs)
		}
	}

	if cfg, err := h.orgConfigFor(libs[1], libs); err != nil || !cfg.Flat {
		t.Fatalf("没选方案 = 不分类: %+v %v", cfg, err)
	}
	if _, err := h.orgConfigFor(libs[2], libs); err != errLibNoStaging {
		t.Fatalf("没设转存文件夹的库只同步: %v", err)
	}
	if _, err := h.orgConfigFor(libs[3], libs); err == nil || !strings.Contains(err.Error(), "分类方案已经删除") {
		t.Fatalf("方案被删要报出来: %v", err)
	}
	if got := organizeLibs(libs); len(got) != 3 {
		t.Fatalf("要整理的库: %d", len(got))
	}
}

func TestOrgConfigNoWorkspace(t *testing.T) {
	newTestDB(t, "orgcfg_v2_nows.db")
	h := &Handler{DB: model.DB}
	lib := model.Library{ID: 1, Name: "电影", Cid: "100", Local: "电影", StagingCid: "s1"}
	if _, err := h.orgConfigFor(lib, []model.Library{lib}); err == nil || !strings.Contains(err.Error(), "已存在") {
		t.Fatalf("没设已存在 / 冗余要拦: %v", err)
	}
}

func TestOrgLibOfRecord(t *testing.T) {
	libs := []model.Library{
		{ID: 1, Account: acctMain, Cid: "100", StagingCid: "s1"},
		{ID: 2, Account: acctMain, Cid: "200", StagingCid: "s2"},
		{ID: 3, Account: 2, Cid: "300", StagingCid: "s3"},
	}
	cases := []struct {
		rec  model.OrganizeRecord
		want uint
		ok   bool
	}{
		{model.OrganizeRecord{LibID: 3}, 3, true},
		{model.OrganizeRecord{LibID: 9}, 0, false},        // 库已删除
		{model.OrganizeRecord{TargetDir: "a"}, 1, true},   // 老记录 LibCid 空 = 首个库
		{model.OrganizeRecord{LibCid: "200"}, 2, true},    // 老记录记了附加库 cid
		{model.OrganizeRecord{LibCid: "a2:300"}, 3, true}, // 附加账号的库 key
		{model.OrganizeRecord{LibCid: "999"}, 0, false},   // 认不出
	}
	for i, c := range cases {
		l, ok := orgLibOfRecord(&c.rec, libs)
		if ok != c.ok || (ok && l.ID != c.want) {
			t.Errorf("#%d → %d %v，期望 %d %v", i, l.ID, ok, c.want, c.ok)
		}
	}
	// 没入过库的：看来源在哪个库的转存文件夹里（LibCid 空也不当成首个库，先看来源）
	rec := model.OrganizeRecord{LibCid: "x", SourceCid: "s3"}
	if l, ok := orgLibOfRecord(&rec, libs); !ok || l.ID != 3 {
		t.Fatalf("按来源认库: %d %v", l.ID, ok)
	}
}

func TestLibOfChain(t *testing.T) {
	libs := []model.Library{
		{ID: 1, Account: acctMain, Cid: "100", StagingCid: "s1"},
		{ID: 2, Account: 2, Cid: "100b", StagingCid: "s2"},
	}
	if l, inStaging, ok := libOfChain(libs, acctMain, []string{"0", "s1", "x"}); !ok || !inStaging || l.ID != 1 {
		t.Fatalf("转存文件夹里: %d %v %v", l.ID, inStaging, ok)
	}
	if l, inStaging, ok := libOfChain(libs, acctMain, []string{"0", "100", "华语电影"}); !ok || inStaging || l.ID != 1 {
		t.Fatalf("库目录里: %d %v %v", l.ID, inStaging, ok)
	}
	if _, _, ok := libOfChain(libs, acctMain, []string{"0", "s2"}); ok {
		t.Fatal("别的账号的库不算")
	}
}
