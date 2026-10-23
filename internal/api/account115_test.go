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

// 多个 115 账号的账号层（account115.go，MULTI-ACCOUNT-PLAN.md 阶段 2）

func acctSetup(t *testing.T) (*Handler, *gin.Engine) {
	t.Helper()
	newTestDB(t, "acct.db")
	cfg := &config.Config{DataDir: t.TempDir(), ConfigDir: t.TempDir()}
	h := &Handler{DB: model.DB, Config: cfg}
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.GET("/storage", h.ListStorage)
	r.DELETE("/storage/:id", h.DeleteStorage)
	r.GET("/storage/115/accounts", h.ListAccounts115)
	r.POST("/storage/115/accounts", h.CreateAccount115)
	r.DELETE("/storage/115/accounts/:id", h.DeleteAccount115)
	return h, r
}

func acctDo(t *testing.T, r *gin.Engine, method, path, body string) (int, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	var out map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &out)
	return w.Code, out
}

func TestMigrateMainAccount(t *testing.T) {
	h, _ := acctSetup(t)
	// 老部署：一行 type=115、没有 Main
	old := model.Storage{Name: "老用户", Type: "115", Status: "online"}
	h.DB.Create(&old)
	if m, err := mainStorage115(h.DB); err != nil || m.ID != old.ID {
		t.Fatalf("迁移前按老口径认第一行：%+v %v", m, err)
	}
	h.MigrateMainAccount()
	h.MigrateMainAccount() // 幂等
	var n int64
	h.DB.Model(&model.Storage{}).Where("main = ?", true).Count(&n)
	if n != 1 {
		t.Fatalf("应只有一行主号，实得 %d", n)
	}
	// 之后加的附加账号排在后面，不会被认成主号
	extra := model.Storage{Name: "副号", Type: "115"}
	h.DB.Create(&extra)
	if m, _ := mainStorage115(h.DB); m.ID != old.ID {
		t.Fatalf("主号认错了：%+v", m)
	}
}

func TestAccountsCRUD(t *testing.T) {
	h, r := acctSetup(t)
	// 没有主号时不许加附加账号
	if code, _ := acctDo(t, r, "POST", "/storage/115/accounts", `{}`); code != http.StatusBadRequest {
		t.Fatalf("没主号应拒绝，实得 %d", code)
	}
	h.DB.Create(&model.Storage{Name: "主人", Type: "115", Main: true, Status: "online"})

	code, out := acctDo(t, r, "POST", "/storage/115/accounts", `{}`)
	if code != http.StatusOK {
		t.Fatalf("添加失败：%d %v", code, out)
	}
	data := out["data"].(map[string]any)
	id := uint(data["id"].(float64))
	if data["name"] != "未登录账号" {
		t.Errorf("还没登录时的名字：%v", data["name"])
	}
	// 上次添加了没扫码：再点添加复用那一行，不多出一行
	if _, again := acctDo(t, r, "POST", "/storage/115/accounts", `{}`); uint(again["data"].(map[string]any)["id"].(float64)) != id {
		t.Error("没登录过的账号应复用")
	}
	// 登录后名字就是 115 用户名
	h.DB.Model(&model.Storage{}).Where("id = ?", id).Update("user_name", "小王的家")
	_, after := acctDo(t, r, "GET", "/storage/115/accounts", "")
	if rows := after["data"].([]any); rows[1].(map[string]any)["name"] != "小王的家" {
		t.Errorf("名字应取 115 用户名：%v", rows[1])
	}

	// 老接口 /storage 只给主号：老前端拿 type=115 的第一行当主号
	_, list := acctDo(t, r, "GET", "/storage", "")
	if rows := list["data"].([]any); len(rows) != 1 || rows[0].(map[string]any)["name"] != "主人" {
		t.Fatalf("/storage 不该列出附加账号：%v", list)
	}
	_, accts := acctDo(t, r, "GET", "/storage/115/accounts", "")
	if rows := accts["data"].([]any); len(rows) != 2 || rows[0].(map[string]any)["main"] != true {
		t.Fatalf("账号列表主号在前：%v", accts)
	}

	// 主号不能经附加账号的接口改、删；有附加账号时主号不能从老接口删
	main, _ := mainStorage115(h.DB)
	if code, _ := acctDo(t, r, "DELETE", "/storage/115/accounts/"+uitoa(main.ID), ""); code != http.StatusBadRequest {
		t.Error("附加账号接口不该删得了主号")
	}
	if code, _ := acctDo(t, r, "DELETE", "/storage/"+uitoa(main.ID), ""); code != http.StatusBadRequest {
		t.Error("还有附加账号时不该删得了主号")
	}
	if code, _ := acctDo(t, r, "DELETE", "/storage/"+uitoa(id), ""); code != http.StatusBadRequest {
		t.Error("老接口不该删附加账号")
	}

	_ = h.Config.SaveCookieOf(id, "UID=2_A1_x; CID=c; SEID=s")
	if code, _ := acctDo(t, r, "DELETE", "/storage/115/accounts/"+uitoa(id), ""); code != http.StatusOK {
		t.Fatal("删除失败")
	}
	if _, err := h.Config.LoadCookieOf(id); err == nil {
		t.Error("删账号要连 Cookie 文件一起删")
	}
}

