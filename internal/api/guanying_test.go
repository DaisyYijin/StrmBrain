package api

import (
	"encoding/json"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
)

// TestGyPowLoopMath 验证 RSW 求解循环（t 次平方取模）等价于 x^(2^t) mod N
func TestGyPowLoopMath(t *testing.T) {
	cases := []struct {
		nHex, xHex string
		t          int
	}{
		{"61", "2", 5},
		{"9a7f3", "1c", 17},
		{"fffffffffffffffffffffffffffffffe", "abcdef0123456789", 33},
	}
	for _, c := range cases {
		n, _ := new(big.Int).SetString(c.nHex, 16)
		x, _ := new(big.Int).SetString(c.xHex, 16)
		exp := new(big.Int).Lsh(big.NewInt(1), uint(c.t))
		want := new(big.Int).Exp(x, exp, n)
		y := new(big.Int).Set(x)
		for i := 0; i < c.t; i++ {
			y.Mul(y, y)
			y.Mod(y, n)
		}
		if y.Cmp(want) != 0 {
			t.Errorf("RSW loop mismatch: N=%s x=%s t=%d got %s want %s", c.nHex, c.xHex, c.t, y.Text(16), want.Text(16))
		}
	}
}

// TestGyExtractTorrentsSample 用脱敏搜索页样本验证内嵌 _obj.search 解析。
// 样本保留站点真实的页面结构（_obj.* 内嵌 JSON、并行数组、数字型 seeds），
// 片名/ID/用户名全部换成占位值——真实抓包含站点账号与版权片源，不入库。
func TestGyExtractTorrentsSample(t *testing.T) {
	raw, err := os.ReadFile("testdata/gy_search.html")
	if err != nil {
		t.Skipf("样本缺失: %v", err)
	}
	items := gyExtractTorrents(string(raw))
	if len(items) == 0 {
		t.Fatalf("样本解析出 0 个条目")
	}
	var found bool
	for _, it := range items {
		if title, _ := it["title"].(string); strings.HasPrefix(title, "示例影片2") && it["size"] == "10.49G" {
			found = true
		}
	}
	if !found {
		t.Errorf("未解析到示例影片2种子（10.49G），得到 %v", items)
	}
}

// TestGyExtractMagnetSample 用脱敏种子详情页样本验证磁力提取
func TestGyExtractMagnetSample(t *testing.T) {
	raw, err := os.ReadFile("testdata/gy_detail.html")
	if err != nil {
		t.Skipf("样本缺失: %v", err)
	}
	body := string(raw)
	m := reGyMagnet.FindStringSubmatch(body)
	if m == nil {
		t.Fatalf("样本未提取到磁力链接")
	}
	if !strings.HasPrefix(m[0], "magnet:?xt=urn:btih:0123456789ABCDEF") {
		t.Errorf("磁力哈希异常: %s", m[0])
	}
	title := ""
	if objStr, ok := gyObjJSON(body, "d"); ok {
		var d struct {
			Title string `json:"title"`
		}
		if json.Unmarshal([]byte(objStr), &d) == nil {
			title = d.Title
		}
	}
	if !strings.Contains(title, "示例影片2") {
		t.Errorf("标题解析异常: %q", title)
	}
}

// TestGyObjJSONBalanced 验证花括号配平扫描（含字符串内转义与花括号）
func TestGyObjJSONBalanced(t *testing.T) {
	body := `xx _obj.d={"title":"a\"b{brace}","n":2}; _obj.footer={t:1};`
	objStr, ok := gyObjJSON(body, "d")
	if !ok {
		t.Fatal("未提取到 _obj.d")
	}
	if !strings.HasPrefix(objStr, `{"title"`) || !strings.HasSuffix(objStr, `"n":2}`) {
		t.Errorf("提取范围异常: %q", objStr)
	}
}

