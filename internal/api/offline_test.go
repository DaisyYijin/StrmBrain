package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// 115 离线任务接口响应形态多变，解析器需全部兼容
func TestExtractTaskItems(t *testing.T) {
	cases := []string{
		`[{"name":"a.mkv","percent":45.5}]`,                                            // 顶层数组
		`{"state":true,"data":[{"name":"a.mkv"}]}`,                                      // data 数组
		`{"state":true,"info":[{"name":"a.mkv"}]}`,                                      // info 数组
		`{"state":true,"data":{"tasks":[{"name":"a.mkv"}],"count":1}}`,                  // data.tasks
		`{"state":true,"info":{"list":[{"name":"a.mkv"}]}}`,                             // info.list
		`{"state":true,"tasks":[{"name":"a.mkv"}]}`,                                     // 顶层 tasks
		`{"state":true,"data":{"info":[{"name":"a.mkv"}]}}`,                             // data.info
	}
	for i, c := range cases {
		items, ok := extractTaskItems([]byte(c))
		if !ok || len(items) != 1 || items[0]["name"] != "a.mkv" {
			t.Errorf("case %d failed: ok=%v items=%v", i, ok, items)
		}
	}
	if _, ok := extractTaskItems([]byte(`{"state":true}`)); ok {
		t.Error("empty response should not parse")
	}
	_ = json.Marshal
}

func TestExtractTaskItemsEmpty(t *testing.T) {
	cases := []string{
		`{"page":1,"page_count":0,"page_row":30,"page_size":30,"count":0,"quota":4995,"total":5000,"tasks":null,"state":true,"real_state":false,"errtype":"suc"}`,
		`[]`,
		`{"state":true,"tasks":[]}`,
		`{"state":true,"data":[]}`,
		`{"state":true,"info":{"list":[]}}`,
		`{"state":true,"data":{"tasks":null,"count":0}}`,
	}
	for _, body := range cases {
		items, ok := extractTaskItems([]byte(body))
		if !ok || items == nil || len(items) != 0 {
			t.Errorf("extractTaskItems(%s) = %v, %v; want empty array", body, items, ok)
		}
	}
}

func TestExtractTaskItemsInvalid(t *testing.T) {
	for _, body := range []string{
		`null`, `{"state":true}`, `{"count":0}`, `{"tasks":null}`,
		`{"tasks":null,"count":1}`, `{"tasks":{},"count":0}`,
		`{"tasks":"invalid","count":0}`, `{"tasks":`,
	} {
		if items, ok := extractTaskItems([]byte(body)); ok {
			t.Errorf("extractTaskItems(%s) unexpectedly accepted: %v", body, items)
		}
	}
}

func TestClassifyLinkShareDomains(t *testing.T) {
	cases := []struct {
		link string
		want string
	}{
		// 115cdn.com/s/ 曾被误判为 http 交给离线下载（115 假成功建空壳目录）
		{"https://115cdn.com/s/swsvlam3nqo?password=9527#", "share"},
		{"https://115.com/s/abc123?password=xxxx", "share"},
		{"https://anxia.com/s/abcd1234", "share"},
		{"magnet:?xt=urn:btih:ABC", "magnet"},
		{"https://example.com/file.mp4", "http"},
	}
	for _, c := range cases {
		if got := classifyLink(c.link); got != c.want {
			t.Errorf("classifyLink(%q) = %q, want %q", c.link, got, c.want)
		}
	}
}

func TestIs115BusyResp(t *testing.T) {
	busy := `{"state":false,"error":"服务器开小差了，稍后再试吧","errNo":0}`
	ok := `{"state":false,"error":"分享不存在","errNo":0}`
	if !is115BusyResp([]byte(busy)) {
		t.Errorf("开小差响应应识别为风控")
	}
	if is115BusyResp([]byte(ok)) {
		t.Errorf("业务错误不应误判为风控")
	}
	// 115 把中文转义成 \uXXXX：原文里找不到「频繁」，要解码后认（2026-10-14 现场）
	escaped := `{"state":false,"error":"操作过于频繁，请稍后再试。","errno":190044}`
	if !is115BusyResp([]byte(escaped)) {
		t.Errorf("转义的限频响应应识别为风控")
	}
	if got := share115ErrText([]byte(escaped)); got != "操作过于频繁，请稍后再试。" {
		t.Errorf("错误文案 = %q", got)
	}
	if !is115BusyResp([]byte(`{"state":false,"error":"","errNo":"190044"}`)) {
		t.Errorf("只有错误码 190044 也应识别为风控")
	}
	if is115BusyResp([]byte(`{"state":true}`)) {
		t.Errorf("成功响应不应误判")
	}
}

func TestShareReceiveBusyRetry(t *testing.T) {
	calls, alwaysBusy := 0, false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls == 1 || alwaysBusy {
			w.Write([]byte(`{"state":false,"error":"操作过于频繁，请稍后再试。","errno":190044}`))
			return
		}
		w.Write([]byte(`{"state":true}`))
	}))
	defer srv.Close()
	orig, origWait := shareAPIOrigins, shareReceiveBusyWait
	shareAPIOrigins, shareReceiveBusyWait = []string{srv.URL}, time.Millisecond
	t.Cleanup(func() { shareAPIOrigins, shareReceiveBusyWait = orig, origWait })

	if err := shareReceive("sw1", "abcd", []string{"1"}, "9", "UID=1"); err != nil || calls != 2 {
		t.Fatalf("限频后应重试成功: err=%v calls=%d", err, calls)
	}

	// 一直限频：报 115 的中文原话，不是一串转义的 JSON
	alwaysBusy = true
	err := shareReceive("sw1", "abcd", []string{"1"}, "9", "UID=1")
	if err == nil || !strings.Contains(err.Error(), "操作过于频繁") || strings.Contains(err.Error(), `\u`) {
		t.Fatalf("错误文案不对: %v", err)
	}
}
