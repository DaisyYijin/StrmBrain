package api

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm/clause"

	"115-station/internal/model"
)

// 海报墙剧集卡片的集数进度（2026-10-20，参考 LitePan 卡片上的「1170/1181 集」与完结 / 追更）。
//
// 「有几集」按台账认季集（ledgerVideoEpisodes，与订阅盘点同口径），零 115 请求；
// 「播了几集」读 TMDB /tv/{id}，存 TmdbTVProgress 表。接口只回已有的，缺的 / 过期的交给后台
// 逐个去拉（一次一个、间隔 tvProgGap），前端看到 pending 过一会儿再来要 —— 几百部剧不能在请求里现查。

const (
	tvProgGap        = 300 * time.Millisecond
	tvProgTTLAiring  = 24 * time.Hour
	tvProgTTLEnded   = 30 * 24 * time.Hour
	tvProgFailPause  = 10 * time.Minute
	tvProgHaveTTL    = 2 * time.Minute
	tvProgStatusGone = "missing" // TMDB 上没有这部（404）：按完结的期限过期，免得每次打开都去问
)

// tvProgInfo 接口给一部剧的数
type tvProgInfo struct {
	Have  int  `json:"have"`  // 台账里有的正片集数（不含第 0 季，同一集几份算一集）
	Aired int  `json:"aired"` // TMDB 已播正片集数；0 = 还不知道
	Total int  `json:"total"` // TMDB 已排期正片集数
	Ended bool `json:"ended"` // 完结 / 取消
}

// parseTVProgress 从 /tv/{id} 的返回算已播 / 已排期正片集数。
// 已播 = 最近播出那一集之前各季的集数 + 它的集号；TMDB 没给最近播出时（还没开播）为 0。
// 只看正片季：第 0 季（特别篇）不进分母，台账那边同样不算
func parseTVProgress(body []byte) (status string, aired, total int, err error) {
	var raw struct {
		Status    string `json:"status"`
		LastToAir *struct {
			Season  int `json:"season_number"`
			Episode int `json:"episode_number"`
		} `json:"last_episode_to_air"`
		Seasons []struct {
			Season int `json:"season_number"`
			Count  int `json:"episode_count"`
		} `json:"seasons"`
	}
	if err = json.Unmarshal(body, &raw); err != nil {
		return
	}
	status = raw.Status
	for _, s := range raw.Seasons {
		if s.Season <= 0 {
			continue
		}
		total += s.Count
		if raw.LastToAir != nil && s.Season < raw.LastToAir.Season {
			aired += s.Count
		}
	}
	if l := raw.LastToAir; l != nil && l.Season > 0 {
		aired += l.Episode
	}
	if aired > total {
		total = aired // 季列表的集数偶尔比最近播出那集落后
	}
	return
}

// tvProgStale 这条记录要不要重拉
func tvProgStale(p model.TmdbTVProgress, now time.Time) bool {
	ttl := tvProgTTLAiring
	if tmdbEnded(p.Status) || p.Status == tvProgStatusGone {
		ttl = tvProgTTLEnded
	}
	return now.Sub(p.FetchedAt) > ttl
}

// countHaveEpisodes 台账里一个片目有几集正片（同一集几份、双集文件都按集号去重）。
// 失效 STRM（orphan_at 非空，网盘上已经没有）不算，与订阅盘点 subHaveOf 同口径
func countHaveEpisodes(key string, rows []model.SyncedFile) int {
	seen := map[epKey]bool{}
	for _, sf := range rows {
		if sf.OrphanAt != nil {
			continue
		}
		for _, k := range ledgerVideoEpisodes(key, sf.RelPath) {
			if k.S > 0 {
				seen[k] = true
			}
		}
	}
	return len(seen)
}

// ---- 台账集数缓存：每集都要解析文件名，全库一遍不便宜，短时间内反复打开页面复用 ----

var tvHave struct {
	mu sync.Mutex
	at time.Time
	m  map[string]int
}

func tvHaveCounts(keys map[string]bool) map[string]int {
	tvHave.mu.Lock()
	defer tvHave.mu.Unlock()
	if tvHave.m != nil && time.Since(tvHave.at) < tvProgHaveTTL {
		return tvHave.m
	}
	m := map[string]int{}
	for key, rows := range ledgerVideoRowsByTitle() {
		if keys[key] {
			m[key] = countHaveEpisodes(key, rows)
		}
	}
	tvHave.m, tvHave.at = m, time.Now()
	return m
}

