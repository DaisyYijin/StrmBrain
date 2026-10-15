package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"115-station/internal/config"
	"115-station/internal/model"

	"github.com/gin-gonic/gin"
)

// 端到端：真 SQLite + 真 setting.yaml，走 HTTP 调 PUT /libraries 与 POST /libraries/fix-rename。
// 不碰 115：移除库与改名修复都不需要（库名读路径缓存，这里直接种进去）。
// 校验逻辑在 libraries_test.go 测过，这里看的是「写进去的东西对不对、几份配置有没有对上」

const httpCatYAML = `movie:
  电影/动画电影:
    genre_ids: '16'
    library: 动漫
  电影/外语电影:
tv:
  电视剧/日番:
    library: '300'
  电视剧/国漫:
    genre_ids: '16'
    library: 动画
`

func libHTTPSetup(t *testing.T) (*Handler, *gin.Engine) {
	t.Helper()
	newTestDB(t, "libhttp.db")
	prevSrc := notifyConfigSource
	cfg := &config.Config{DataDir: t.TempDir(), ConfigDir: t.TempDir()}
	notifyConfigSource = cfg
	t.Cleanup(func() { notifyConfigSource = prevSrc; forgetClassifyRules() })

	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
	}
	// 动漫库在 115 上从「动画」改名成了「动漫」：配置里还存着旧显示路径
	must(cfg.SaveSettings(map[string]string{
		"full":         `{"cid":"100","cid_path":"/影视","local_path":"` + strings.ReplaceAll(t.TempDir(), `\`, "/") + `","mode":"fast","extra_libs":[{"cid":"200","path":"/动画"},{"cid":"300","path":"/4K"}]}`,
		"share":        `{"folder":"root"}`,
		stagingLibsKey: `[{"cid":"s4k","name":"4K","lib":"300"},{"cid":"sanime","name":"动漫","lib":"200"}]`,
		"subscribe":    `{"subdir":"s4k","interval":60}`,
	}))
	rememberDirPaths([]model.PathCache{
		{FileID: "100", ParentID: "0", Name: "影视", Path: "/影视"},
		{FileID: "200", ParentID: "0", Name: "动漫", Path: "/动漫"},
		{FileID: "300", ParentID: "0", Name: "4K", Path: "/4K"},
	})
	rows, err := parseCategoryYAML(httpCatYAML)
	must(err)
	must(model.DB.Create(&model.ScrapeRule{Type: "category_config", Enabled: true, Config: httpCatYAML}).Error)
	must(model.DB.Create(&rows).Error)
	must(model.DB.Create(&model.Subscription{Title: "A", TmdbID: 1, MediaType: "tv", Subdir: "s4k"}).Error)
	must(model.DB.Create(&model.Subscription{Title: "B", TmdbID: 2, MediaType: "tv", Subdir: "sanime"}).Error)

	gin.SetMode(gin.TestMode)
	h := &Handler{DB: model.DB, Config: cfg}
	r := gin.New()
	r.GET("/libraries", h.GetLibraries)
	r.PUT("/libraries", h.SaveLibraries)
	r.POST("/libraries/fix-rename", h.FixLibraryRename)
	return h, r
}

