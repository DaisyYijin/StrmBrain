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

// 通用配置接口 GET / POST /config/setting 对资源站凭据的掩码：
// re0 / guanying / mukaku 界面走专用接口，但通用接口一样读得到，之前漏在掩码清单外、明文返回
func TestSettingMaskSourceCreds(t *testing.T) {
	newTestDB(t, "settingmask.db")
	prevSrc := notifyConfigSource
	cfg := &config.Config{DataDir: t.TempDir(), ConfigDir: t.TempDir()}
	notifyConfigSource = cfg
	t.Cleanup(func() { notifyConfigSource = prevSrc })

	seeds := map[string]string{
		"re0":      `{"base_url":"https://re0.me","client_id":"cid","client_secret":"cs","access_token":"at","refresh_token":"rt","token_exp":123,"username":"u","password":"pw"}`,
		"guanying": `{"base_url":"https://gy","username":"u","password":"pw","cookies":{"sess":"abc","pow":"x"}}`,
		"mukaku":   `{"base_url":"https://mk","access_token":"at","username":"u","password":"pw"}`,
	}
	if err := cfg.SaveSettings(seeds); err != nil {
		t.Fatal(err)
	}
	// RevealSecret 的专用分支读 10 秒缓存，别拿到别的测试留下的
	re0CfgMu.Lock()
	re0CfgV = nil
	re0CfgMu.Unlock()
	gyCfgMu.Lock()
	gyCfgV = nil
	gyCfgMu.Unlock()

	gin.SetMode(gin.TestMode)
	h := &Handler{DB: model.DB, Config: cfg}
	r := gin.New()
	r.GET("/config/setting", h.GetSetting)
	r.POST("/config/setting", h.SaveSetting)
	r.GET("/config/secret", h.RevealSecret)

	get := func(path string) map[string]any {
		t.Helper()
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, path, nil))
		if w.Code != http.StatusOK {
			t.Fatalf("GET %s = %d %s", path, w.Code, w.Body.String())
		}
		var out map[string]any
		_ = json.Unmarshal(w.Body.Bytes(), &out)
		return out
	}
	getValue := func(key string) (string, map[string]any) {
		t.Helper()
		raw, _ := get("/config/setting?key=" + key)["value"].(string)
		var m map[string]any
		if err := json.Unmarshal([]byte(raw), &m); err != nil {
			t.Fatalf("%s 返回的不是 JSON：%q", key, raw)
		}
		return raw, m
	}

	masked := map[string][]string{
		"re0":      {"client_secret", "access_token", "refresh_token", "password"},
		"guanying": {"password", "cookies"},
		"mukaku":   {"access_token", "password"},
	}
	plain := map[string]map[string]any{
		"re0":      {"client_id": "cid", "username": "u", "token_exp": float64(123)},
		"guanying": {"username": "u"},
		"mukaku":   {"username": "u"},
	}
	for key, fields := range masked {
		raw, m := getValue(key)
		for _, f := range fields {
			if m[f] != settingMask {
				t.Errorf("%s.%s = %v，应为掩码（整份：%s）", key, f, m[f], raw)
			}
		}
		for f, want := range plain[key] {
			if m[f] != want {
				t.Errorf("%s.%s = %v，不该被掩码，want %v", key, f, m[f], want)
			}
		}

		// 原样回传掩码 = 没改：保存后存着的还是原值
		body, _ := json.Marshal(map[string]string{"key": key, "value": raw})
		w := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodPost, "/config/setting", bytes.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		r.ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Fatalf("保存 %s = %d %s", key, w.Code, w.Body.String())
		}
		var got, want map[string]any
		_ = json.Unmarshal([]byte(h.settingValueRaw(key)), &got)
		_ = json.Unmarshal([]byte(seeds[key]), &want)
		gb, _ := json.Marshal(got)
		wb, _ := json.Marshal(want)
		if string(gb) != string(wb) {
			t.Errorf("%s 回传掩码后被改了：\n got %s\nwant %s", key, gb, wb)
		}
	}

	// 「眼睛」按需取明文：专用分支与通用分支都还能用
	for _, c := range []struct{ key, field, want string }{
		{"re0", "client_secret", "cs"},
		{"re0", "password", "pw"},
		{"re0", "access_token", "at"},
		{"mukaku", "access_token", "at"},
		{"guanying", "password", "pw"},
	} {
		q := url.Values{"key": {c.key}, "field": {c.field}}
		if v := get("/config/secret?" + q.Encode())["value"]; v != c.want {
			t.Errorf("reveal %s.%s = %v, want %q", c.key, c.field, v, c.want)
		}
	}
}

func TestMaskSensitiveJSONCookieMap(t *testing.T) {
	// 空 Cookie 表不掩码（界面据此显示「未登录」）
	if got := maskSensitiveJSON(`{"cookies":{}}`); got != `{"cookies":{}}` {
		t.Errorf("空表被掩码：%s", got)
	}
	// 掩码回传还原成原来的整张表
	old := `{"cookies":{"a":"1"},"password":"p"}`
	if got := unmaskSensitiveJSON(maskSensitiveJSON(old), old); got != old {
		t.Errorf("回填 = %s, want %s", got, old)
	}
}
