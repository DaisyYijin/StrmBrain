package api

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"

	"115-station/internal/config"
	"115-station/internal/model"

	"github.com/gin-gonic/gin"
)

// V2 阶段 4：媒体库页面与分类方案的接口（libapiv2.go / catschemeapi.go），真 SQLite + 真 setting.yaml 走 HTTP

func mediaLibsSetup(t *testing.T) (*Handler, *gin.Engine, *config.Config) {
	t.Helper()
	newTestDB(t, "medialibs.db")
	forgetLibraries()
	forgetSchemes()
	t.Cleanup(func() { forgetLibraries(); forgetSchemes(); forgetClassifyRules() })
	cfg := &config.Config{DataDir: t.TempDir(), ConfigDir: t.TempDir()}
	prev := notifyConfigSource
	notifyConfigSource = cfg
	t.Cleanup(func() { notifyConfigSource = prev })
	if err := model.DB.Create(&model.Storage{Name: "115主号", Type: "115", Main: true}).Error; err != nil {
		t.Fatal(err)
	}
	if _, err := ensureDefaultScheme(model.DB); err != nil {
		t.Fatal(err)
	}
	gin.SetMode(gin.TestMode)
	h := &Handler{DB: model.DB, Config: cfg}
	r := gin.New()
	r.GET("/media-libs", h.ListMediaLibs)
	r.POST("/media-libs", h.CreateMediaLib)
	r.POST("/media-libs/batch", h.CreateMediaLibsBatch)
	r.PUT("/media-libs/:id", h.UpdateMediaLib)
	r.DELETE("/media-libs/:id", h.DeleteMediaLib)
	r.GET("/category/schemes/:id", h.GetScheme)
	r.POST("/category/schemes", h.CreateScheme)
	r.PUT("/category/schemes/:id", h.UpdateScheme)
	r.DELETE("/category/schemes/:id", h.DeleteScheme)
	return h, r, cfg
}

func TestMediaLibsCRUD(t *testing.T) {
	_, r, cfg := mediaLibsSetup(t)
	code, body := libHTTPDo(t, r, "POST", "/media-libs",
		`{"name":"电影","cid":"100","path":"/影视/电影","local":"/media/电影/主号","staging_cid":"s1","staging_path":"/转存/电影","scheme_id":0,"scrape":"auto"}`)
	if code != http.StatusOK {
		t.Fatalf("新建：%d %v", code, body)
	}
	libs := loadLibraries()
	if len(libs) != 1 || libs[0].Local != "电影/主号" || libs[0].StagingCid != "s1" || libs[0].Scrape != "auto" {
		t.Fatalf("存下的库不对（本地目录要去掉媒体根）：%+v", libs)
	}
	// 写回老配置：首个库进 full.cid，lib_opts 记本地名
	if jsonStrField(cfg.GetSetting("full"), "cid") != "100" || !strings.Contains(cfg.GetSetting(libOptsKey), "电影/主号") {
		t.Fatalf("老配置没跟上：full=%s opts=%s", cfg.GetSetting("full"), cfg.GetSetting(libOptsKey))
	}

	// 本地目录互相包含的拒绝
	code, body = libHTTPDo(t, r, "POST", "/media-libs", `{"name":"电影2","cid":"200","local":"电影/主号/x"}`)
	if code != http.StatusBadRequest {
		t.Fatalf("本地目录包含要拒绝：%d %v", code, body)
	}
	// 同一个上级下的两个库可以（多个账号 / 目录进同一个 Emby 库）
	code, body = libHTTPDo(t, r, "POST", "/media-libs", `{"name":"电影·备份","cid":"200","local":"电影/备份"}`)
	if code != http.StatusOK {
		t.Fatalf("并列的本地目录：%d %v", code, body)
	}
	if opts := parseLibOpts(cfg.GetSetting(libOptsKey)); !opts.of("200").self() {
		t.Fatal("没设转存文件夹、不分类的库在老配置里是「我自己整理」")
	}

	id := loadLibraries()[0].ID
	// 已有同步内容的库：本地目录不能再改
	model.DB.Create(&model.SyncedFile{FileID: "f1", RelPath: "电影/主号/阿凡达/a.strm", Kind: "video"})
	code, body = libHTTPDo(t, r, "PUT", fmt.Sprintf("/media-libs/%d", id), `{"name":"电影","cid":"100","local":"电影/新","staging_cid":"s1","scrape":"auto"}`)
	if code != http.StatusBadRequest || !strings.Contains(fmt.Sprint(body["error"]), "本地目录不能再改") {
		t.Fatalf("有内容时改本地目录要拒绝：%d %v", code, body)
	}
	// 其余可以改：去掉转存文件夹 = 只同步
	code, body = libHTTPDo(t, r, "PUT", fmt.Sprintf("/media-libs/%d", id), `{"name":"电影·主号","cid":"100","local":"电影/主号","scrape":"manual"}`)
	if code != http.StatusOK {
		t.Fatalf("修改：%d %v", code, body)
	}
	if l, _ := libraryByID(id); l.Name != "电影·主号" || l.StagingCid != "" {
		t.Fatalf("修改没存上：%+v", l)
	}

	// 别的账号：没开多账号时拒绝
	code, _ = libHTTPDo(t, r, "POST", "/media-libs", `{"name":"x","account":9,"cid":"300","local":"x"}`)
	if code != http.StatusBadRequest {
		t.Fatalf("不存在的账号要拒绝：%d", code)
	}

	code, _ = libHTTPDo(t, r, "DELETE", fmt.Sprintf("/media-libs/%d", id), "")
	if code != http.StatusOK || len(loadLibraries()) != 1 {
		t.Fatalf("删除：%d %d", code, len(loadLibraries()))
	}
	if jsonStrField(cfg.GetSetting("full"), "cid") != "200" {
		t.Fatal("删掉首个库后 full.cid 跟着换成下一个")
	}
}

