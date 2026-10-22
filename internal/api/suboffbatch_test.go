package api

import (
	"reflect"
	"strings"
	"testing"
	"time"

	"115-station/internal/model"
)

// 解锁出一批 ed2k：只提交缺的集，一起进同一个包装目录；秒传到了的留下、没到的删任务；按任务数记账
func TestSubOfflineBatch(t *testing.T) {
	if _, err := model.InitDB("file:suboffbatch_test?mode=memory&cache=shared"); err != nil {
		t.Fatalf("init db: %v", err)
	}
	defer func() { model.DB = nil }()
	ed2k := func(name string) string {
		return "ed2k://|file|" + name + "|1000|" + strings.Repeat("A", 31) + string(rune('0'+len(name)%10)) + "|/"
	}
	links := []string{ed2k("剧 (2026) S01E01.1080p.mkv"), ed2k("剧 (2026) S01E02.1080p.mkv"), ed2k("剧 (2026) S01E03.1080p.mkv")}
	sub := &model.Subscription{ID: 1, Title: "剧", MediaType: "tv"}
	missing := map[epKey]bool{{S: 1, E: 1}: true, {S: 1, E: 2}: true} // E03 库里已有

	var submitted, dropped []string
	var nextID uint
	r := &subRunner{h: &Handler{DB: model.DB}, cfg: testSubCfg(), target: "T", now: time.Now,
		mkdir: func(string, string) (string, error) { return "W", nil },
		rmdir: func(string) error { return nil },
		offline: func(link, target string) (int, string, uint, string) {
			if target != "W" {
				t.Errorf("该提交进包装目录，实际 %s", target)
			}
			submitted = append(submitted, link)
			nextID++
			return 200, "ok", nextID, "h" + downloadLinkName(link)
		},
		// 只有 E01 秒传到了
		landed:   func(string) []string { return []string{"剧 (2026) S01E01.1080p.mkv"} },
		dropTask: func(h string) error { dropped = append(dropped, h); return nil },
		sleep:    func(time.Duration) bool { return true },
	}
	att := model.SubAttempt{SubID: 1, Status: subAttemptFailed}
	fail := func(reason string, retry bool) (model.SubAttempt, []epKey) {
		att.Status, att.Reason = subAttemptFailed, reason
		return att, nil
	}
	got, covered := r.tryOfflineBatch(sub, subCand{}, resLink{Action: "offline", URL: links[0], More: links[1:]}, missing, 50, &att, fail)
	if got.Status != subAttemptInflight {
		t.Fatalf("状态 %s（%s）", got.Status, got.Reason)
	}
	if !reflect.DeepEqual(submitted, links[:2]) {
		t.Fatalf("只该提交缺的 E01 E02，实际 %v", submitted)
	}
	if !reflect.DeepEqual(dropped, []string{"h剧 (2026) S01E02.1080p.mkv"}) {
		t.Fatalf("没秒传的 E02 该删任务，实际 %v", dropped)
	}
	if !reflect.DeepEqual(covered, []epKey{{S: 1, E: 1}}) {
		t.Fatalf("补上的只算到了的 E01，实际 %v", covered)
	}
	if got.Tasks != 2 || got.LinkID != 1 || !reflect.DeepEqual(subAttemptLinkIDs(&got), []uint{1, 2}) {
		t.Fatalf("记账 tasks=%d link=%d ids=%v", got.Tasks, got.LinkID, subAttemptLinkIDs(&got))
	}
	if n := subOfflineUsedThisMonth(model.DB, time.Now()); n != 2 {
		t.Fatalf("每月上限按任务数算，应 2，实际 %d", n)
	}

	// 本月只剩 1 个：不提交，交给用户
	submitted = nil
	r2 := &subRunner{h: r.h, cfg: testSubCfg(), target: "T", now: time.Now, offline: r.offline, mkdir: r.mkdir}
	r2.offlineChecked, r2.offlineLeft, r2.quotaLeft = true, 1, -1
	att = model.SubAttempt{SubID: 1}
	got, _ = r2.tryOfflineBatch(sub, subCand{}, resLink{Action: "offline", URL: links[0], More: links[1:]}, missing, 50, &att, fail)
	if got.Status != subAttemptFailed || len(submitted) != 0 || !strings.Contains(got.Reason, "本月只剩 1 个") {
		t.Fatalf("配额不够不该提交: %s %v", got.Reason, submitted)
	}

	// 有认不出文件名的链接（磁力）：挑不了集，交给用户
	att = model.SubAttempt{SubID: 1}
	got, _ = r.tryOfflineBatch(sub, subCand{}, resLink{Action: "offline", URL: links[0], More: []string{"magnet:?xt=urn:btih:abc"}}, missing, 50, &att, fail)
	if got.Status != subAttemptFailed || !strings.Contains(got.Reason, "挑不了集") {
		t.Fatalf("磁力挑不了集: %s", got.Reason)
	}
}

func TestLandedOf(t *testing.T) {
	want := []string{"a.mkv", "b.mkv"}
	if got := landedOf(want, []string{"a.mkv"}); !reflect.DeepEqual(got, []string{"a.mkv"}) {
		t.Fatal(got)
	}
	// 条目数够了、名字被 115 改过：按全到了算
	if got := landedOf(want, []string{"a.mkv", "b_.mkv"}); !reflect.DeepEqual(got, want) {
		t.Fatal(got)
	}
	if got := landedOf(want, nil); got != nil {
		t.Fatal(got)
	}
}
