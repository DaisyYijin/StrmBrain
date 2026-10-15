package api

// ==================== 癫影（m.dian115.com，站名 MADOW）网页通道：浏览器里调站点自己的接口 ====================
//
// 站点是 Vue 单页应用，数据全走 /api/portal/*。两道门：
//   - Cloudflare WAF：非浏览器的 TLS 指纹直接 403（连静态 JS 不带 UA 都会被拦），没有交互式验证页；
//   - 站点自己的「浏览器证明」：GET /auth/browser-challenge 拿 proof，再把页面里生成的 ECDSA P-256 公钥
//     POST /auth/browser-session 登记，之后每个请求带 X-Portal-Browser-TS / Nonce / Sig，
//     签的是 "portal-browser-request/v1\n方法\n路径\n时间戳\nnonce"（WebCrypto 的 r||s，base64url）。
//     无头浏览器的 UA 带 HeadlessChrome 时 challenge 本身就回 403 browser required（CloakBrowser 没这个问题）。
//
// 所以和 RE0 一样用 CloakBrowser（re0browser.go 下载的那一个，进程、用户数据目录、操作锁都共用），
// 但不点页面：打开首页后在页面里注入 dianSignJS，用站点同一套签名直接调接口。2026-10-15 实测：
//   - 资源列表（resource-detail）不登录也能读，解锁 / 签到 / 我的信息要登录；
//   - 登录是 POST /auth/login {email, password}，会话在 HttpOnly Cookie 里，用户数据目录持久化后不用每次登；
//   - 签名私钥存在 IndexedDB（portal-browser-security-v1 / keys / request-signing-p256），
//     这里读写同一条：站点的会话可能绑定这把钥匙，页面自己的代码与我们用的必须是同一把。
//
// 接口与字段来自站点前端代码（/assets/index-*.js、ResourceDetail-*.js、ShareAcquire-*.js），站点改版后先看那里。
// 站点也有 OpenAPI（/openapi/*），只给审批通过的开发者、要用站方下发的封包 SDK，普通用户用不了。

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"strings"
	"time"

	"github.com/go-rod/rod"
	"github.com/go-rod/rod/lib/proto"
)

const dianBase = "https://m.dian115.com"

// dianSignJS 注入页面：window.__s115dian(method, path, body) → {status, text}，path 相对 /api/portal。
// 照抄站点前端 axios 拦截器的做法；proof 失效（browser_proof_required / invalid）时重新登记一次再发
const dianSignJS = `() => {
  if (window.__s115dian) return
  const b64u = (buf) => { let s = ''; const a = new Uint8Array(buf); for (let i = 0; i < a.length; i++) s += String.fromCharCode(a[i]); return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '') }
  const DB = 'portal-browser-security-v1', STORE = 'keys', KID = 'request-signing-p256'
  const st = { key: null, proof: null, proofExp: 0, sess: false, skew: 0 }
  const base = { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' }
  const openDB = () => new Promise((ok, bad) => {
    const r = indexedDB.open(DB, 1)
    r.onupgradeneeded = () => { if (!r.result.objectStoreNames.contains(STORE)) r.result.createObjectStore(STORE, { keyPath: 'id' }) }
    r.onsuccess = () => ok(r.result); r.onerror = () => bad(r.error)
  })
  async function loadKey() {
    if (st.key) return st.key
    try {
      const db = await openDB()
      const rec = await new Promise((ok) => { const q = db.transaction(STORE, 'readonly').objectStore(STORE).get(KID); q.onsuccess = () => ok(q.result || null); q.onerror = () => ok(null) })
      db.close()
      if (rec && rec.privateKey && rec.publicJWK && rec.publicJWK.x) { st.key = rec; return rec }
    } catch (e) {}
    const kp = await crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign', 'verify'])
    const jwk = await crypto.subtle.exportKey('jwk', kp.publicKey)
    const rec = { id: KID, privateKey: kp.privateKey, publicJWK: jwk }
    try {
      const db = await openDB()
      await new Promise((ok) => { const t = db.transaction(STORE, 'readwrite'); t.objectStore(STORE).put(rec); t.oncomplete = ok; t.onerror = ok; t.onabort = ok })
      db.close()
    } catch (e) {}
    st.key = rec
    return rec
  }
  async function proof(force) {
    if (!force && st.proof && st.proofExp - Date.now() > 30000) return st.proof
    const r = await fetch('/api/portal/auth/browser-challenge', { credentials: 'include', headers: base })
    const j = await r.json().catch(() => ({}))
    if (j.enabled === false || !j.proof) { st.proof = null; return null }
    st.proof = String(j.proof)
    st.proofExp = j.expires_at ? Date.parse(j.expires_at) : Date.now() + Math.max(Number(j.ttl) || 600, 60) * 1000
    return st.proof
  }
  async function session(pf, force) {
    if (st.sess && !force) return
    const k = await loadKey()
    const h = { ...base }; if (pf) h['X-Portal-Browser-Proof'] = pf
    const { kty, crv, x, y } = k.publicJWK
    const r = await fetch('/api/portal/auth/browser-session', { method: 'POST', credentials: 'include', headers: h, body: JSON.stringify({ public_jwk: { kty, crv, x, y } }) })
    const j = await r.json().catch(() => ({}))
    const srv = Number(j.server_time_ms)
    st.skew = Number.isFinite(srv) ? srv - Date.now() : 0
    st.sess = true
  }
  async function call(method, path, body, retried) {
    const pf = await proof(retried)
    await session(pf, retried)
    const u = '/api/portal' + path
    const pathname = new URL(u, location.origin).pathname
    const ts = String(Math.round(Date.now() + st.skew))
    const nb = new Uint8Array(24); crypto.getRandomValues(nb); const nonce = b64u(nb)
    const msg = 'portal-browser-request/v1\n' + method.toUpperCase() + '\n' + pathname + '\n' + ts + '\n' + nonce
    const k = await loadKey()
    const sig = await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, k.privateKey, new TextEncoder().encode(msg))
    const h = { ...base, 'X-Portal-Browser-TS': ts, 'X-Portal-Browser-Nonce': nonce, 'X-Portal-Browser-Sig': b64u(sig) }
    if (pf) h['X-Portal-Browser-Proof'] = pf
    const r = await fetch(u, { method, credentials: 'include', headers: h, body: body == null ? undefined : JSON.stringify(body) })
    const text = await r.text()
    if (!retried && r.status === 403 && /browser_proof/.test(text)) return call(method, path, body, true)
    return { status: r.status, text }
  }
  window.__s115dian = (method, path, body) => call(method, path, body, false)
}`