func TestSchemesCRUD(t *testing.T) {
	_, r, _ := mediaLibsSetup(t)
	first := primarySchemeID()
	code, body := libHTTPDo(t, r, "POST", "/category/schemes", `{"name":"电影分类"}`)
	if code != http.StatusOK {
		t.Fatalf("新建方案：%d %v", code, body)
	}
	newID := uint(body["id"].(float64))
	if s, ok := schemeByID(newID); !ok || s.Name != "电影分类" {
		t.Fatalf("新方案没存上：%+v", s)
	}
	code, _ = libHTTPDo(t, r, "POST", "/category/schemes", `{"name":"电影分类"}`)
	if code != http.StatusBadRequest {
		t.Fatal("重名要拒绝")
	}
	yaml := "movie:\n  华语电影:\n    original_language: zh\n    library: 别的库\n  外语电影:\ntv:\n  剧集:\n"
	b, _ := json.Marshal(map[string]string{"yaml": yaml, "name": "电影专用"})
	code, body = libHTTPDo(t, r, "PUT", fmt.Sprintf("/category/schemes/%d", newID), string(b))
	if code != http.StatusOK {
		t.Fatalf("改方案：%d %v", code, body)
	}
	s, _ := schemeByID(newID)
	if s.Name != "电影专用" || strings.Contains(s.Config, "library") {
		t.Fatalf("改名与内容（library 键要去掉）：%+v", s)
	}
	if n := len(classifyRulesOf(newID, "movie")); n != 2 {
		t.Fatalf("规则行按方案重建：%d", n)
	}
	// 被库用着的不能删
	model.DB.Create(&model.Library{Name: "电影", Cid: "100", Local: "电影", SchemeID: newID})
	forgetLibraries()
	if code, _ = libHTTPDo(t, r, "DELETE", fmt.Sprintf("/category/schemes/%d", newID), ""); code != http.StatusConflict {
		t.Fatalf("在用的方案要拒删：%d", code)
	}
	if code, _ = libHTTPDo(t, r, "DELETE", fmt.Sprintf("/category/schemes/%d", first), ""); code != http.StatusOK {
		t.Fatalf("没人用的可以删：%d", code)
	}
	if code, _ = libHTTPDo(t, r, "DELETE", fmt.Sprintf("/category/schemes/%d", newID), ""); code != http.StatusConflict {
		t.Fatal("最后一套也不能删")
	}
}