// 登录按钮先保存配置、再发登录请求：请求体里没带账号密码时要用已保存的，
// 不能回「请填写账号和密码」（此前前端发空请求体，登录永远失败）
func TestGyLoginFallsBackToSavedCredentials(t *testing.T) {
	gin.SetMode(gin.TestMode)
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError) // 登录走不下去即可，只看有没有被参数校验拦下
	}))
	defer site.Close()
	setCfg := func(c *gyCfg) {
		gyCfgMu.Lock()
		gyCfgV, gyCfgAt = c, time.Now()
		gyCfgMu.Unlock()
	}
	t.Cleanup(func() { setCfg(nil) })
	h := &Handler{}
	post := func(body string) *httptest.ResponseRecorder {
		w := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(w)
		c.Request = httptest.NewRequest(http.MethodPost, "/guanying/login", strings.NewReader(body))
		c.Request.Header.Set("Content-Type", "application/json")
		h.GyLogin(c)
		return w
	}

	setCfg(&gyCfg{BaseURL: site.URL, Cookies: map[string]string{}})
	if w := post(`{}`); w.Code != http.StatusBadRequest {
		t.Fatalf("没有已保存的账号时应当 400: %d %s", w.Code, w.Body)
	}
	setCfg(&gyCfg{BaseURL: site.URL, Username: "u", Password: "p", Cookies: map[string]string{}})
	if w := post(`{}`); w.Code == http.StatusBadRequest {
		t.Fatalf("空请求体应当用已保存的账号去登录: %s", w.Body)
	}
	setCfg(&gyCfg{BaseURL: site.URL, Username: "u", Password: "p", Cookies: map[string]string{}})
	if w := post(`{"username":"u","password":"` + settingMask + `"}`); w.Code == http.StatusBadRequest {
		t.Fatalf("密码是掩码时应当用已存密码: %s", w.Body)
	}
}

// TestGyParseTitles 影片搜索页的 _obj.search：年份是数字，类型 mv / tv / ac
func TestGyParseTitles(t *testing.T) {
	body := `<script>_obj.search={"q":"希望","n":"3","l":{"title":["希望 HOPE","希望 望み","马萨卡的孩子：舞出希望"],"year":[2026,2020,0],"d":["mv","mv","mv"],"i":["a1","b2","c3"]}};</script>`
	got := gyParseTitles(body)
	if len(got) != 3 || got[0].ID != "a1" || got[0].Year != "2026" || got[0].D != "mv" || got[2].Year != "" {
		t.Fatalf("解析结果不对: %+v", got)
	}
}

// TestGyPickTitle 现场：搜「希望」出来一串，只认类型、片名、年份都对得上的《希望 HOPE (2026)》
func TestGyPickTitle(t *testing.T) {
	list := []gyTitle{
		{D: "mv", ID: "x", Title: "马萨卡的孩子：舞出希望", Year: "2025"},
		{D: "mv", ID: "b", Title: "希望 望み", Year: "2020"},
		{D: "mv", ID: "c", Title: "希望 Hope", Year: "2014"},
		{D: "tv", ID: "d", Title: "希望", Year: "2026"},
		{D: "mv", ID: "a", Title: "希望 HOPE", Year: "2026"},
	}
	q := resQuery{TmdbID: 1, Type: "movie", Title: "希望", OrigTitle: "호프", Year: "2026"}
	if p := gyPickTitle(q, list); p == nil || p.ID != "a" {
		t.Fatalf("挑错了: %+v", p)
	}
	// 年份差一年照认（豆瓣首映与 TMDB 上映常差一年）
	if p := gyPickTitle(resQuery{TmdbID: 1, Type: "movie", Title: "希望", Year: "2025"}, list[4:]); p == nil {
		t.Fatalf("差一年应当认")
	}
	// 一部都对不上就不挑
	if p := gyPickTitle(resQuery{TmdbID: 1, Type: "movie", Title: "希望", Year: "2010"}, list); p != nil {
		t.Fatalf("年份都对不上不该挑: %+v", p)
	}
	// 剧集认 tv / ac，不认 mv
	if p := gyPickTitle(resQuery{TmdbID: 1, Type: "tv", Title: "希望", Year: "2026"}, list); p == nil || p.ID != "d" {
		t.Fatalf("剧集挑错了: %+v", p)
	}
}