// dianCaller 一次网页操作里调接口用
type dianCaller struct {
	p *rod.Page
}

// dianAPIErr 站点接口返回的错误（code 非 ok）
type dianAPIErr struct {
	Status int
	Code   string
	Msg    string
}

func (e *dianAPIErr) Error() string {
	if e.Msg != "" {
		return e.Msg
	}
	if e.Code != "" {
		return fmt.Sprintf("%s（HTTP %d）", e.Code, e.Status)
	}
	return fmt.Sprintf("HTTP %d", e.Status)
}

// needLogin 没登录 / 登录失效
func (e *dianAPIErr) needLogin() bool {
	return e.Status == 401 || e.Code == "no_token" || e.Code == "invalid_token" || e.Code == "token_revoked" || e.Code == "unauthorized"
}

// do 调一个接口，返回原始 JSON；code 不是 ok（或 HTTP 非 2xx）时返回 *dianAPIErr
func (c *dianCaller) do(method, path string, body any) ([]byte, error) {
	var arg any = nil
	if body != nil {
		arg = body
	}
	res, err := c.p.Eval(`(m, p, b) => window.__s115dian(m, p, b)`, method, path, arg)
	if err != nil {
		return nil, fmt.Errorf("页面里调用 %s 失败: %v", path, err)
	}
	status := res.Value.Get("status").Int()
	text := res.Value.Get("text").Str()
	var env struct {
		Code string `json:"code"`
		Msg  string `json:"msg"`
	}
	if json.Unmarshal([]byte(text), &env) != nil {
		// Cloudflare 拦截页之类：不是 JSON
		snippet := strings.TrimSpace(text)
		if strings.Contains(snippet, "Cloudflare") {
			return nil, &dianAPIErr{Status: status, Msg: fmt.Sprintf("被 Cloudflare 拦下（HTTP %d），稍后再试或换个网络", status)}
		}
		return nil, &dianAPIErr{Status: status, Msg: fmt.Sprintf("站点返回了非 JSON 内容（HTTP %d）: %s", status, truncateStr(snippet, 80))}
	}
	if status < 200 || status >= 300 || (env.Code != "" && env.Code != "ok") {
		return nil, &dianAPIErr{Status: status, Code: env.Code, Msg: env.Msg}
	}
	return []byte(text), nil
}

// dianMe /me 里用得到的几项
type dianMe struct {
	ID       int    `json:"id"`
	Nickname string `json:"nickname"`
	Points   int    `json:"points"`
	VIP      bool   `json:"vip"`
}

func (c *dianCaller) me() (*dianMe, error) {
	raw, err := c.do("GET", "/me", nil)
	if err != nil {
		return nil, err
	}
	var out struct {
		User dianMe `json:"user"`
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("解析账号信息失败: %v", err)
	}
	return &out.User, nil
}

