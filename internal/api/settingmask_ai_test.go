package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"

	"115-station/internal/config"
	"115-station/internal/model"

	"github.com/gin-gonic/gin"
)

// org-gpt 的 API 密钥字段叫 key，名字太泛不进 sensitiveFieldRe，按配置 key 单独点名掩码；
// 「测试」按钮发来掩码时换成已保存的密钥
func TestSettingMaskAIKey(t *testing.T) {
	newTestDB(t, "settingmask_ai.db")
	prevSrc := notifyConfigSource
	cfg := &config.Config{DataDir: t.TempDir(), ConfigDir: t.TempDir()}
	notifyConfigSource = cfg
	t.Cleanup(func() { notifyConfigSource = prevSrc })

	// 开关关着也要能测：savedAIKey 不看 enabled
	seed := `{"enabled":false,"url":"http://x","key":"sk-real","model":"m","confirm_mode":"auto"}`
	if err := cfg.SaveSettings(map[string]string{aiSettingKey: seed}); err != nil {
		t.Fatal(err)
	}

	gin.SetMode(gin.TestMode)
	h := &Handler{DB: model.DB, Config: cfg}
	r := gin.New()
	r.GET("/config/setting", h.GetSetting)
	r.POST("/config/setting", h.SaveSetting)
	r.GET("/config/secret", h.RevealSecret)
	r.POST("/config/test-ai", h.TestAIConnection)

	do := func(method, path string, body any) map[string]any {
		t.Helper()
		var rd *bytes.Reader
		if body != nil {
			b, _ := json.Marshal(body)
			rd = bytes.NewReader(b)
		} else {
			rd = bytes.NewReader(nil)
		}
		w := httptest.NewRecorder()
		req := httptest.NewRequest(method, path, rd)
		req.Header.Set("Content-Type", "application/json")
		r.ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Fatalf("%s %s = %d %s", method, path, w.Code, w.Body.String())
		}
		var out map[string]any
		_ = json.Unmarshal(w.Body.Bytes(), &out)
		return out
	}

	raw, _ := do(http.MethodGet, "/config/setting?key="+aiSettingKey, nil)["value"].(string)
	var m map[string]any
	_ = json.Unmarshal([]byte(raw), &m)
	if m["key"] != settingMask {
		t.Errorf("org-gpt.key = %v，应为掩码", m["key"])
	}
	if m["url"] != "http://x" || m["model"] != "m" || m["confirm_mode"] != "auto" {
		t.Errorf("其他字段不该被掩码：%s", raw)
	}
	// 别的配置里叫 key 的字段不受影响
	if got := maskSensitiveJSON(`{"key":"v"}`); got != `{"key":"v"}` {
		t.Errorf("通用掩码不该认 key：%s", got)
	}

	// 原样回传掩码 = 没改
	do(http.MethodPost, "/config/setting", map[string]string{"key": aiSettingKey, "value": raw})
	if savedAIKey() != "sk-real" {
		t.Errorf("回传掩码后密钥 = %q", savedAIKey())
	}

	// 「眼睛」
	q := url.Values{"key": {aiSettingKey}, "field": {"key"}}
	if v := do(http.MethodGet, "/config/secret?"+q.Encode(), nil)["value"]; v != "sk-real" {
		t.Errorf("reveal org-gpt.key = %v", v)
	}

	// 测试按钮：掩码换成已保存的密钥，明文照用
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"hi"}}]}`))
	}))
	defer srv.Close()
	for _, c := range []struct{ key, want string }{
		{settingMask, "Bearer sk-real"},
		{"sk-new", "Bearer sk-new"},
	} {
		out := do(http.MethodPost, "/config/test-ai", map[string]string{"url": srv.URL, "key": c.key, "model": "m"})
		if out["ok"] != true {
			t.Fatalf("测试失败：%v", out)
		}
		if gotAuth != c.want {
			t.Errorf("key=%q 时 Authorization = %q, want %q", c.key, gotAuth, c.want)
		}
	}
}
