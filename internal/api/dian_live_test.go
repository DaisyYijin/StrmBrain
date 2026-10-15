//go:build dianlive

package api

// 对真站点的冒烟测试：站点改版后先跑它，看签名、列资源、解析是不是还对得上。
// 不进 CI（要联网、要浏览器）：
//
//	DIAN_LIVE_BROWSER="C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" go test -tags dianlive -run DianLive ./internal/api/ -v
//
// 用本机的 Chrome / Edge 起无头浏览器并改掉 UA（无头 UA 带 HeadlessChrome 时站点直接回 browser required），
// 生产环境用的是 CloakBrowser，不需要这一步。只测不登录也能做的事：签名、列资源、解析。

import (
	"os"
	"sort"
	"strconv"
	"testing"
	"time"

	"github.com/go-rod/rod"
	"github.com/go-rod/rod/lib/launcher"
)

func TestDianLive(t *testing.T) {
	bin := os.Getenv("DIAN_LIVE_BROWSER")
	if bin == "" {
		t.Skip("没设 DIAN_LIVE_BROWSER")
	}
	u, err := launcher.New().Bin(bin).Leakless(false).Headless(true).
		Set("user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36").
		Launch()
	if err != nil {
		t.Fatal(err)
	}
	b := rod.New().ControlURL(u).NoDefaultDevice()
	if err := b.Connect(); err != nil {
		t.Fatal(err)
	}
	defer b.Close()
	p := b.MustPage(dianBase + "/").Timeout(90 * time.Second)
	_ = p.WaitLoad()
	if _, err := p.Eval(dianSignJS); err != nil {
		t.Fatalf("注入签名脚本: %v", err)
	}
	c := &dianCaller{p: p}

	// 没登录：/me 应该认得出是「要登录」
	_, err = c.me()
	if ae, ok := err.(*dianAPIErr); !ok || !ae.needLogin() {
		t.Fatalf("未登录读 /me 应要求登录，得到 %v", err)
	}

	for _, q := range []struct {
		typ string
		id  int
	}{{"movie", 27205}, {"tv", 1396}} {
		raw, err := c.do("GET", "/resource-detail?key="+dianResourceKey(q.typ, q.id), nil)
		if err != nil {
			t.Fatalf("%s/%d 列资源: %v", q.typ, q.id, err)
		}
		shares, err := parseDianDetail(raw)
		if err != nil || len(shares) == 0 {
			t.Fatalf("%s/%d 解析: %d 条, %v", q.typ, q.id, len(shares), err)
		}
		offline, withFiles := 0, 0
		for _, s := range shares {
			it := dianItemOf(s)
			if it.Points == nil || it.Ref == "" || it.Title == "" {
				t.Fatalf("%s/%d 分享 %d 缺字段: %+v", q.typ, q.id, s.ID, it)
			}
			if it.offline() {
				offline++
			}
			if len(it.files) > 0 {
				withFiles++
			}
		}
		t.Logf("%s/%d：%d 条分享（离线 %d，带文件清单 %d），第一条「%s」%d 积分",
			q.typ, q.id, len(shares), offline, withFiles, dianItemOf(shares[0]).Title, *dianItemOf(shares[0]).Points)
	}

	// ---- 以下要账号：DIAN_EMAIL / DIAN_PASSWORD ----
	cfg := &dianCfg{Email: os.Getenv("DIAN_EMAIL"), Password: os.Getenv("DIAN_PASSWORD")}
	if !dianConfigured(cfg) {
		t.Log("没设 DIAN_EMAIL / DIAN_PASSWORD，跳过登录、签到、解锁")
		return
	}
	me, err := c.ensureLogin(cfg)
	if err != nil {
		t.Fatalf("登录: %v", err)
	}
	t.Logf("登录成功：%s，积分 %d，VIP %v", me.Nickname, me.Points, me.VIP)
	// 再读一次 /me：确认登录态在后续签名请求里还认（会话没和签名钥匙脱节）
	if _, err := c.me(); err != nil {
		t.Fatalf("登录后再读 /me: %v", err)
	}

	// 登录后列一部片：看「已解锁」认不认得出，顺便列出最便宜的几条，挑一条给 DIAN_UNLOCK_REF
	tmdb := 27205
	if v, err := strconv.Atoi(os.Getenv("DIAN_TMDB")); err == nil && v > 0 {
		tmdb = v
	}
	typ := firstNonEmpty(os.Getenv("DIAN_TYPE"), "movie")
	raw, err := c.do("GET", "/resource-detail?key="+dianResourceKey(typ, tmdb), nil)
	if err != nil {
		t.Fatalf("登录后列资源: %v", err)
	}
	shares, _ := parseDianDetail(raw)
	sort.SliceStable(shares, func(i, j int) bool { return dianCost(shares[i]) < dianCost(shares[j]) })
	for i, s := range shares {
		if i >= 8 {
			break
		}
		it := dianItemOf(s)
		t.Logf("  ref=%s  %d 积分  已解锁=%v  %s  %s", it.Ref, dianCost(s), it.Owned, it.Kind, truncateStr(it.Title, 60))
	}

	if os.Getenv("DIAN_CHECKIN") == "1" {
		raw, err := c.do("POST", "/signin", map[string]string{"mode": "normal"})
		switch {
		case err != nil && dianAlreadySigned(err):
			t.Logf("签到：今日已签（%v）", err)
		case err != nil:
			t.Fatalf("签到: %v", err)
		default:
			t.Logf("签到：%s（原始返回 %s）", dianCheckinText(raw, -1), truncateStr(string(raw), 200))
		}
	}

	// 解锁会扣积分：只解锁明确点名的那一条
	if ref := os.Getenv("DIAN_UNLOCK_REF"); ref != "" {
		shareID, resourceID, ok := parseDianRef(ref)
		if !ok {
			t.Fatalf("DIAN_UNLOCK_REF 格式是 分享id:资源id，得到 %q", ref)
		}
		raw, err := c.do("POST", "/unlock", map[string]int{"share_id": shareID, "resource_id": resourceID})
		if err != nil {
			t.Fatalf("解锁: %v", err)
		}
		t.Logf("解锁原始返回：%s", truncateStr(string(raw), 600))
		u, err := parseDianUnlock(raw)
		if err != nil {
			t.Fatalf("解析解锁结果: %v", err)
		}
		t.Logf("解锁结果：分享=%q 提取码=%q 离线链接=%d 条 之前解锁过=%v", u.Share, u.Code, len(u.Links), u.Again)
	}
}

func dianCost(s dianShare) int {
	if s.UnlockCost == nil {
		return 1 << 30
	}
	return *s.UnlockCost
}