func TestCookieOfAndReadOnlyOps(t *testing.T) {
	h, _ := acctSetup(t)
	_ = h.Config.SaveCookie("UID=1_A1_x; CID=c; SEID=s")
	h.DB.Create(&model.Storage{Name: "主人", Type: "115", Main: true})
	extra := model.Storage{Name: "副号", Type: "115"}
	h.DB.Create(&extra)

	if ck, err := h.cookieOf(acctMain); err != nil || !strings.HasPrefix(ck, "UID=1_") {
		t.Fatalf("0 = 主号：%q %v", ck, err)
	}
	if _, err := h.cookieOf(extra.ID); err == nil {
		t.Fatal("没登录的附加账号应报错")
	}
	_ = h.Config.SaveCookieOf(extra.ID, "UID=2_A1_y; CID=c; SEID=s")
	ops, err := h.opsFor(extra.ID)
	if err != nil || ops.acct != extra.ID || !strings.HasPrefix(ops.cookie, "UID=2_") {
		t.Fatalf("附加账号的通道：%+v %v", ops, err)
	}
	// 同一个 115 用户不能同时是主号和附加账号
	if dup := h.acctDupOf(extra.ID, "UID=1_B2_z; SEID=s"); dup != "主账号" {
		t.Errorf("和主号同一个用户：%q", dup)
	}
	if dup := h.acctDupOf(acctMain, "UID=2_B2_z; SEID=s"); !strings.Contains(dup, "副号") {
		t.Errorf("主号登录成附加账号的用户：%q", dup)
	}
	if dup := h.acctDupOf(extra.ID, "UID=3_B2_z; SEID=s"); dup != "" {
		t.Errorf("新用户不该算重复：%q", dup)
	}
}

func TestDirCacheKeyPerAccount(t *testing.T) {
	if dirCacheKey(acctMain, "123") != "123" {
		t.Error("主号沿用裸 cid（老缓存照用）")
	}
	if dirCacheKey(5, "123") == "123" {
		t.Error("附加账号的缓存键要和主号分开")
	}
}

func uitoa(n uint) string {
	b, _ := json.Marshal(n)
	return string(b)
}

// 挂着库的附加账号不能删：不然那些库的 STRM 指向一个不存在的账号（acctlibs.go）
func TestDeleteAccountWithLibs(t *testing.T) {
	h, r := acctSetup(t)
	h.DB.Create(&model.Storage{Name: "主人", Type: "115", Main: true})
	extra := model.Storage{Name: "副号", Type: "115"}
	h.DB.Create(&extra)
	_ = h.Config.SaveSetting(acctLibsKey, `[{"account":`+uitoa(extra.ID)+`,"cid":"9","local":"副号电影"}]`)
	code, out := acctDo(t, r, "DELETE", "/storage/115/accounts/"+uitoa(extra.ID), "")
	if code != http.StatusBadRequest || !strings.Contains(out["error"].(string), "副号电影") {
		t.Fatalf("挂着库应拒绝删除：%d %v", code, out)
	}
	_ = h.Config.SaveSetting(acctLibsKey, `[]`)
	if code, _ := acctDo(t, r, "DELETE", "/storage/115/accounts/"+uitoa(extra.ID), ""); code != http.StatusOK {
		t.Fatalf("移除库之后应能删：%d", code)
	}
}

// 挂着库的附加账号不许重新登录成另一个 115 用户（库里的 STRM 全是原来那个用户的 pickcode）
func TestSaveExtraCookieUserSwitch(t *testing.T) {
	h, _ := acctSetup(t)
	h.DB.Create(&model.Storage{Name: "主人", Type: "115", Main: true})
	extra := model.Storage{Name: "副号", Type: "115"}
	h.DB.Create(&extra)
	if _, err := h.saveExtraCookie(extra.ID, "UID=2_A1_x; SEID=s", "web", "二号"); err != nil {
		t.Fatal(err)
	}
	// 同一个用户重新登录：照常
	if w, err := h.saveExtraCookie(extra.ID, "UID=2_B2_y; SEID=s", "web", "二号"); err != nil || w != "" {
		t.Fatalf("同一用户重登：%q %v", w, err)
	}
	// 挂着库时换用户：拒绝，Cookie 不动
	_ = h.Config.SaveSetting(acctLibsKey, `[{"account":`+uitoa(extra.ID)+`,"cid":"9","local":"副号电影"}]`)
	if _, err := h.saveExtraCookie(extra.ID, "UID=3_A1_z; SEID=s", "web", "三号"); err == nil || !strings.Contains(err.Error(), "副号电影") {
		t.Fatalf("挂着库换用户应拒绝：%v", err)
	}
	if ck, _ := h.Config.LoadCookieOf(extra.ID); !strings.HasPrefix(ck, "UID=2_") {
		t.Fatalf("拒绝时 Cookie 不该被覆盖：%q", ck)
	}
	// 没有库时换用户：允许，给提醒
	_ = h.Config.SaveSetting(acctLibsKey, `[]`)
	if w, err := h.saveExtraCookie(extra.ID, "UID=3_A1_z; SEID=s", "web", "三号"); err != nil || w == "" {
		t.Fatalf("没挂库换用户应允许并提醒：%q %v", w, err)
	}
}
