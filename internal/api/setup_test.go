package api

import (
	"strings"
	"testing"
)

func setupStepOf(t *testing.T, steps []setupStep, key string) setupStep {
	t.Helper()
	for _, s := range steps {
		if s.Key == key {
			return s
		}
	}
	t.Fatalf("清单里没有 %s", key)
	return setupStep{}
}

func TestPlanSetupFresh(t *testing.T) {
	steps := planSetup(setupInputs{LocalRoot: "/media"})
	// 前端按 key 配跳转，顺序就是上手顺序（工作目录的一键创建要求先选媒体库）
	want := []string{"pan115", "library", "workspace", "tmdb", "full", "emby", "organize"}
	if len(steps) != len(want) {
		t.Fatalf("步数 %d", len(steps))
	}
	for i, k := range want {
		if steps[i].Key != k {
			t.Fatalf("第 %d 步是 %s，应为 %s", i, steps[i].Key, k)
		}
		if steps[i].Done {
			t.Fatalf("全新部署 %s 不该算完成", k)
		}
	}
	if !setupStepOf(t, steps, "emby").Optional || !setupStepOf(t, steps, "organize").Optional {
		t.Fatal("Emby 与整理是可选的")
	}
	if w := setupStepOf(t, steps, "workspace"); !strings.Contains(w.Detail, "转存、已存在、冗余") {
		t.Fatalf("工作目录要说清缺哪几个：%s", w.Detail)
	}
}

func TestPlanSetupLibraryRoot(t *testing.T) {
	// 网盘根不能当媒体库
	if setupStepOf(t, planSetup(setupInputs{LibCid: "0"}), "library").Done {
		t.Fatal("cid=0 不算选好了媒体库")
	}
}

func TestPlanSetupEmby(t *testing.T) {
	cases := []struct {
		name string
		in   setupInputs
		done bool
		has  string
	}{
		{"没填", setupInputs{}, false, "服务器地址"},
		{"缺密钥", setupInputs{EmbyConfig: true}, false, "API 密钥"},
		{"连不上", setupInputs{EmbyConfig: true, EmbyKey: true, EmbyErr: "连不上 Emby：timeout"}, false, "timeout"},
		{"没有库", setupInputs{EmbyConfig: true, EmbyKey: true}, false, "添加一个媒体库"},
		{"对不上带推算", setupInputs{EmbyConfig: true, EmbyKey: true, EmbyLibs: 2, EmbySugg: "/strm", LocalRoot: "/media"}, false, "/strm"},
		{"对不上", setupInputs{EmbyConfig: true, EmbyKey: true, EmbyLibs: 2, LocalRoot: "/media"}, false, "路径映射"},
		{"覆盖", setupInputs{EmbyConfig: true, EmbyKey: true, EmbyLibs: 2, EmbyCover: 1}, true, "对得上"},
	}
	for _, c := range cases {
		s := setupStepOf(t, planSetup(c.in), "emby")
		if s.Done != c.done || !strings.Contains(s.Detail, c.has) {
			t.Errorf("%s：done=%v detail=%q", c.name, s.Done, s.Detail)
		}
	}
}

func TestPlanSetupAllDone(t *testing.T) {
	steps := planSetup(setupInputs{
		Pan115: true, Username: "u", LibCid: "1", Share: "2", Existing: "3", Redundant: "4",
		TmdbKey: true, StrmCount: 1, EmbyConfig: true, EmbyKey: true, EmbyLibs: 1, EmbyCover: 1, Organized: 1,
	})
	for _, s := range steps {
		if !s.Done {
			t.Errorf("%s 应已完成：%s", s.Key, s.Detail)
		}
	}
	if d := setupStepOf(t, steps, "pan115").Detail; !strings.Contains(d, "u") {
		t.Errorf("登录后带上用户名：%s", d)
	}
}
