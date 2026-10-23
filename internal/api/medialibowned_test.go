package api

import (
	"testing"
	"time"

	"115-station/internal/model"
)

// 「已入库」要以 STRM 台账为准：MediaLibrary 里留着的旧行（片早就不在库里）不能标上
func TestMediaLibOwnedUsesLedger(t *testing.T) {
	newTestDB(t, "medialib_owned.db")
	db := model.DB
	gone := time.Now()
	for _, f := range []model.SyncedFile{
		{FileID: "1", RelPath: "媒体库/电影/欧美电影/超凡蜘蛛侠 (2012)/超凡蜘蛛侠 (2012).strm", Kind: "video"},
		// 失效 STRM 不算
		{FileID: "2", RelPath: "媒体库/电影/欧美电影/蜘蛛侠3 (2007)/蜘蛛侠3 (2007).strm", Kind: "video", OrphanAt: &gone},
		// 洗版换了文件名，标题目录还在
		{FileID: "3", RelPath: "媒体库/电影/欧美电影/阿凡达 (2009)/阿凡达 (2009) - 2160p.strm", Kind: "video"},
		// 剧集删了一集，别的集还在
		{FileID: "4", RelPath: "媒体库/剧集/国产剧/繁花 (2023)/Season 01/繁花 S01E02.strm", Kind: "video"},
		// 平铺片目：分类目录里别的片
		{FileID: "5", RelPath: "媒体库/电影/华语电影/别的片.strm", Kind: "video"},
	} {
		db.Create(&f)
	}
	rows := []model.MediaLibrary{
		{TmdbID: 1930, Title: "超凡蜘蛛侠", MediaType: "movie", TargetPath: "电影/欧美电影/超凡蜘蛛侠 (2012)/超凡蜘蛛侠 (2012).mkv"},
		{TmdbID: 557, Title: "蜘蛛侠", MediaType: "movie", TargetPath: "电影/欧美电影/蜘蛛侠 (2002)/蜘蛛侠 (2002).mkv"},
		{TmdbID: 559, Title: "蜘蛛侠3", MediaType: "movie", TargetPath: "电影/欧美电影/蜘蛛侠3 (2007)/蜘蛛侠3 (2007).mkv"},
		{TmdbID: 19995, Title: "阿凡达", MediaType: "movie", TargetPath: "电影/欧美电影/阿凡达 (2009)/阿凡达 (2009) - 1080p.mkv"},
		{TmdbID: 2, Title: "繁花", MediaType: "tv", TargetPath: "剧集/国产剧/繁花 (2023)/Season 01/繁花 S01E01.mkv"},
		{TmdbID: 3, Title: "平铺", MediaType: "movie", TargetPath: "电影/华语电影/平铺.mkv"},
		{TmdbID: 4, Title: "没落点", MediaType: "movie"},
	}
	got := map[int]bool{}
	for _, r := range mediaLibOwned(db, rows) {
		got[r.TmdbID] = true
	}
	want := map[int]bool{1930: true, 19995: true, 2: true}
	for _, r := range rows {
		if got[r.TmdbID] != want[r.TmdbID] {
			t.Errorf("%s: 已入库 = %v，期望 %v", r.Title, got[r.TmdbID], want[r.TmdbID])
		}
	}
}
