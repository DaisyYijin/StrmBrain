package api

import (
	"testing"
	"time"

	"115-station/internal/model"
)

// 已播 = 最近播出那一集之前各季的集数 + 它的集号；第 0 季不算
func TestParseTVProgress(t *testing.T) {
	body := []byte(`{"status":"Returning Series",
		"last_episode_to_air":{"season_number":2,"episode_number":5},
		"seasons":[{"season_number":0,"episode_count":3},{"season_number":1,"episode_count":12},{"season_number":2,"episode_count":10}]}`)
	st, aired, total, err := parseTVProgress(body)
	if err != nil || st != "Returning Series" || aired != 17 || total != 22 {
		t.Fatalf("got %q %d %d %v", st, aired, total, err)
	}
	// 还没开播：没有 last_episode_to_air
	_, aired, total, _ = parseTVProgress([]byte(`{"status":"Planned","seasons":[{"season_number":1,"episode_count":8}]}`))
	if aired != 0 || total != 8 {
		t.Fatalf("未开播: %d %d", aired, total)
	}
	// 季列表落后于最近播出：total 至少等于已播
	_, aired, total, _ = parseTVProgress([]byte(`{"last_episode_to_air":{"season_number":1,"episode_number":9},"seasons":[{"season_number":1,"episode_count":8}]}`))
	if aired != 9 || total != 9 {
		t.Fatalf("季列表落后: %d %d", aired, total)
	}
}

func TestTVProgStale(t *testing.T) {
	now := time.Now()
	if tvProgStale(model.TmdbTVProgress{Status: "Returning Series", FetchedAt: now.Add(-2 * time.Hour)}, now) {
		t.Fatal("连载中两小时内不该重拉")
	}
	if !tvProgStale(model.TmdbTVProgress{Status: "Returning Series", FetchedAt: now.Add(-25 * time.Hour)}, now) {
		t.Fatal("连载中过了一天要重拉")
	}
	if tvProgStale(model.TmdbTVProgress{Status: "Ended", FetchedAt: now.Add(-10 * 24 * time.Hour)}, now) {
		t.Fatal("完结的十天内不该重拉")
	}
	if tvProgStale(model.TmdbTVProgress{Status: tvProgStatusGone, FetchedAt: now.Add(-10 * 24 * time.Hour)}, now) {
		t.Fatal("TMDB 上没有的按完结的期限")
	}
}

// 同一集几份算一集，季目录给季号，第 0 季不算
func TestCountHaveEpisodes(t *testing.T) {
	now := time.Now()
	key := "电视剧/国产剧/繁花 (2023) {tmdb-207468}"
	rows := []model.SyncedFile{
		{RelPath: key + "/Season 01/繁花 S01E01.strm"},
		{RelPath: key + "/Season 01/繁花 S01E01 4K.strm"},
		{RelPath: key + "/Season 01/繁花 S01E02-E03.strm"},
		{RelPath: key + "/Season 02/E05.strm"},
		{RelPath: key + "/Season 00/繁花 S00E01.strm"},
		{RelPath: key + "/Season 02/E06.strm", OrphanAt: &now},
	}
	if n := countHaveEpisodes(key, rows); n != 4 {
		t.Fatalf("应为 4 集（S01E01-03、S02E05；失效的 E06 不算），得 %d", n)
	}
}