// ---- 后台拉 TMDB ----

var tvProgFetch struct {
	mu      sync.Mutex
	running bool
	queue   map[int]bool
	failAt  time.Time
}

// kickTVProgress 把要拉的编号交给后台（已在跑就并进去），返回还没拉完的个数
func kickTVProgress(ids []int) int {
	f := &tvProgFetch
	f.mu.Lock()
	defer f.mu.Unlock()
	if time.Since(f.failAt) < tvProgFailPause {
		return 0 // 刚出过错（多半是连不上 TMDB）：先歇一会儿，别每次打开页面都撞一遍
	}
	if f.queue == nil {
		f.queue = map[int]bool{}
	}
	for _, id := range ids {
		f.queue[id] = true
	}
	n := len(f.queue)
	if n > 0 && !f.running {
		f.running = true
		go runTVProgressFetch()
	}
	return n
}

func runTVProgressFetch() {
	f := &tvProgFetch
	defer func() {
		f.mu.Lock()
		f.running = false
		f.mu.Unlock()
	}()
	tc, err := loadTmdbClient()
	if err != nil {
		f.mu.Lock()
		f.queue, f.failAt = nil, time.Now()
		f.mu.Unlock()
		return
	}
	done := 0
	for {
		f.mu.Lock()
		id := 0
		for k := range f.queue {
			id = k
			break
		}
		f.mu.Unlock()
		if id == 0 {
			break
		}
		row := model.TmdbTVProgress{TmdbID: id, FetchedAt: time.Now()}
		body, err := tc.get(fmt.Sprintf("/tv/%d", id), nil)
		switch {
		case err != nil && isTmdbNotFound(err):
			row.Status = tvProgStatusGone
		case err != nil:
			log.Printf("[海报墙] ✗ 读剧集播出进度失败（tv/%d）: %v，%v 后再试", id, err, tvProgFailPause)
			f.mu.Lock()
			f.queue, f.failAt = nil, time.Now()
			f.mu.Unlock()
			return
		default:
			if row.Status, row.Aired, row.Total, err = parseTVProgress(body); err != nil {
				row.Status = tvProgStatusGone
			}
		}
		if model.DB != nil {
			model.DB.Clauses(clause.OnConflict{UpdateAll: true}).Create(&row)
		}
		f.mu.Lock()
		delete(f.queue, id)
		f.mu.Unlock()
		done++
		time.Sleep(tvProgGap)
	}
	if done > 0 {
		log.Printf("[海报墙] ✓ 读了 %d 部剧集的播出进度", done)
	}
}

// LocalTVProgress GET /local/titles/tv-progress
// 返回 {configured, items: {片目 key: tvProgInfo}, pending}：pending > 0 时后台还在拉，前端过一会儿再要
func (h *Handler) LocalTVProgress(c *gin.Context) {
	if localMediaRoot() == "" || model.DB == nil {
		c.JSON(http.StatusOK, gin.H{"configured": false})
		return
	}
	if _, err := loadTmdbClient(); err != nil {
		c.JSON(http.StatusOK, gin.H{"configured": false})
		return
	}
	keyOf := map[string]int{}
	keys := map[string]bool{}
	var ids []int
	for _, t := range h.localTitlesSnapshot(false) {
		if t.MediaType != "tv" || t.TmdbID <= 0 {
			continue
		}
		keyOf[t.Key] = t.TmdbID
		keys[t.Key] = true
		ids = append(ids, t.TmdbID)
	}
	var rows []model.TmdbTVProgress
	if len(ids) > 0 {
		model.DB.Where("tmdb_id IN ?", ids).Find(&rows)
	}
	prog := make(map[int]model.TmdbTVProgress, len(rows))
	for _, r := range rows {
		prog[r.TmdbID] = r
	}
	now := time.Now()
	var stale []int
	seen := map[int]bool{}
	for _, id := range ids {
		if p, ok := prog[id]; (!ok || tvProgStale(p, now)) && !seen[id] {
			seen[id] = true
			stale = append(stale, id)
		}
	}
	pending := kickTVProgress(stale)

	have := tvHaveCounts(keys)
	items := make(map[string]tvProgInfo, len(keyOf))
	for key, id := range keyOf {
		p := prog[id]
		items[key] = tvProgInfo{Have: have[key], Aired: p.Aired, Total: p.Total, Ended: tmdbEnded(p.Status)}
	}
	c.JSON(http.StatusOK, gin.H{"configured": true, "items": items, "pending": pending})
}
