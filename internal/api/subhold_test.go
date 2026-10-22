package api

import (
	"testing"
	"time"

	"115-station/internal/model"
)

// 订阅这一轮建的包装目录：整理与守望者都先跳过，放行后照常
func TestSubWrapperHold(t *testing.T) {
	newTestDB(t, "subhold.db")
	r := &subRunner{h: &Handler{DB: model.DB}, target: "stage", holdWrap: true,
		mkdir: func(parent, name string) (string, error) { return "wrap1", nil }}
	sub := &model.Subscription{Title: "凡人修仙传", Year: "2020"}
	if _, cid, err := r.makeWrapper(sub, nil); err != nil || cid != "wrap1" {
		t.Fatalf("makeWrapper: %s %v", cid, err)
	}
	defer releaseSubWrappers("wrap1")
	if !subWrapperHeld("wrap1") || len(r.held) != 1 {
		t.Fatal("建出来的包装目录要压着")
	}

	entries := []map[string]interface{}{
		{"f": "0", "cid": "wrap1", "n": "凡人修仙传 (2020) S01 ·订阅6-14"},
		{"f": "1", "fid": "f1", "n": "a.mkv"},
	}
	if n := countUnheld(entries); n != 1 {
		t.Fatalf("守望者不该数压着的包装目录: %d", n)
	}
	ctx := &orgCtx{cfg: &OrgConfig{Pending: "stage"}, sink: &orgSink{}, handled: map[string]bool{}}
	es, _ := stagingWork(ctx, []stagingEntry{
		{dirEntry: dirEntry{Fid: "wrap1", Cid: "wrap1", IsDir: true}, Pending: "stage"},
		{dirEntry: dirEntry{Fid: "f1"}, Pending: "stage"},
	})
	if len(es) != 1 || es[0].Fid != "f1" {
		t.Fatalf("整理要跳过压着的包装目录: %+v", es)
	}

	// 提交失败收掉的包装目录同样放行
	r.dropWrapper("wrap1", "x")
	if subWrapperHeld("wrap1") {
		t.Fatal("dropWrapper 之后不该再压着")
	}

	// 不开 holdWrap（测试里直接跑 run 的）不压
	r2 := &subRunner{h: r.h, target: "stage", mkdir: func(string, string) (string, error) { return "wrap2", nil }}
	r2.makeWrapper(sub, nil)
	if subWrapperHeld("wrap2") {
		t.Fatal("没开 holdWrap 不该压")
	}
}

// 订阅的离线：30 秒内秒传到了照常在路上；没到就删任务、收掉包装目录、记一笔不重试的失败
func TestSubOfflineWaitOrAbandon(t *testing.T) {
	newTestDB(t, "suboffline.db")
	for _, tc := range []struct {
		name    string
		landAt  int // 第几次看包装目录时东西到了，0 = 一直没到
		want    string
		dropped bool
	}{
		{"秒传到了", 2, subAttemptInflight, false},
		{"等不到", 0, subAttemptFailed, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var removed, deleted []string
			looks, naps := 0, 0
			r := &subRunner{h: &Handler{DB: model.DB}, cfg: testSubCfg(), target: "T", now: time.Now, holdWrap: true,
				mkdir:    func(string, string) (string, error) { return "W", nil },
				rmdir:    func(cid string) error { removed = append(removed, cid); return nil },
				offline:  func(string, string) (int, string, uint, string) { return 200, "ok", 7, "abc" },
				landed:   func(string) bool { looks++; return tc.landAt > 0 && looks >= tc.landAt },
				dropTask: func(h string) error { deleted = append(deleted, h); return nil },
				sleep:    func(time.Duration) bool { naps++; return true },
			}
			defer releaseSubWrappers("W")
			sub := &model.Subscription{ID: 1, MediaType: "movie", Title: "片"}
			att := model.SubAttempt{SubID: 1, Status: subAttemptFailed}
			fail := func(reason string, retry bool) (model.SubAttempt, []epKey) {
				if retry {
					t.Error("弃用的不该重试")
				}
				att.Status, att.Reason = subAttemptFailed, reason
				return att, nil
			}
			got, _ := r.tryOffline(sub, subCand{}, resLink{URL: "magnet:?xt=urn:btih:1"}, nil, &att, fail)
			if got.Status != tc.want || got.LinkID != 7 {
				t.Fatalf("状态 %s（%s）link=%d，想要 %s", got.Status, got.Reason, got.LinkID, tc.want)
			}
			if tc.dropped {
				if len(deleted) != 1 || deleted[0] != "abc" || len(removed) != 1 || naps != 6 {
					t.Fatalf("等满 30 秒后删任务、删包装目录: 删任务 %v 删目录 %v 歇 %d 次", deleted, removed, naps)
				}
				if subWrapperHeld("W") {
					t.Fatal("弃用的包装目录不该再压着")
				}
			} else if len(deleted)+len(removed) != 0 || naps != 2 {
				t.Fatalf("到了就不删: %v %v，歇 %d 次", deleted, removed, naps)
			}
		})
	}
}
