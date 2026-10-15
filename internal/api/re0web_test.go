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
	// 没资源的片：网盘一栏是空状态组件（2026-10-14 现场页面原样）
	rows, ok = parseRe0PageResources(strings.Join([]string{
		`0:["$","div",null,{"children":"$2c"}]`,
		`2c:["$","$L54",null,{"kind":"empty","error":{"message":"暂无资源","description":"若你持有对应资源，欢迎积极提交共享。"}}]`,
	}, "\n"))
	if !ok || len(rows) != 0 {
		t.Errorf("暂无资源应当算读全、零条，得到 ok=%v %d 条", ok, len(rows))
	}
	// 别的栏目的空状态不算
	if _, ok := parseRe0PageResources(`0:["$","$L54",null,{"kind":"empty","error":{"message":"暂无片单"}}]`); ok {
		t.Error("片单的空状态不该当成没资源")
	}
}

// ED2K 资源站方也放在「115网盘」组里，行上 website 是 "ed2k"：要留下（解锁后走 115 离线），
// 照插件那样剔掉的话只有 ED2K 资源的片显示「没有 115 资源」（2026-10-15 现场，tv/332261）
func TestParseRe0PageResourcesEd2k(t *testing.T) {
	raw := `0:["$","$L56",null,{"groupData":{"115":[{"slug":"e-1","website":"ed2k","remark":"S01E01 1080P Netflix WEB-DL AAC2.0 内封简繁字幕","share_size":"1.59GB","is_unlocked":true}],"quark":[{"slug":"q-1","website":"quark"}]}}]`
	rows, ok := parseRe0PageResources(raw)
	if !ok || len(rows) != 1 {
		t.Fatalf("ED2K 资源应当留下，得到 ok=%v %+v", ok, rows)
	}
	if r := rows[0]; r.Slug != "e-1" || r.PanType != "ed2k" || !r.IsUnlocked {
		t.Errorf("解析不对: %+v", r)
	}
	if !isRe0Ed2k("ed2k://|file|Lupin S01E01 1080p.mkv|1707000000|0123456789ABCDEF0123456789ABCDEF|/") || isRe0Ed2k("https://115cdn.com/s/abc") {
		t.Error("ed2k 链接判定不对")
	}
	// 订阅：RE0 的 ED2K 资源按离线算（过离线配额等闸门），不能当分享按集挑
	it := ResourceItem{Source: "re0", Kind: "ed2k", Action: "unlock"}
	if !it.offline() || (subCand{Item: it}).share() {
		t.Error("RE0 ED2K 资源应当按离线处理")
	}
	if sh := (ResourceItem{Source: "re0", Kind: "share115", Action: "unlock"}); sh.offline() || !(subCand{Item: sh}).share() {
		t.Error("RE0 115 分享仍按分享处理")
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

// 机器人：免费 / 解锁过的 RE0 能直接处理、自动择优也会挑；要花积分的不挑
func TestRe0BotFree(t *testing.T) {
	zero, five := 0, 5
	paid := ResourceItem{Source: "re0", Action: "unlock", Relevant: true, Points: &five}
	free := ResourceItem{Source: "re0", Action: "unlock", Relevant: true, Points: &zero}
	owned := ResourceItem{Source: "re0", Action: "unlock", Relevant: true, Points: &five, Owned: true}
	unknown := ResourceItem{Source: "re0", Action: "unlock", Relevant: true}
	if re0BotFree(paid) || !re0BotFree(free) || !re0BotFree(owned) || re0BotFree(unknown) {
		t.Fatal("免费 / 已解锁才算能直接解锁，积分不知道的不算")
	}
	f := &botFlow{items: []ResourceItem{paid, unknown, free}, sent: map[int]bool{}}
	if i := f.bestIndex(); i != 2 {
		t.Fatalf("自动择优应当跳过要花积分的，挑到 %d", i)
	}
}
