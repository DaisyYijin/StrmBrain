package api

import (
	"os"
	"strings"
	"testing"
)

// 签名清单是从 GitHub 取的真文件（chromium-v146.0.7680.177.5），验的是写死的官方公钥
func TestCloakVerifySignature(t *testing.T) {
	sums, err := os.ReadFile("testdata/cloak/SHA256SUMS")
	if err != nil {
		t.Fatal(err)
	}
	sig, err := os.ReadFile("testdata/cloak/SHA256SUMS.sig")
	if err != nil {
		t.Fatal(err)
	}
	if err := cloakVerifySignature(sums, sig); err != nil {
		t.Fatalf("真签名应当通过: %v", err)
	}
	if v := cloakManifestVersion(string(sums)); v != "146.0.7680.177.5" {
		t.Fatalf("manifest 版本 = %q", v)
	}
	if h := cloakParseChecksums(string(sums))["cloakbrowser-linux-x64.tar.gz"]; len(h) != 64 {
		t.Fatalf("没解析出 linux-x64 的 sha256: %q", h)
	}
	// 改一个字节就不认
	bad := []byte(strings.Replace(string(sums), "4a12", "4a13", 1))
	if err := cloakVerifySignature(bad, sig); err == nil {
		t.Fatal("篡改过的清单不该通过")
	}
	if err := cloakVerifySignature(sums, []byte("not-base64!!")); err == nil {
		t.Fatal("坏签名不该通过")
	}
}

func TestParseRe0CheckinToast(t *testing.T) {
	cases := []struct {
		text, status string
		ok, done     bool
	}{
		{"签到成功\n获得 6 积分", "success", true, true},
		{"签到失败\n你今天已经签到过了，明天再来吧", "error", true, true}, // 已签算这天完成
		{"签到失败\n请先登录", "error", false, true},
		{"保存成功", "", false, false}, // 不是签到结果，继续等
		{"Check-in successful", "", true, true},
		{"", "success", false, false},
	}
	for _, c := range cases {
		ok, msg, done := parseRe0CheckinToast(c.text, c.status)
		if ok != c.ok || done != c.done {
			t.Errorf("%q/%s → ok=%v done=%v msg=%q，期望 ok=%v done=%v", c.text, c.status, ok, done, msg, c.ok, c.done)
		}
	}
}

func TestIsRe0ShareURL(t *testing.T) {
	for u, want := range map[string]bool{
		"https://115cdn.com/s/swabc123?password=x1y2": true,
		"https://115.com/s/abc":                       true,
		"https://anxia.com/s/abc/":                    true,
		"https://115.com/s/abc/def":                   false,
		"https://evil.com/s/abc":                      false,
		"https://u:p@115.com/s/abc":                   false,
		"ftp://115.com/s/abc":                         false,
	} {
		if got := isRe0ShareURL(u); got != want {
			t.Errorf("%s → %v，期望 %v", u, got, want)
		}
	}
}

// RSC 数据：分组对象带 groupData（引用另一条记录），里面按网盘分，取 "115"；行里的字段也可能是引用
func TestParseRe0PageResources(t *testing.T) {
	raw := strings.Join([]string{
		`0:["$","div",null,{"children":["$","Resources",null,{"groupData":"$a"}]}]`,
		`a:{"115":["$b",{"slug":"s-2","website":"115","remark":"沙丘2 4K","share_size":"20GB","unlock_points":null,"video_resolution":["2160P"],"source":["WEB-DL/WEBRip"],"subtitle_language":["简中"],"submitted_at":"2026-10-01"}],"quark":[{"slug":"q-1","website":"quark"}]}`,
		`b:{"slug":"s-1","website":"115","title":"沙丘2 1080p","unlock_points":5,"share_size":"$c","is_official":true}`,
		`c:"8.5GB"`,
		`d:Tsome text chunk that is not json`,
	}, "\n")
	rows, ok := parseRe0PageResources(raw)
	if !ok {
		t.Fatal("应当读全")
	}
	if len(rows) != 2 {
		t.Fatalf("应当两条 115 资源，得到 %d: %+v", len(rows), rows)
	}
	bySlug := map[string]re0Resource{}
	for _, r := range rows {
		bySlug[r.Slug] = r
	}
	r1, r2 := bySlug["s-1"], bySlug["s-2"]
	if r1.Title != "沙丘2 1080p" || r1.ShareSize != "8.5GB" || r1.UnlockPoints == nil || *r1.UnlockPoints != 5 {
		t.Errorf("s-1 解析不对: %+v", r1)
	}
	if r2.Title != "沙丘2 4K" || *r2.UnlockPoints != 0 || len(r2.VideoResolution) != 1 || r2.CreatedAt != "2026-10-01" {
		t.Errorf("s-2 解析不对（积分为空按免费）: %+v", r2)
	}

	// 引用还没流式输出到：算没读全，接着等
	if _, ok := parseRe0PageResources(strings.Split(raw, "\n")[0]); ok {
		t.Error("groupData 引用解析不出来时不该算读全")
	}
	// 页面上根本没有分组
	if _, ok := parseRe0PageResources(`0:["$","div",null,{}]`); ok {
		t.Error("没有分组不该算读全")
	}
}

func TestParseRe0JSONResources(t *testing.T) {
	rows := parseRe0JSONResources([]byte(`{"data":[{"href":"/resource/115/abc_1","title":"X"},{"slug":"bad slug!"},{"slug":"y","website":"baidu"}]}`))
	if len(rows) != 1 || rows[0].Slug != "abc_1" {
		t.Fatalf("得到 %+v", rows)
	}
}

func TestRe0Channel(t *testing.T) {
	if ch := re0Channel(&re0Cfg{ClientSecret: "s", AccessToken: "t", Username: "u", Password: "p"}); ch != "openapi" {
		t.Errorf("已授权 OpenAPI 应当优先，得到 %s", ch)
	}
	if ch := re0Channel(&re0Cfg{ClientSecret: "s", Username: "u", Password: "p"}); ch != "web" {
		t.Errorf("没授权时走网页，得到 %s", ch)
	}
	if ch := re0Channel(&re0Cfg{Username: "u"}); ch != "" {
		t.Errorf("没密码不算配置了网页登录，得到 %s", ch)
	}
}