func libHTTPDo(t *testing.T, r *gin.Engine, method, path, body string) (int, map[string]any) {
	t.Helper()
	w := httptest.NewRecorder()
	req := httptest.NewRequest(method, path, bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	r.ServeHTTP(w, req)
	out := map[string]any{}
	_ = json.Unmarshal(w.Body.Bytes(), &out)
	return w.Code, out
}

func TestLibrariesHTTPRemove(t *testing.T) {
	h, r := libHTTPSetup(t)

	code, view := libHTTPDo(t, r, http.MethodGet, "/libraries", "")
	if code != 200 || len(view["libs"].([]any)) != 3 {
		t.Fatalf("GET：%d %v", code, view)
	}

	// 移除 4K：保留动漫库与它的入口，4K 入口不带上（解绑），清本地
	code, out := libHTTPDo(t, r, http.MethodPut, "/libraries",
		`{"extra_libs":[{"cid":"200","path":"/动画"}],"inboxes":[{"cid":"sanime","name":"动漫","lib":"200"}],"clean_local":["300"]}`)
	if code != 200 {
		t.Fatalf("PUT：%d %v", code, out)
	}

	full := parseFullCfg(h.getSettingValue("full"))
	if len(full.ExtraLibs) != 1 || full.ExtraLibs[0].Cid != "200" {
		t.Errorf("full.extra_libs：%+v", full.ExtraLibs)
	}
	if jsonStrField(h.getSettingValue("full"), "mode") != "fast" {
		t.Error("full 的其他字段要保留")
	}
	var bindings []stagingLib
	_ = json.Unmarshal([]byte(h.getSettingValue(stagingLibsKey)), &bindings)
	if len(bindings) != 1 || bindings[0].Cid != "sanime" {
		t.Errorf("staging_libs：%+v", bindings)
	}
	if !strings.Contains(h.getSettingValue("subscribe"), `"subdir":""`) || !strings.Contains(h.getSettingValue("subscribe"), `"interval":60`) {
		t.Errorf("订阅默认子目录被解绑应清空、其余保留：%s", h.getSettingValue("subscribe"))
	}
	var subs []model.Subscription
	model.DB.Order("id").Find(&subs)
	if subs[0].Subdir != "root" || subs[1].Subdir != "sanime" {
		t.Errorf("引用解绑子目录的订阅改 root，其余不动：%q %q", subs[0].Subdir, subs[1].Subdir)
	}
	var rule model.ScrapeRule
	model.DB.Where("type = ?", "category_config").First(&rule)
	if strings.Contains(rule.Config, "'300'") || !strings.Contains(rule.Config, "library: 动漫") {
		t.Errorf("指向 4K 的 library 删掉、别的不动：\n%s", rule.Config)
	}
	var cr model.CategoryRule
	model.DB.Where("media_type = ? AND name = ?", "tv", "电视剧/日番").First(&cr)
	if cr.Library != "" {
		t.Errorf("规则表跟着 YAML 重建：%+v", cr)
	}
	var jobs []model.TaskJob
	model.DB.Where("kind = ?", "libclean").Find(&jobs)
	if len(jobs) != 1 || !strings.Contains(jobs[0].Title, "4K") {
		t.Errorf("清本地入队：%+v", jobs)
	}
}

func TestLibrariesHTTPRemoveRejected(t *testing.T) {
	h, r := libHTTPSetup(t)
	before := h.getSettingValue("full")
	// 入口指向这次被移除的库：拒绝，什么都不写
	code, _ := libHTTPDo(t, r, http.MethodPut, "/libraries",
		`{"extra_libs":[{"cid":"200","path":"/动画"}],"inboxes":[{"cid":"s4k","name":"4K","lib":"300"}]}`)
	if code != http.StatusBadRequest {
		t.Fatalf("应拒绝：%d", code)
	}
	if h.getSettingValue("full") != before {
		t.Error("拒绝时不该写配置")
	}
	var rule model.ScrapeRule
	model.DB.Where("type = ?", "category_config").First(&rule)
	if rule.Config != httpCatYAML {
		t.Error("拒绝时不该改 YAML")
	}
}

func TestLibrariesHTTPFixRename(t *testing.T) {
	h, r := libHTTPSetup(t)
	code, view := libHTTPDo(t, r, http.MethodGet, "/libraries", "")
	fixes, _ := view["rename_fixes"].([]any)
	if code != 200 || len(fixes) != 1 {
		t.Fatalf("应认出「动画」是动漫库改的名：%v", view["rename_fixes"])
	}
	if code, _ := libHTTPDo(t, r, http.MethodPost, "/libraries/fix-rename", `{"from":"不存在"}`); code != http.StatusBadRequest {
		t.Errorf("认不出的不改：%d", code)
	}
	code, out := libHTTPDo(t, r, http.MethodPost, "/libraries/fix-rename", `{"from":"动画"}`)
	if code != 200 {
		t.Fatalf("修复：%d %v", code, out)
	}
	var cr model.CategoryRule
	model.DB.Where("media_type = ? AND name = ?", "tv", "电视剧/国漫").First(&cr)
	if cr.Library != "动漫" {
		t.Errorf("规则里的旧名应换成新名：%+v", cr)
	}
	if p := parseFullCfg(h.getSettingValue("full")).ExtraLibs[0].Path; p != "/动漫" {
		t.Errorf("显示路径刷新成新路径：%s", p)
	}
	data := out["data"].(map[string]any)
	if len(data["orphan_rules"].([]any)) != 0 || len(data["rename_fixes"].([]any)) != 0 {
		t.Errorf("修完不该再有认不出的规则：%v", data)
	}
}
