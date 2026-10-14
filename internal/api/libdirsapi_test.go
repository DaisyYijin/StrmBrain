package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"115-station/internal/config"
	"115-station/internal/model"

	"github.com/gin-gonic/gin"
)

// 媒体库目录表：启用、导入导出、老接口上锁（libdirsapi.go）

func libDirsTestEnv(t *testing.T) *Handler {
	t.Helper()
	gin.SetMode(gin.TestMode)
	newTestDB(t, "libdirsapi.db")
	notifyConfigSource = &config.Config{DataDir: t.TempDir(), ConfigDir: t.TempDir()}
	t.Cleanup(func() { notifyConfigSource = nil; forgetLibraryDirs(); resetClassifyRules() })
	forgetLibraryDirs()
	return &Handler{DB: model.DB, Config: notifyConfigSource}
}

func callJSON(fn gin.HandlerFunc, method, body string) *httptest.ResponseRecorder {
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(method, "/", strings.NewReader(body))
	c.Request.Header.Set("Content-Type", "application/json")
	fn(c)
	return w
}

// 启用：老配置迁成目录表、打开开关、识别与整理切过去；老接口从此拒绝写
func TestActivateLibraryDirs(t *testing.T) {
	h := libDirsTestEnv(t)
	seedCategoryRules(t,
		model.CategoryRule{MediaType: "movie", Name: "电影/华语", OriginalLanguage: "zh", Priority: 1},
		model.CategoryRule{MediaType: "movie", Name: "电影/外语", IsDefault: true, Priority: 2},
		model.CategoryRule{MediaType: "tv", Name: "剧集", IsDefault: true, Priority: 1},
	)
	model.DB.Create(&model.SyncedFile{FileID: "1", RelPath: "影视/LGNB/无间道 (2002)/无间道.strm", Kind: "video"})
	if err := h.saveLibExtraDirs([]libExtraDir{{Dir: "LGNB", Type: "movie"}}); err != nil {
		t.Fatal(err)
	}
	resetClassifyRules()

	if libraryDirsActive() {
		t.Fatal("启用前开关应关着")
	}
	if ok, why := h.activateLibraryDirs(testRoots); !ok {
		t.Fatalf("启用失败: %s", why)
	}
	if !libraryDirsActive() {
		t.Fatal("开关没打开")
	}
	var rows []model.LibraryDir
	model.DB.Order("priority").Find(&rows)
	if len(rows) != 4 || rows[0].Path != "电影/华语" || rows[3].Path != "LGNB" || rows[3].Organize {
		t.Fatalf("迁移结果: %+v", rows)
	}
	if c := classifyMedia(&TmdbMedia{MediaType: "movie", OrigLanguage: "zh"}); c != "电影/华语" {
		t.Errorf("整理走目录表: %q", c)
	}
	if _, ok := loadLibCategoryLayout().titleAt("影视/LGNB/无间道 (2002)/无间道.strm"); !ok {
		t.Error("其他媒体库目录迁过来后仍认得")
	}
	// 老配置原样保留（回滚用）
	var n int64
	model.DB.Model(&model.CategoryRule{}).Count(&n)
	if n != 3 || len(loadLibExtraDirs()) != 1 {
		t.Errorf("老配置应原样保留: rules=%d extras=%d", n, len(loadLibExtraDirs()))
	}
	for name, fn := range map[string]gin.HandlerFunc{"分类规则": h.SaveCategories, "其他目录": h.SaveLibExtraDirs, "其他目录追加": h.AddLibExtraDirs} {
		if w := callJSON(fn, http.MethodPost, `{}`); w.Code != http.StatusConflict {
			t.Errorf("%s 写接口启用后应拒绝: %d", name, w.Code)
		}
	}
	// 已启用再调：不重新迁移（不会把用户改过的表盖掉）
	model.DB.Where("path = ?", "LGNB").Delete(&model.LibraryDir{})
	forgetLibraryDirs()
	if ok, _ := h.ensureLibraryDirs(); !ok {
		t.Fatal("已启用")
	}
	model.DB.Model(&model.LibraryDir{}).Count(&n)
	if n != 3 {
		t.Errorf("已启用时不该重新迁移: %d 行", n)
	}
}