// TestGyDetailItems /res/downurl 的磁力拼链接、网盘按链接形态定类型
func TestGyDetailItems(t *testing.T) {
	raw := `{"code":200,"downlist":{"imdb":"tt1","list":{"m":["0123456789ABCDEF0123456789ABCDEF01234567","bad"],"t":["Hope.2026.1080p.WEB-DL.mkv","x"],"s":["1.99GB","1G"],"n":["昨天","1天前"]}},
	"panlist":{"name":["希望 4K"],"p":["abcd"],"url":["https://115cdn.com/s/swabc123"],"time":["2026-10-12"],"tname":["115"]}}`
	var d gyDetail
	if err := json.Unmarshal([]byte(raw), &d); err != nil {
		t.Fatal(err)
	}
	items := gyDetailItems(&d)
	if len(items) != 2 {
		t.Fatalf("条目数不对: %+v", items)
	}
	m := items[0]
	if m.Kind != "magnet" || !strings.HasPrefix(m.URL, "magnet:?xt=urn:btih:0123456789abcdef") || m.Size != "1.99GB" || m.TimeUnix == 0 {
		t.Errorf("磁力条目不对: %+v", m)
	}
	if s := items[1]; s.Kind != "share115" || s.Action != "transfer" || s.Code != "abcd" {
		t.Errorf("115 分享条目不对: %+v", s)
	}
}

// TestGyCode403 详情接口未登录回 JSON code=403，要触发重登
func TestGyCode403(t *testing.T) {
	if !gyCode403(`{"code":403,"msg":"x"}`) || gyCode403(`{"code":200}`) || gyCode403(`<html>`) {
		t.Fatal("gyCode403 判断不对")
	}
}

// 站点要验证码时回 409 带图；用户点完带坐标再登，要先用同一个会话核对、再把结果串当 code 登录
func TestGyLoginCaptchaFlow(t *testing.T) {
	gin.SetMode(gin.TestMode)
	var gotCode, gotCheck string
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		r.ParseForm()
		sess, _ := r.Cookie("sess")
		switch r.URL.Path {
		case "/res/captcha/2":
			if r.Form.Get("do") == "check" {
				if sess == nil || sess.Value != "s1" {
					w.Write([]byte(`{"code":0}`))
					return
				}
				gotCheck = r.Form.Get("info")
				w.Write([]byte(`{"code":200}`))
				return
			}
			w.Write([]byte(`{"type":"jpeg","text":"甲乙丙","img":"QUJD"}`))
		case "/user/login":
			if r.Form.Get("code") == "" {
				http.SetCookie(w, &http.Cookie{Name: "sess", Value: "s1"})
				w.Write([]byte(`{"code":403,"msg":"验证码错误","captcha":2}`))
				return
			}
			gotCode = r.Form.Get("code")
			w.Write([]byte(`{"code":200,"msg":"ok"}`))
		}
	}))
	defer site.Close()
	gyCfgMu.Lock()
	gyCfgV, gyCfgAt = &gyCfg{BaseURL: site.URL, Username: "u", Password: "p", Cookies: map[string]string{}}, time.Now()
	gyCfgMu.Unlock()
	t.Cleanup(func() {
		gyCfgMu.Lock()
		gyCfgV = nil
		gyCfgMu.Unlock()
		gyPendSet(nil)
	})
	h := &Handler{}
	post := func(body string) *httptest.ResponseRecorder {
		w := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(w)
		c.Request = httptest.NewRequest(http.MethodPost, "/guanying/login", strings.NewReader(body))
		c.Request.Header.Set("Content-Type", "application/json")
		h.GyLogin(c)
		return w
	}
	w := post(`{}`)
	if w.Code != http.StatusConflict || !strings.Contains(w.Body.String(), `"need_captcha":true`) ||
		!strings.Contains(w.Body.String(), `data:image/jpeg;base64,QUJD`) || !strings.Contains(w.Body.String(), "甲乙丙") {
		t.Fatalf("要验证码时应回 409 带图: %d %s", w.Code, w.Body)
	}
	// 保存会话那一步没有配置源，走到它说明登录已经过了
	w = post(`{"captcha":[[10,20],[30,40],[50,60]]}`)
	want := "10,20-30,40-50,60;350;200"
	if gotCheck != want || gotCode != want {
		t.Fatalf("核对 / 登录的结果串不对: check=%q code=%q (%d %s)", gotCheck, gotCode, w.Code, w.Body)
	}
}

func TestGyNeedCaptcha(t *testing.T) {
	cases := []struct {
		raw, msg string
		want     bool
	}{
		{``, "账号不存在", false},
		{`0`, "密码错误", false},
		{`2`, "密码错误", true},
		{`"1"`, "", true},
		{`null`, "验证码错误", true},
	}
	for _, c := range cases {
		if got := gyNeedCaptcha(json.RawMessage(c.raw), c.msg); got != c.want {
			t.Errorf("%q %q = %v", c.raw, c.msg, got)
		}
	}
}