// ensureLogin 读一次 /me，没登录就用账号密码登录（会话 Cookie 存在用户数据目录里，下次不用再登）
func (c *dianCaller) ensureLogin(cfg *dianCfg) (*dianMe, error) {
	me, err := c.me()
	if err == nil {
		log.Printf("[癫影] ○ 登录态有效（%s）", me.Nickname)
		return me, nil
	}
	var ae *dianAPIErr
	if !errors.As(err, &ae) || !ae.needLogin() {
		return nil, err
	}
	re0Step("登录癫影")
	log.Printf("[癫影] ○ 未登录，登录账号 %s", re0MaskUser(cfg.Email))
	if raw, err := c.do("GET", "/auth/policy", nil); err == nil {
		var pol struct {
			Turnstile bool `json:"turnstile_enabled"`
		}
		if json.Unmarshal(raw, &pol) == nil && pol.Turnstile {
			return nil, fmt.Errorf("癫影登录现在要求人机验证，本站暂时没法自动登录")
		}
	}
	if _, err := c.do("POST", "/auth/login", map[string]string{"email": cfg.Email, "password": cfg.Password}); err != nil {
		if errors.As(err, &ae) && ae.Code == "email_not_verified" {
			return nil, fmt.Errorf("登录失败：邮箱还没验证，请先去邮箱点验证链接")
		}
		return nil, fmt.Errorf("登录失败：%v", err)
	}
	me, err = c.me()
	if err != nil {
		return nil, fmt.Errorf("登录后读取账号失败：%v", err)
	}
	log.Printf("[癫影] ✓ 已登录（%s，积分 %d）", me.Nickname, me.Points)
	return me, nil
}

// dianWeb 一次网页操作：起（复用）浏览器 → 打开首页 → 注入签名 → login=true 时确保已登录 → fn。
// 与 RE0 共用浏览器进程与 re0OpMu：同一个浏览器同一时间只做一件事
func dianWeb(what string, login bool, timeout time.Duration, fn func(c *dianCaller) error) error {
	cfg := loadDianCfg()
	if login && !dianConfigured(cfg) {
		return fmt.Errorf("未配置癫影账号密码")
	}
	if why := re0BrowserBlocked("癫影" + what + "时发现浏览器没装"); why != "" {
		return errors.New(why)
	}
	if !re0OpMu.TryLock() {
		if cur := re0OpSnapshot(); cur != nil {
			log.Printf("[癫影] ○ %s 排队：等「%s」做完", what, cur.What)
		}
		re0OpMu.Lock()
	}
	defer re0OpMu.Unlock()
	start := time.Now()
	re0OpStMu.Lock()
	re0OpSt = &re0OpState{What: "癫影" + what, Step: "准备", Since: start.Unix()}
	re0OpStMu.Unlock()
	defer func() {
		re0OpStMu.Lock()
		re0OpSt = nil
		re0OpStMu.Unlock()
		re0BrMu.Lock()
		re0BrLastUse = time.Now()
		re0BrMu.Unlock()
	}()
	err := dianWebRun(cfg, what, login, timeout, fn)
	if err != nil {
		log.Printf("[癫影] ✗ %s 失败（用时 %s）: %v", what, time.Since(start).Round(time.Millisecond), err)
		return err
	}
	log.Printf("[癫影] ✓ %s（用时 %s）", what, time.Since(start).Round(time.Millisecond))
	return nil
}

func dianWebRun(cfg *dianCfg, what string, login bool, timeout time.Duration, fn func(c *dianCaller) error) (retErr error) {
	b, err := re0StartBrowser()
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	page, err := b.Page(proto.TargetCreateTarget{URL: "about:blank"})
	if err != nil {
		re0CloseBrowser("开新页面失败")
		return fmt.Errorf("开页面失败: %v", err)
	}
	defer page.Close()
	defer func() {
		if retErr != nil {
			re0SaveDebug(page, "癫影"+what)
		}
	}()
	p := page.Context(ctx)
	_ = (proto.EmulationSetDeviceMetricsOverride{Width: 1920, Height: 947, DeviceScaleFactor: 1}).Call(p)
	re0Step("打开癫影首页")
	if err := p.Navigate(dianBase + "/"); err != nil {
		return fmt.Errorf("打开癫影失败: %v", err)
	}
	wctx, wcancel := context.WithTimeout(ctx, 30*time.Second)
	_ = p.Context(wctx).WaitLoad()
	wcancel()
	// 现在没有 Cloudflare 验证页，万一站方打开了也照 RE0 那样等它过
	if err := re0WaitCloudflare(p); err != nil {
		return err
	}
	if _, err := p.Eval(dianSignJS); err != nil {
		return fmt.Errorf("注入签名脚本失败: %v", err)
	}
	c := &dianCaller{p: p}
	if login {
		re0Step("确认登录状态")
		if _, err := c.ensureLogin(cfg); err != nil {
			return err
		}
	}
	re0Step("%s", what)
	err = fn(c)
	if err != nil && ctx.Err() != nil {
		return fmt.Errorf("操作超时（%s）: %v", timeout, err)
	}
	return err
}