// 没配媒体库：不启用，说明原因
func TestEnsureLibraryDirsNoLibrary(t *testing.T) {
	h := libDirsTestEnv(t)
	if ok, why := h.ensureLibraryDirs(); ok || !strings.Contains(why, "媒体库") {
		t.Errorf("got %v %q", ok, why)
	}
	if libraryDirsActive() {
		t.Error("不该打开开关")
	}
}

// T8：导出成老分类规则 YAML，再按导入同一套转换回来，参与整理的行不变
func TestLibraryDirsYAMLRoundTrip(t *testing.T) {
	rows := []model.LibraryDir{
		{Priority: 1, LibCid: "200", LibName: "动漫库", Path: "动漫", MediaType: "tv", Organize: true, GenreIds: "16"},
		{Priority: 2, LibCid: "100", LibName: "影视", Path: "国产剧", MediaType: "tv", Organize: true, OriginCountry: "CN,TW,HK"},
		{Priority: 3, LibCid: "100", LibName: "影视", Path: "剧集", MediaType: "tv", Organize: true, Fallback: true},
		{Priority: 4, LibCid: "100", LibName: "影视", Path: "电影/老片", MediaType: "movie", Organize: true, ReleaseYear: "1900-1999", CustomRegex: "^老"},
		{Priority: 5, LibCid: "100", LibName: "影视", Path: "电影", MediaType: "movie", Organize: true, Fallback: true},
		{Priority: 6, LibCid: "100", LibName: "影视", Path: "收藏", MediaType: "all"}, // 仅识别：导出成注释
	}
	out := exportLibraryDirsYAML(rows, "100")
	if !strings.Contains(out, "library: 动漫库") || !strings.Contains(out, "# 仅识别：影视 / 收藏") {
		t.Fatalf("导出:\n%s", out)
	}
	rules, err := parseCategoryYAML(out)
	if err != nil {
		t.Fatal(err)
	}
	mig, err := planLibraryDirMigration(rules, nil, testRoots, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	strip := func(rs []model.LibraryDir) []string {
		var s []string
		for _, r := range rs {
			if !r.Organize {
				continue
			}
			s = append(s, strings.Join([]string{r.LibCid, r.Path, r.MediaType, r.GenreIds, r.OriginCountry, r.ReleaseYear, r.CustomRegex}, "|")+
				map[bool]string{true: "|兜底", false: ""}[r.Fallback])
		}
		return s
	}
	// 导出按类型分节：电影一节在前，所以比的是各自类型内的顺序
	want := strip([]model.LibraryDir{rows[3], rows[4], rows[0], rows[1], rows[2]})
	if got := strip(mig.Rows); strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Errorf("往返:\n got  %v\n want %v", got, want)
	}
}

// 一行的规范化：路径、类型、库
func TestNormLibDirRow(t *testing.T) {
	r, err := normLibDirRow(model.LibraryDir{LibCid: "200", Path: ` /动漫\日番/ `, MediaType: "tv", Fallback: true}, testRoots)
	if err != nil || r.Path != "动漫/日番" || r.LibName != "动漫库" || r.Fallback {
		t.Errorf("规范化 / 仅识别的行去掉兜底: %+v %v", r, err)
	}
	for _, bad := range []model.LibraryDir{
		{LibCid: "100", Path: "a/../b", MediaType: "tv"},
		{LibCid: "100", Path: "a", MediaType: "x"},
		{LibCid: "", Path: "a", MediaType: "tv"},
	} {
		if _, err := normLibDirRow(bad, testRoots); err == nil {
			t.Errorf("应拒收: %+v", bad)
		}
	}
	// 库不在配置里的行（迁移来的、库移出了配置）原样保留，不让整张表存不进去
	if r, err := normLibDirRow(model.LibraryDir{LibCid: "早就删了的库", Path: "a", MediaType: "tv"}, testRoots); err != nil || r.LibName != "" {
		t.Errorf("库不在配置里的行应保留: %+v %v", r, err)
	}
	if r, err := normLibDirRow(model.LibraryDir{LibCid: "x", LibName: "旧名", Path: "a", MediaType: "tv"}, testRoots); err != nil || r.LibName != "旧名" {
		t.Errorf("带着快照的认不出库的行保留: %+v %v", r, err)
	}
}
