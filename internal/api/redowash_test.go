package api

import (
	"testing"

	"115-station/internal/model"
)

// 重新整理也要洗版：「已存在」里的 REMUX 按新策略重新整理进库时，同集的 WEB-DL 让位；
// 记录自己的文件不算库内版本（原地刷新不能判给自己让位），平手的不动
func TestPlanRedoWash(t *testing.T) {
	st := &washStrategy{Mode: "replace", PriorityLevel: []washRule{
		{ResourcePix: "2160p,1080p", ResourceType: "REMUX", ResourceEffect: "DV.HDR,DV,HDR10+,HDR"},
		{ResourcePix: "2160p,1080p", ResourceType: "REMUX"},
		{ResourcePix: "2160p,1080p", ResourceType: "WEB-DL"},
	}}
	media := &TmdbMedia{Title: "间谍过家家", MediaType: "tv"}
	rel := "动漫番剧/间谍过家家.2022.{tmdbid=120089}/Season 3"
	groups := map[string][]orgRecordFile{rel: {
		{Fid: "n7", Kind: "video", Name: "间谍过家家.SPY×FAMILY.S03E07.1080p.BluRay.H264.REMUX.LPCM.2.0.mkv"},
		{Fid: "n8", Kind: "video", Name: "间谍过家家.SPY×FAMILY.S03E08.1080p.BluRay.H264.REMUX.LPCM.2.0.mkv"},
	}}
	ledger := []model.SyncedFile{
		{FileID: "o7", Kind: "video", RelPath: "影视/" + rel + "/间谍过家家.SPY×FAMILY.2022.S03E07.WEB-DL.1080p.H264.strm"},
		{FileID: "o8", Kind: "video", RelPath: "影视/" + rel + "/间谍过家家.SPY×FAMILY.2022.S03E08.Bluray.REMUX.1080p.strm"},
		{FileID: "n7", Kind: "video", RelPath: "影视/" + rel + "/间谍过家家.SPY×FAMILY.S03E07.1080p.BluRay.H264.REMUX.LPCM.2.0.strm"},
	}
	own := map[string]bool{"n7": true, "n8": true}
	plans := planRedoWash(media, st, groups, own, func(string) []model.SyncedFile { return ledger }, func(string) {})
	if len(plans) != 1 {
		t.Fatalf("应只有 E07 让位，得到 %d 个", len(plans))
	}
	if v := plans[0].victims; len(v) != 1 || v[0].FileID != "o7" {
		t.Fatalf("让位的应是 E07 的 WEB-DL，得到 %+v", v)
	}
}
