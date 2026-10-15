package api

import (
	"encoding/json"
	"testing"
)

// 改名修复：只换写着旧名的 library（带不带斜杠都认），别的库、别的条件不动
func TestRenameFixYAML(t *testing.T) {
	src := "movie:\n  电影/动画电影:\n    genre_ids: '16'\n    library: 动画\ntv:\n  电视剧/国漫:\n    library: '/动画/'\n  电视剧/日番:\n    library: 4K\n"
	out, changed, err := renameFixYAML(src, "动画", "动漫")
	if err != nil || !changed {
		t.Fatalf("changed=%v err=%v", changed, err)
	}
	rows := parseOrFail(t, out)
	for _, k := range [][2]string{{"movie", "电影/动画电影"}, {"tv", "电视剧/国漫"}} {
		if lib, _ := libOfRule(rows, k[0], k[1]); lib != "动漫" {
			t.Errorf("%s 应改成新名，得到 %q", k[1], lib)
		}
	}
	if lib, _ := libOfRule(rows, "tv", "电视剧/日番"); lib != "4K" {
		t.Errorf("别的库不该动，得到 %q", lib)
	}
	if _, changed, _ := renameFixYAML(out, "动画", "动漫"); changed {
		t.Error("修过一次再修应无改动")
	}
}

func TestFullWithFreshPath(t *testing.T) {
	raw := `{"cid":"100","cid_path":"/影视","mode":"fast","extra_libs":[{"cid":"200","path":"/动画"},{"cid":"300","path":"/4K"}]}`
	out, ok := fullWithFreshPath(raw, "200", "/动漫")
	if !ok {
		t.Fatal("附加库的显示路径应刷新")
	}
	cfg := parseFullCfg(out)
	if cfg.ExtraLibs[0].Path != "/动漫" || cfg.ExtraLibs[1].Path != "/4K" {
		t.Errorf("只改这个库：%+v", cfg.ExtraLibs)
	}
	var m map[string]any
	_ = json.Unmarshal([]byte(out), &m)
	if m["mode"] != "fast" || m["cid_path"] != "/影视" {
		t.Errorf("其他字段原样：%s", out)
	}
	if out2, ok := fullWithFreshPath(raw, "100", "/影视2"); !ok || jsonStrField(out2, "cid_path") != "/影视2" {
		t.Errorf("主库刷新 cid_path：%s", out2)
	}
	if _, ok := fullWithFreshPath(raw, "300", "/4K"); ok {
		t.Error("路径没变不算改动")
	}
}