// 批量新建：转存文件夹只给路径的按路径建；整组校验不过时一个目录都不建、一个库都不存
func TestMediaLibsBatch(t *testing.T) {
	_, r, _ := mediaLibsSetup(t)
	var made []string
	prev := batchEnsureStaging
	batchEnsureStaging = func(_ *Handler, acct uint, path string) (string, error) {
		made = append(made, fmt.Sprintf("%d:%s", acct, path))
		return "new-" + path, nil
	}
	t.Cleanup(func() { batchEnsureStaging = prev })

	// 名字重了：先校验，不建目录
	code, body := libHTTPDo(t, r, "POST", "/media-libs/batch", `{"libs":[
		{"name":"电影","cid":"100","local":"电影","staging_path":"/最近接收/电影"},
		{"name":"电影","cid":"200","local":"剧集","staging_path":"/最近接收/剧集"}]}`)
	if code != http.StatusBadRequest || len(made) != 0 || len(loadLibraries()) != 0 {
		t.Fatalf("整组校验不过要先拒绝：%d %v made=%v", code, body, made)
	}

	code, body = libHTTPDo(t, r, "POST", "/media-libs/batch", `{"libs":[
		{"name":"电影","cid":"100","path":"/影视/电影","local":"电影","staging_path":"/最近接收/电影","scrape":"auto"},
		{"name":"剧集","cid":"200","path":"/影视/剧集","local":"剧集","staging_cid":"s2","staging_path":"/转存/剧集"},
		{"name":"纪录片","cid":"300","path":"/影视/纪录片","local":"纪录片"}]}`)
	if code != http.StatusOK {
		t.Fatalf("批量新建：%d %v", code, body)
	}
	if len(made) != 1 || made[0] != "0:/最近接收/电影" {
		t.Fatalf("只有只给路径的要建：%v", made)
	}
	libs := loadLibraries()
	if len(libs) != 3 {
		t.Fatalf("应建 3 个库：%+v", libs)
	}
	by := map[string]model.Library{}
	for _, l := range libs {
		by[l.Name] = l
	}
	if l := by["电影"]; l.StagingCid != "new-/最近接收/电影" || l.StagingPath != "/最近接收/电影" || l.Scrape != "auto" {
		t.Errorf("电影：%+v", l)
	}
	if l := by["剧集"]; l.StagingCid != "s2" || l.Scrape != libScrapeManual {
		t.Errorf("剧集（给了 cid 不建、刮削默认只手动）：%+v", l)
	}
	if l := by["纪录片"]; l.StagingCid != "" {
		t.Errorf("纪录片没设转存文件夹：%+v", l)
	}

	// 和已有的库撞目录：整批拒绝
	made = nil
	code, _ = libHTTPDo(t, r, "POST", "/media-libs/batch", `{"libs":[
		{"name":"动漫","cid":"400","local":"动漫","staging_path":"/最近接收/动漫"},
		{"name":"电影2","cid":"100","local":"电影2"}]}`)
	if code != http.StatusBadRequest || len(made) != 0 || len(loadLibraries()) != 3 {
		t.Fatalf("和已有库同一个 115 目录要整批拒绝：%d made=%v", code, made)
	}

	var many []string
	for i := 0; i <= libBatchMax; i++ {
		many = append(many, fmt.Sprintf(`{"name":"库%d","cid":"9%d","local":"库%d"}`, i, i, i))
	}
	if code, _ := libHTTPDo(t, r, "POST", "/media-libs/batch", `{"libs":[`+strings.Join(many, ",")+`]}`); code != http.StatusBadRequest {
		t.Error("超过上限要拒绝")
	}
}
