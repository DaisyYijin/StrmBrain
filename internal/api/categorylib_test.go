package api

import (
	"errors"
	"strings"
	"testing"

	"115-station/internal/model"
)

const catLibSrc = `# 分类规则
movie:
    电影/动画电影:   # 动画
        genre_ids: '16'
        library: "动漫"
    电影/外语电影:
        library: 动漫

tv:
    电视剧/日番:
        genre_ids: 16
        origin_country: !CN
        library: 动漫
    电视剧/国产剧:
        origin_country: 'CN,TW,HK'
    电影/动画电影:
        genre_ids: '16'
        library: 4K
`

func parseOrFail(t *testing.T, src string) []model.CategoryRule {
	t.Helper()
	rows, err := parseCategoryYAML(src)
	if err != nil {
		t.Fatalf("解析失败: %v\n%s", err, src)
	}
	return rows
}

func libOfRule(rows []model.CategoryRule, mediaType, name string) (string, bool) {
	for _, r := range rows {
		if r.MediaType == mediaType && r.Name == name {
			return r.Library, true
		}
	}
	return "", false
}

// 移除库：指向它的 library 删掉，其余原样；只剩 library 的兜底变回「名字:」
func TestRewriteCategoryLibraryRemove(t *testing.T) {
	out, changed, err := rewriteCategoryLibrary(catLibSrc, func(_, _, old string) (string, bool) {
		if old == "动漫" {
			return "", false
		}
		return old, true
	})
	if err != nil || !changed {
		t.Fatalf("changed=%v err=%v", changed, err)
	}
	rows := parseOrFail(t, out)
	for _, k := range [][2]string{{"movie", "电影/动画电影"}, {"movie", "电影/外语电影"}, {"tv", "电视剧/日番"}} {
		if lib, ok := libOfRule(rows, k[0], k[1]); !ok || lib != "" {
			t.Errorf("%s %s 的 library 应删掉，得到 %q（存在=%v）", k[0], k[1], lib, ok)
		}
	}
	// 剧集下同名的「电影/动画电影」指向 4K，不动
	if lib, _ := libOfRule(rows, "tv", "电影/动画电影"); lib != "4K" {
		t.Errorf("tv 段同名规则的 library 不该动，得到 %q", lib)
	}
	// 外语电影只有 library，删完仍是兜底
	for _, r := range rows {
		if r.MediaType == "movie" && r.Name == "电影/外语电影" && !r.IsDefault {
			t.Error("只剩 library 的规则删完应仍是兜底")
		}
	}
	if !strings.Contains(out, "# 动画") || !strings.Contains(out, "# 分类规则") {
		t.Errorf("注释应保留：\n%s", out)
	}
	if !strings.Contains(out, "    电影/外语电影:\n") {
		t.Errorf("删光 library 的规则应写回「名字:」、缩进保持 4：\n%s", out)
	}
	if !strings.Contains(out, "!CN") {
		t.Errorf("不加引号的排除写法应原样保留：\n%s", out)
	}
}

// 库改名：旧名换新名，只改指定类型那一条
func TestRewriteCategoryLibraryRename(t *testing.T) {
	out, changed, err := rewriteCategoryLibrary(catLibSrc, func(mt, name, old string) (string, bool) {
		if mt == "tv" && old == "4K" {
			return "超清", true
		}
		return old, true
	})
	if err != nil || !changed {
		t.Fatalf("changed=%v err=%v", changed, err)
	}
	rows := parseOrFail(t, out)
	if lib, _ := libOfRule(rows, "tv", "电影/动画电影"); lib != "超清" {
		t.Errorf("tv 段应改成新名，得到 %q", lib)
	}
	if lib, _ := libOfRule(rows, "movie", "电影/动画电影"); lib != "动漫" {
		t.Errorf("movie 段同名规则不该动，得到 %q", lib)
	}
}

// 没改动：原文一个字节都不变（不重新编码，免得打开看看就把空行和缩进改掉）
func TestRewriteCategoryLibraryNoop(t *testing.T) {
	out, changed, err := rewriteCategoryLibrary(catLibSrc, func(_, _, old string) (string, bool) { return old, true })
	if err != nil || changed || out != catLibSrc {
		t.Fatalf("没改动时应原样返回：changed=%v err=%v", changed, err)
	}
}

// 锚点 / 别名不碰
func TestRewriteCategoryLibraryAnchors(t *testing.T) {
	src := "movie:\n  电影/动画: &anime\n    genre_ids: '16'\n    library: 动漫\n  电影/卡通: *anime\n"
	_, _, err := rewriteCategoryLibrary(src, func(_, _, _ string) (string, bool) { return "", false })
	if !errors.Is(err, errCategoryYAMLExotic) {
		t.Fatalf("应拒绝带锚点的 YAML，得到 %v", err)
	}
}

func TestYamlIndentOf(t *testing.T) {
	for src, want := range map[string]int{
		"movie:\n  a:\n":          2,
		"# x\n\nmovie:\n    a:\n": 4,
		"movie: {}\n":             2,
	} {
		if got := yamlIndentOf(src); got != want {
			t.Errorf("%q → %d，应为 %d", src, got, want)
		}
	}
}

func TestSameCategoryRulesButLibrary(t *testing.T) {
	a := "movie:\n  电影/动画:\n    genre_ids: '16'\n    library: 动漫\n"
	if err := sameCategoryRulesButLibrary(a, "movie:\n  电影/动画:\n    genre_ids: '16'\n"); err != nil {
		t.Errorf("只差 library 应通过: %v", err)
	}
	if err := sameCategoryRulesButLibrary(a, "movie:\n  电影/动画:\n    genre_ids: '99'\n"); err == nil {
		t.Error("条件变了应报错")
	}
	if err := sameCategoryRulesButLibrary(a, "movie:\n  电影/动画:\n"); err == nil {
		t.Error("条件丢了应报错（规则从普通变兜底）")
	}
}

// 规则按库分组：写名字 / 写 cid / 没写（主库）/ 认不出
func TestRulesByLib(t *testing.T) {
	libs := []libRoot{{Cid: "100", Name: "影视"}, {Cid: "200", Name: "动漫"}, {Cid: "300", Name: "4K"}}
	rows := []model.CategoryRule{
		{MediaType: "movie", Name: "电影/动画电影", Library: "动漫"},
		{MediaType: "movie", Name: "电影/外语电影"},
		{MediaType: "tv", Name: "电视剧/日番", Library: "200"},
		{MediaType: "tv", Name: "电视剧/国漫", Library: "动画"},
		{MediaType: "tv", Name: "电视剧/4K剧", Library: "/4K/"},
	}
	by, orphans := rulesByLib(rows, libs)
	if len(by["100"]) != 1 || by["100"][0].Name != "电影/外语电影" {
		t.Errorf("主库：%+v", by["100"])
	}
	if len(by["200"]) != 2 {
		t.Errorf("动漫库应有 2 条：%+v", by["200"])
	}
	if len(by["300"]) != 1 {
		t.Errorf("4K 库应有 1 条（库名首尾斜杠同 pickRuleLib 去掉）：%+v", by["300"])
	}
	if len(orphans) != 1 || orphans[0].Library != "动画" {
		t.Errorf("认不出的库：%+v", orphans)
	}
}
