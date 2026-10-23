package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"115-station/internal/config"
	"115-station/internal/model"

	"github.com/gin-gonic/gin"
)

func resetWebhookSeen() {
	webhookSeen.Lock()
	webhookSeen.v, webhookSeen.loaded = embyWebhookSeen{}, false
	webhookSeen.Unlock()
}

// Webhook 收到过什么：token 不对记被拒、删除与测试事件各记一笔，深度删除页据此提醒
func TestEmbyWebhookSeen(t *testing.T) {
	gin.SetMode(gin.TestMode)
	newTestDB(t, "whseen.db")
	resetWebhookSeen()
	t.Cleanup(resetWebhookSeen)
	cfg := &config.Config{DataDir: t.TempDir(), ConfigDir: t.TempDir()}
	h := &Handler{DB: model.DB, Config: cfg}

	status := func() map[string]any {
		w := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(w)
		c.Request = httptest.NewRequest(http.MethodGet, "/emby/webhook-status", nil)
		h.EmbyWebhookStatus(c)
		var out map[string]any
		_ = json.Unmarshal(w.Body.Bytes(), &out)
		return out
	}
	post := func(token, body string) int {
		w := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(w)
		c.Request = httptest.NewRequest(http.MethodPost, "/emby/webhook?token="+token, strings.NewReader(body))
		h.EmbyWebhook(c)
		return w.Code
	}

	if s := status(); s["token_set"] != false || s["any"] != "" {
		t.Fatalf("还没生成 token、什么也没收到：%v", s)
	}
	_ = cfg.SaveSetting("emby-notify", `{"token":"tk"}`)
	if post("bad", `{"Event":"library.deleted"}`) != http.StatusUnauthorized {
		t.Fatal("token 不对应拒收")
	}
	s := status()
	if s["token_set"] != true || s["reject_at"] == "" || !strings.Contains(s["reject_why"].(string), "不一致") || s["delete"] != "" {
		t.Fatalf("应记下被拒的原因、不算收到删除事件：%v", s)
	}
	post("tk", `{"Event":"system.notificationtest"}`)
	if s := status(); s["test"] == "" || s["any"] == "" || s["delete"] != "" {
		t.Fatalf("测试事件：%v", s)
	}
	// 写进了 setting：重启后还在
	resetWebhookSeen()
	if s := status(); s["test"] == "" {
		t.Fatalf("测试事件应持久化：%v", s)
	}
}

func TestSaveFullLastScan(t *testing.T) {
	cfg := &config.Config{DataDir: t.TempDir(), ConfigDir: t.TempDir()}
	h := &Handler{Config: cfg}
	p := fullParams{VideoExt: []string{".mkv"}, ImageExt: []string{".jpg"}, DataExt: []string{".srt"}}
	h.saveFullLastScan(p, map[string]libSyncStat{"1": {Error: "x"}})
	if cfg.GetSetting(fullLastScanKey) != "" {
		t.Fatal("一个库都没成功时不记")
	}
	h.saveFullLastScan(fullParams{AcctOnly: true, VideoExt: []string{".mp4"}}, map[string]libSyncStat{"1": {OK: true}})
	if cfg.GetSetting(fullLastScanKey) != "" {
		t.Fatal("附加账号的补同步不记")
	}
	h.saveFullLastScan(p, map[string]libSyncStat{"1": {OK: true}, "2": {Error: "x"}})
	var got fullLastScan
	_ = json.Unmarshal([]byte(cfg.GetSetting(fullLastScanKey)), &got)
	if got.At == 0 || len(got.VideoExt) != 1 || got.DataExt[0] != ".srt" {
		t.Fatalf("实得 %+v", got)
	}
}
