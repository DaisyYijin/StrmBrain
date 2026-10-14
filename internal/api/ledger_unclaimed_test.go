package api

import (
	"fmt"
	"testing"

	"115-station/internal/model"
)

// T17：台账里认不出片目的视频按「库名 + 库内前两段」聚合（LIBRARY-LAYOUT-PLAN.md §3.6）
func TestScanLedgerUnclaimed(t *testing.T) {
	ledgerTestDB(t, []model.CategoryRule{{MediaType: "movie", Name: "电影"}, {MediaType: "tv", Name: "剧集"}}, []model.SyncedFile{
		{FileID: "1", Kind: "video", RelPath: "影视/电影/阿凡达 (2009)/阿凡达.strm"},
		{FileID: "2", Kind: "video", RelPath: "影视/LGNB电影/香港/无间道 (2002)/无间道.strm"},
		{FileID: "3", Kind: "video", RelPath: "影视/LGNB电影/香港/英雄本色 (1986)/英雄本色.strm"},
		{FileID: "4", Kind: "video", RelPath: "影视/LGNB电影/台湾/悲情城市 (1989)/悲情城市.strm"},
		{FileID: "5", Kind: "video", RelPath: "影视/剧集/狂飙.S01E01.strm"}, // 剧集分类里平铺的集不算片目
		{FileID: "6", Kind: "asset", RelPath: "影视/LGNB电影/香港/无间道 (2002)/poster.jpg"},
		{FileID: "7", Kind: "video", RelPath: "孤零零.strm"},
	})
	titles, u := scanLedger()
	if len(titles) != 1 {
		t.Fatalf("应只认出 1 部，实得 %d", len(titles))
	}
	want := []ledgerUnclaimedDir{
		{Path: "影视/LGNB电影/香港", Videos: 2},
		{Path: "", Videos: 1},
		{Path: "影视/LGNB电影/台湾", Videos: 1},
		{Path: "影视/剧集", Videos: 1},
	}
	if u.Videos != 5 || u.MoreDirs != 0 || len(u.Dirs) != len(want) {
		t.Fatalf("unclaimed = %+v", u)
	}
	for i := range want {
		if u.Dirs[i] != want[i] {
			t.Errorf("Dirs[%d] = %+v，预期 %+v", i, u.Dirs[i], want[i])
		}
	}
}

func TestSummarizeUnclaimedCap(t *testing.T) {
	by := map[string]int{}
	for i := 0; i < ledgerUnclaimedDirMax+3; i++ {
		by[fmt.Sprintf("库/目录%02d", i)] = i + 1
	}
	u := summarizeUnclaimed(by)
	if len(u.Dirs) != ledgerUnclaimedDirMax || u.MoreDirs != 3 || u.Dirs[0].Videos != ledgerUnclaimedDirMax+3 {
		t.Errorf("前 %d 个、视频多的在前: %+v", ledgerUnclaimedDirMax, u)
	}
	if empty := summarizeUnclaimed(nil); empty.Videos != 0 || empty.Dirs == nil {
		t.Errorf("没有认不出的视频时 Dirs 给空数组（前端不判 null）: %+v", empty)
	}
}
