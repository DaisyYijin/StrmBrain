package api

import (
	"115-station/internal/model"
	"path"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

// ledgerTitleEntry 从台账聚合出的一个标题条目
type ledgerTitleEntry struct {
	Key       string // 标题目录的相对路径（含库名前缀，详情查询用）
	Title     string
	Year      string
	TmdbID    int
	MediaType string
	Category  string // 分类目录（库内相对路径，如 电影、电视剧/日番）；库根本身就是分类时为空
	// LibName Key 的第一段库名；老台账不带库名的两段式路径为空。换算网盘相对路径时剥掉它。
	// 由 titleAt 按「带库名那一轮匹配上」直接给出，别再按段数反推（库根分类时分类段数为 0，推不出）
	LibName string
	Videos  int // 台账里的视频数（海报墙的卡片显示）
	// LastAt 片目里最近一个视频首次入账的时间（海报墙「最近入库」、媒体信息补全的先后）。
	// 取 CreatedAt 不取 UpdatedAt：全量同步对每个文件都 upsert 一遍、刷新 updated_at，
	// 拿它排序的话手动全量一次，所有片目的「入库时间」都变成那次全量的时刻
	LastAt time.Time
}

// dirKey 片目所在目录（含库名前缀）：普通片目就是 Key，平铺片目是它所在的分类目录
func (e *ledgerTitleEntry) dirKey() string { return titleDirKey(e.Key) }

// solo 平铺片目（一个视频一部）
func (e *ledgerTitleEntry) solo() bool { return isSoloTitleKey(e.Key) }

// ---- 平铺片目 ----
//
// 有的用户不给每部电影单独建目录，视频直接放在分类目录里（2026-10-12 用户反馈：
// 影视/LGNB电影/LGNB中国香港电影/*.mkv）。Emby 把这样的每个文件当成一部电影，
// 海报墙此前要求「分类/标题目录/视频」，这些一部都不认，两边数量差出一大截。
//
// 现在电影分类目录里直接放着的视频，每个算一部：片目 key 就是这个 STRM 的相对路径
// （以 .strm 结尾，和目录形态的 key 撞不上）。这样的片目没有自己的目录，产物一律按视频名
// 写在它旁边（<视频名>.nfo、<视频名>-poster.jpg、<视频名>-fanart.jpg），这也是 Emby 在
// 一个目录放多部电影时认的写法；目录里的 poster.jpg / movie.nfo 是整个分类目录的，不算这一部的，
// 刮削也绝不写、不收它们。只认电影：剧集平铺 Emby 自己也认不成剧。
// 整理不往平铺目录里放（落盘仍是「分类/标题目录」），这里只管把已有的认出来。

// isSoloTitleKey key（或路径最后一段）是平铺片目
func isSoloTitleKey(key string) bool { return strings.HasSuffix(strings.ToLower(key), ".strm") }

// titleDirKey 片目 key → 所在目录：平铺片目取上一层，其余原样
func titleDirKey(key string) string {
	if isSoloTitleKey(key) {
		if d := path.Dir(key); d != "." {
			return d
		}
		return ""
	}
	return key
}

// soloTitleStem 平铺片目的视频名（STRM 去掉 .strm）
func soloTitleStem(key string) string {
	b := path.Base(key)
	return b[:len(b)-len(".strm")]
}

// titleKeyName 片目 key 的名字部分：目录名，或平铺片目的视频名
func titleKeyName(key string) string {
	if isSoloTitleKey(key) {
		return soloTitleStem(key)
	}
	return path.Base(key)
}

// soloAllowed 分类目录里直接放着的这个视频能不能单独算一部：电影分类可以，剧集分类不行；
// 电影 / 剧集同名的分类按文件名判断，带集号的不算
func soloAllowed(mediaType, strmName string) bool {
	switch mediaType {
	case "movie":
		return true
	case "tv":
		return false
	}
	return parseFileName(soloTitleStem(strmName)).Episode == 0
}

// soloTitleParse 平铺片目的片名 / 年份 / TMDB 编号（从视频名解析）
func soloTitleParse(stem string) (title, year string, tmdb int) {
	p := parseFileName(stem)
	title, year, tmdb = p.Title, p.Year, p.TmdbID
	if title == "" {
		title = stem
	}
	return
}

// 台账扫描缓存：刮削共享同一份结果（30 秒 TTL），
// 避免每次请求都全表扫描 SyncedFile。缓存条目视为只读，调用方不得修改。
var (
	ledgerTitlesMu       sync.Mutex
	ledgerTitlesCache    map[string]*ledgerTitleEntry
	ledgerUnclaimedCache ledgerUnclaimed
	ledgerTitlesAt       time.Time
)

func scanLedgerTitlesCached() map[string]*ledgerTitleEntry {
	titles, _ := scanLedgerCached(false)
	return titles
}

// ledgerUnclaimedCached 台账里不属于任何片目的视频（与 scanLedgerTitlesCached 同一次扫描、同一份缓存）
func ledgerUnclaimedCached() ledgerUnclaimed {
	_, u := scanLedgerCached(false)
	return u
}

// scanLedgerCached refresh = 不用缓存、重扫一遍并写回缓存（海报墙「刷新」）
func scanLedgerCached(refresh bool) (map[string]*ledgerTitleEntry, ledgerUnclaimed) {
	ledgerTitlesMu.Lock()
	defer ledgerTitlesMu.Unlock()
	if !refresh && ledgerTitlesCache != nil && time.Since(ledgerTitlesAt) < 30*time.Second {
		return ledgerTitlesCache, ledgerUnclaimedCache
	}
	ledgerTitlesCache, ledgerUnclaimedCache = scanLedger()
	ledgerTitlesAt = time.Now()
	return ledgerTitlesCache, ledgerUnclaimedCache
}

// forgetLedgerTitles 分类目录变了，台账要按新布局重新聚合
func forgetLedgerTitles() {
	ledgerTitlesMu.Lock()
	ledgerTitlesCache = nil
	ledgerTitlesMu.Unlock()
}

// ledgerUnclaimed 台账里认不出片目的视频：目录没登记成分类（不在分类规则、也不在其他媒体库目录里）。
// 海报墙据此提示「还有 N 个视频不属于任何片目」，而不是让用户对着空墙去跑全量同步
type ledgerUnclaimed struct {
	Videos int                  `json:"videos"`
	Dirs   []ledgerUnclaimedDir `json:"dirs"`
	// MoreDirs Dirs 只列视频最多的前 ledgerUnclaimedDirMax 个，其余目录的个数
	MoreDirs int `json:"more_dirs"`
}

// ledgerUnclaimedDir 按「库名 + 库内前两段」聚合：够定位是哪个目录，又不至于一个片目一行
type ledgerUnclaimedDir struct {
	Path   string `json:"path"`
	Videos int    `json:"videos"`
}

const ledgerUnclaimedDirMax = 20

// unclaimedDirOf 认不出的视频归到哪个目录：所在目录截到前三段（库名 + 库内两段）
func unclaimedDirOf(rel string) string {
	dir := path.Dir(strings.Trim(rel, "/"))
	if dir == "." {
		return ""
	}
	segs := strings.Split(dir, "/")
	if len(segs) > 3 {
		segs = segs[:3]
	}
	return strings.Join(segs, "/")
}

// summarizeUnclaimed 纯函数：目录 → 视频数 汇总成前若干个（视频多的在前，同数按路径）
func summarizeUnclaimed(byDir map[string]int) ledgerUnclaimed {
	out := ledgerUnclaimed{Dirs: []ledgerUnclaimedDir{}}
	for p, n := range byDir {
		out.Videos += n
		out.Dirs = append(out.Dirs, ledgerUnclaimedDir{Path: p, Videos: n})
	}
	sort.Slice(out.Dirs, func(i, j int) bool {
		if out.Dirs[i].Videos != out.Dirs[j].Videos {
			return out.Dirs[i].Videos > out.Dirs[j].Videos
		}
		return out.Dirs[i].Path < out.Dirs[j].Path
	})
	if len(out.Dirs) > ledgerUnclaimedDirMax {
		out.MoreDirs = len(out.Dirs) - ledgerUnclaimedDirMax
		out.Dirs = out.Dirs[:ledgerUnclaimedDirMax]
	}
	return out
}

// scanLedgerTitles 全量扫描台账，按「标题目录」聚合（库名/<分类>/<标题目录>/…）
func scanLedgerTitles() map[string]*ledgerTitleEntry {
	out, _ := scanLedger()
	return out
}

// scanLedger 同 scanLedgerTitles，顺带数出认不出片目的视频
func scanLedger() (map[string]*ledgerTitleEntry, ledgerUnclaimed) {
	layout := loadLibCategoryLayout()
	var sfs []model.SyncedFile
	model.DB.Where("kind = ?", "video").Find(&sfs)
	out := map[string]*ledgerTitleEntry{}
	unclaimed := map[string]int{}
	// 同名分类两边都有时按文件判断：片目里有一集带集号就是剧集
	undecided := map[string]bool{}
	for _, sf := range sfs {
		hit, ok := layout.titleAt(sf.RelPath)
		if !ok {
			unclaimed[unclaimedDirOf(sf.RelPath)]++
			continue
		}
		key, titleDir, category, mediaType := hit.Key, hit.TitleDir, hit.Category, hit.MediaType
		e, seen := out[key]
		if !seen {
			title, year, tmdb := parseTitleDir(titleDir)
			if isSoloTitleKey(key) {
				// 平铺的是视频文件名（常带画质、压制组），按文件名解析，目录名那套会把画质串当成片名
				title, year, tmdb = soloTitleParse(titleDir)
			}
			e = &ledgerTitleEntry{
				Key: key, Title: title, Year: year, TmdbID: tmdb,
				MediaType: mediaType, Category: category,
				// 带库名的那一轮匹配上才有；老台账不带库名的为空
				LibName: hit.Lib,
			}
			out[key] = e
			if mediaType == "" {
				e.MediaType = "movie"
				undecided[key] = true
			}
		}
		if undecided[key] && parseFileName(strings.TrimSuffix(path.Base(sf.RelPath), ".strm")).Episode > 0 {
			e.MediaType = "tv"
			delete(undecided, key)
		}
		e.Videos++
		if sf.CreatedAt.After(e.LastAt) {
			e.LastAt = sf.CreatedAt
		}
	}
	return out, summarizeUnclaimed(unclaimed)
}

var (
	reTitleDirYear    = regexp.MustCompile(`(?:19|20)\d{2}`)
	reTitleDirLetter  = regexp.MustCompile(`^[A-Z\d]-`)
	reTitleDirSepRuns = regexp.MustCompile(`([-. _])[-. _]+`)
)

// titleDirYearAt 最右侧一个「前后是分隔符或括号」的四位年份的位置，没有返回 -1。
// 边界手工判断而不写进正则：正则会把两个年份之间共用的那个分隔符算进前一个匹配，
// 「1917-2019」里的 2019 就再也匹配不上了（此前的实现正是这样，取到的是 1917）
func titleDirYearAt(s string) (int, int) {
	isSep := func(r rune) bool { return strings.ContainsRune("-. _([)]（）", r) }
	start, end := -1, -1
	for _, m := range reTitleDirYear.FindAllStringIndex(s, -1) {
		before, _ := utf8.DecodeLastRuneInString(s[:m[0]])
		after, _ := utf8.DecodeRuneInString(s[m[1]:])
		if (m[0] == 0 || isSep(before)) && (m[1] == len(s) || isSep(after)) {
			start, end = m[0], m[1]
		}
	}
	return start, end
}

// parseTitleDir 解析标题目录名 → (片名, 年份, TMDB 编号)：
//
//	"流浪地球.2019.{tmdbid=535167}"  → (流浪地球, 2019, 535167)   默认重命名模板
//	"流浪地球 (2019) [tmdbid=535167]" → (流浪地球, 2019, 535167)   Emby 风格
//	"Z-重器-2026-[tmdb=291856]"       → (重器, 2026, 291856)       首字母分组的老模板
//
// 编号标签交给识别环节同一个 takeTags 摘（[tmdbid=…] / [tmdb=…] / {tmdb-…} / {[tmdbid=…;type=tv]} 都认）。
// 此前只认 [tmdb=…]，而默认模板渲染出来的是 {tmdbid=…}：用默认模板的库，
// 刮削一个片目都找不到（它只刮带编号的），年份也不认括号
func parseTitleDir(dir string) (title, year string, tmdb int) {
	tag, rest := takeTags(dir)
	tmdb = tag.TmdbID
	// 取最右侧的年份（取最左会把片名本身是年份的 "1917-2019" 剜成 "2019"）
	if ys, ye := titleDirYearAt(rest); ys >= 0 {
		year = rest[ys:ye]
		before, after := rest[:ys], rest[ye:]
		// 包着年份的括号一起摘掉，不然片名尾巴上挂着一对空括号
		for _, p := range [][2]string{{"(", ")"}, {"[", "]"}, {"（", "）"}} {
			if strings.HasSuffix(before, p[0]) && strings.HasPrefix(after, p[1]) {
				before, after = strings.TrimSuffix(before, p[0]), strings.TrimPrefix(after, p[1])
				break
			}
		}
		rest = before + after
	}
	title = reTitleDirSepRuns.ReplaceAllString(rest, "$1") // 摘掉年份留下的 ".." / "--"
	title = reTitleDirLetter.ReplaceAllString(title, "")
	title = strings.Trim(title, "- _.[]()（）")
	if title == "" {
		title = dir
	}
	return
}
