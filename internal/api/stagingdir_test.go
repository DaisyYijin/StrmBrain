package api

import (
	"encoding/json"
	"testing"
)

func jsonMap(t *testing.T, raw string) map[string]any {
	t.Helper()
	m := map[string]any{}
	if err := json.Unmarshal([]byte(raw), &m); err != nil {
		t.Fatalf("不是合法 JSON: %q: %v", raw, err)
	}
	return m
}

// 只配了待整理的老用户：原待整理目录改作转存/待整理，org-basic 去掉 pending，别的字段原样
func TestMergeStaging_OnlyPending(t *testing.T) {
	r := mergeStagingSettings("", `{"pending":"3","pending_path":"/S/待整理","existing":"4","manual_confirm":true}`)
	if r.Legacy != nil {
		t.Fatalf("没有被弃用的目录: %+v", r.Legacy)
	}
	sh := jsonMap(t, r.Share)
	if sh["folder"] != "3" || sh["folder_path"] != "/S/待整理" {
		t.Fatalf("share=%v", sh)
	}
	org := jsonMap(t, r.Org)
	if _, ok := org["pending"]; ok {
		t.Fatalf("pending 没去掉: %v", org)
	}
	if _, ok := org["pending_path"]; ok {
		t.Fatalf("pending_path 没去掉: %v", org)
	}
	if org["existing"] != "4" || org["manual_confirm"] != true {
		t.Fatalf("其余字段要原样保留: %v", org)
	}
}

// 两个都配了且不同：以转存为准，旧待整理记下来提示用户，不改 share
func TestMergeStaging_BothDiffer(t *testing.T) {
	r := mergeStagingSettings(`{"folder":"2","folder_path":"/S/转存"}`, `{"pending":"3","pending_path":"/S/待整理"}`)
	if r.Share != "" {
		t.Fatalf("转存目录不该被改: %s", r.Share)
	}
	if r.Legacy == nil || r.Legacy.Cid != "3" || r.Legacy.Path != "/S/待整理" {
		t.Fatalf("legacy=%+v", r.Legacy)
	}
	if _, ok := jsonMap(t, r.Org)["pending"]; ok {
		t.Fatal("pending 没去掉")
	}
}

// 两个本来就是同一个目录：只去掉 pending，不提示
func TestMergeStaging_Same(t *testing.T) {
	r := mergeStagingSettings(`{"folder":"2"}`, `{"pending":"2"}`)
	if r.Share != "" || r.Legacy != nil || r.Org == "" {
		t.Fatalf("r=%+v", r)
	}
}

// 已经迁移过（org-basic 没有 pending）：什么都不做，每次启动都跑也安全
func TestMergeStaging_Idempotent(t *testing.T) {
	r := mergeStagingSettings(`{"folder":"2"}`, `{"existing":"4"}`)
	if r.Share != "" || r.Org != "" || r.Legacy != nil {
		t.Fatalf("r=%+v", r)
	}
	// 空 pending 字段（老前端存过空值）也要剥掉，但不产生别的变化
	r = mergeStagingSettings(`{"folder":"2"}`, `{"pending":"","existing":"4"}`)
	if r.Share != "" || r.Legacy != nil || r.Org == "" {
		t.Fatalf("r=%+v", r)
	}
	if r2 := mergeStagingSettings(`{"folder":"2"}`, r.Org); r2.Org != "" {
		t.Fatalf("第二次不该再改: %+v", r2)
	}
}

// 老前端整存 org-basic 带回来的 pending 要剥掉；解析不了的原样放过
func TestStripOrgPending(t *testing.T) {
	out, ok := stripOrgPending(`{"pending":"9","pending_path":"/x","redundant":"5"}`)
	if !ok {
		t.Fatal("应当剥掉")
	}
	m := jsonMap(t, out)
	if _, has := m["pending"]; has || m["redundant"] != "5" {
		t.Fatalf("out=%v", m)
	}
	if out, ok := stripOrgPending(`{"redundant":"5"}`); ok || out != `{"redundant":"5"}` {
		t.Fatalf("没有 pending 时不改: %q %v", out, ok)
	}
	if out, ok := stripOrgPending(`not json`); ok || out != `not json` {
		t.Fatalf("解析不了的原样返回: %q %v", out, ok)
	}
}
