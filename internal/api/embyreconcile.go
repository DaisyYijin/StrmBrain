package api

import (
	"net/http"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"github.com/gin-gonic/gin"
)

// ==================== Emby 与本地片目对账 ====================
//
// 总览的电影 / 剧集数读的是 Emby（/Items/Counts，全服务器），海报墙数的是台账片目
// （scanLedgerTitles：只认当前分类规则下的标题目录）。两个口径天然可能不一致，
// 用户看到「剧集 151 / 海报墙 134」只能干猜。这里把 Emby 的每个 Movie / Series 映射回本地，
// 逐个说清楚差在哪：不在本站目录下、Emby 残留、分类规则外、台账里没有、被拆成几个条目、类型不一致、
// 以及反方向「本地有、Emby 没有」。
//
// 只读：只打 Emby（几页 Movie + Series）与本地 os.Stat（只对没对上的条目），零 115 请求。
// 前提是路径映射对：一个都对不上时不出分桶（全会落进「不在本站目录下」，毫无意义），只给映射建议。

// embyReconItem Emby 的片目级条目（只要路径与年份）
type embyReconItem struct {
	ID             string `json:"Id"`
	Name           string `json:"Name"`
	Type           string `json:"Type"`
	Path           string `json:"Path"`
	ProductionYear int    `json:"ProductionYear"`
	// ProviderIds 判平铺 / 多版本用（judgeMovieDir：TMDB 编号比片名可靠）
	ProviderIds map[string]string `json:"ProviderIds"`
}

// embyReconEntry 分桶里的一条
type embyReconEntry struct {
	Name  string `json:"name"`
	Year  int    `json:"year,omitempty"`
	Path  string `json:"path,omitempty"`  // Emby 里的路径
	Local string `json:"local,omitempty"` // 换算到本地的路径
	Key   string `json:"key,omitempty"`   // 对上的台账片目
	Count int    `json:"count,omitempty"` // split：这个片目在 Emby 里有几个条目
}

// embyReconDir 分类规则外的条目按所在目录聚合：用户要做的是「把这个目录加进分类规则」
type embyReconDir struct {
	Dir string `json:"dir"` // 本地根下的相对路径
	// Extra 加进「其他媒体库目录」时的写法：去掉第一段库名（本地根下第一层是 115 库目录名）；
	// 片目直接放在库根下时为空，加不进去
	Extra string `json:"extra,omitempty"`
	// Flat 平铺：电影视频直接放在这个目录里（一个视频一部），Dir 就是放视频的目录
	Flat bool `json:"flat,omitempty"`
	// Unsure 平铺还是多版本认不准（judgeMovieDir）：按「一部片」归到了上一层，UnsureWhy 说原因
	Unsure    bool   `json:"unsure,omitempty"`
	UnsureWhy string `json:"unsure_why,omitempty"`
	// Lib / Rel 登记成分类目录时的写法：库名 + 库内路径（Rel 为空 = 库根，只能按库登记）。
	// Extra 是老写法（不限定库），库根时为空
	Lib     string   `json:"lib,omitempty"`
	Rel     string   `json:"rel"`
	Count   int      `json:"count"`
	Samples []string `json:"samples"`
}

// embyReconBucket 一个分桶：总数 + 至多 embyReconListMax 条明细
type embyReconBucket struct {
	Count int              `json:"count"`
	Items []embyReconEntry `json:"items"`
}

func (b *embyReconBucket) add(e embyReconEntry) {
	b.Count++
	if len(b.Items) < embyReconListMax {
		b.Items = append(b.Items, e)
	}
}

// embyReconSide 一种媒体类型（movie / tv）的对账结果。
// 恒等式：Emby − 台账 = Outside + Stale + Uncategorized + Unledgered + TypeMismatch + SplitExtra − Missing
type embyReconSide struct {
	Emby   int `json:"emby"`   // Emby 里这一类的条目数
	Ledger int `json:"ledger"` // 台账（海报墙）里这一类的片目数
	// Matched 对上了的片目数（去重后）
	Matched int `json:"matched"`
	// Outside 换算后不在本地媒体库根下：Emby 另挂的库，本站不管
	Outside embyReconBucket `json:"outside"`
	// Stale 在根下但本地已经没有了：Emby 没扫库清理的残留条目
	Stale embyReconBucket `json:"stale"`
	// Uncategorized 本地在、却不在当前任何分类目录下：海报墙不认它
	Uncategorized   embyReconBucket `json:"uncategorized"`
	UncategorizedBy []embyReconDir  `json:"uncategorized_dirs"`
	// Unledgered 落在分类目录下、本地在，但台账没有这个片目（不是本站同步 / 整理出来的 STRM）
	Unledgered embyReconBucket `json:"unledgered"`
	// TypeMismatch 对上的片目在台账里是另一种类型（Emby 认成剧集、台账算电影，或反过来）
	TypeMismatch embyReconBucket `json:"type_mismatch"`
	// Split 同一个片目在 Emby 里有多个条目；SplitExtra = 多出来的条目数
	Split      embyReconBucket `json:"split"`
	SplitExtra int             `json:"split_extra"`
	// Missing 台账有、Emby 里没有对应类型的条目
	Missing embyReconBucket `json:"missing"`
}

// embyReconcile 对账结果
type embyReconcile struct {
	MappingOK bool          `json:"mapping_ok"`
	Scanned   int           `json:"scanned"`
	Movie     embyReconSide `json:"movie"`
	TV        embyReconSide `json:"tv"`
}

const embyReconListMax = 100

// embyReconPathExists 换成本地路径后判断存在（可在测试里替换）
var embyReconPathExists = func(p string) bool {
	_, err := os.Stat(filepath.FromSlash(p))
	return err == nil
}

// reconcileEmby 纯函数：Emby 条目 × 台账片目 → 分桶。
// toLocal 把 Emby 路径换成本地路径；exists 判断本地路径在不在
func reconcileEmby(items []embyReconItem, ledger map[string]*ledgerTitleEntry, layout libCategoryLayout,
	rootSlash string, toLocal func(string) string, exists func(string) bool) embyReconcile {
	rootSlash = strings.TrimRight(filepath.ToSlash(rootSlash), "/")
	out := embyReconcile{Scanned: len(items)}
	side := func(t string) *embyReconSide {
		if t == "Series" {
			return &out.TV
		}
		return &out.Movie
	}
	for _, e := range ledger {
		if e.MediaType == "tv" {
			out.TV.Ledger++
		} else {
			out.Movie.Ledger++
		}
	}

	type hit struct{ items []embyReconItem }
	hits := map[string]map[string]*hit{"Movie": {}, "Series": {}} // 类型 → 片目 key → 条目
	// 分类规则外的条目先攒着：要等全部看完才知道哪些电影目录是平铺的
	type uncat struct {
		typ, titleDir, name, tmdb string
	}
	var uncats []uncat
	for _, it := range items {
		if it.Type != "Movie" && it.Type != "Series" {
			continue
		}
		s := side(it.Type)
		s.Emby++
		local := strings.TrimRight(filepath.ToSlash(toLocal(it.Path)), "/")
		entry := embyReconEntry{Name: it.Name, Year: it.ProductionYear, Path: it.Path, Local: local}
		if local == "" || !strings.HasPrefix(local, rootSlash+"/") {
			s.Outside.add(entry)
			continue
		}
		dir := it.Type == "Series" // Series 的路径就是标题目录；Movie 的是视频文件
		if key := localEmbyTitleKeyOf(local, rootSlash, ledger, dir); key != "" {
			entry.Key = key
			want := "movie"
			if dir {
				want = "tv"
			}
			if ledger[key].MediaType != want {
				s.TypeMismatch.add(entry)
				continue
			}
			h := hits[it.Type][key]
			if h == nil {
				h = &hit{}
				hits[it.Type][key] = h
			}
			h.items = append(h.items, it)
			continue
		}
		if !exists(local) {
			s.Stale.add(entry)
			continue
		}
		rel := strings.TrimPrefix(local, rootSlash+"/")
		probe := rel
		if dir {
			probe += "/x" // titleOf 要的是标题目录下的文件
		}
		if _, _, _, _, ok := layout.titleOf(probe); ok {
			s.Unledgered.add(entry)
			continue
		}
		s.Uncategorized.add(entry)
		titleDir := rel
		if !dir {
			titleDir = path.Dir(rel)
		}
		uncats = append(uncats, uncat{typ: it.Type, titleDir: titleDir, name: it.Name, tmdb: providerID(it.ProviderIds, "Tmdb")})
	}

	// 一个目录里有几个电影条目：平铺（视频直接放在分类目录里，这个目录本身就是该加的分类目录）
	// 还是同一部的多版本（Emby 一个版本一个 Movie 条目），按「标题目录的上一层」算。
	// 判定与布局建议同一个函数（liblayoutsuggest.go 的 judgeMovieDir：先比 TMDB 编号再比片名）
	movieIn := map[string][]movieDirItem{}
	for _, u := range uncats {
		if u.typ == "Movie" {
			movieIn[u.titleDir] = append(movieIn[u.titleDir], movieDirItem{Name: u.name, Tmdb: u.tmdb})
		}
	}
	type dirVerdict struct {
		v   movieDirVerdict
		why string
	}
	verdicts := map[string]dirVerdict{}
	for d, its := range movieIn {
		v, why := judgeMovieDir(its, nil)
		verdicts[d] = dirVerdict{v, why}
	}
	uncatDirs := map[string]map[string]*embyReconDir{"Movie": {}, "Series": {}}
	for _, u := range uncats {
		vd := verdicts[u.titleDir]
		flat := u.typ == "Movie" && vd.v == verdictFlat
		// 归到「标题目录的上一层」：那一层就是该加进分类规则的目录
		parent, sample := path.Dir(u.titleDir), path.Base(u.titleDir)
		if flat {
			parent, sample = u.titleDir, u.name
		}
		if parent == "." {
			parent = ""
		}
		d := uncatDirs[u.typ][parent]
		if d == nil {
			d = &embyReconDir{Dir: parent, Flat: flat}
			if _, rest, ok := strings.Cut(parent, "/"); ok {
				d.Extra = rest
			}
			if parent != "" {
				d.Lib, d.Rel = splitLibRel(parent)
			}
			uncatDirs[u.typ][parent] = d
		}
		if u.typ == "Movie" && vd.v == verdictUnsure && !d.Unsure {
			d.Unsure, d.UnsureWhy = true, vd.why
		}
		d.Count++
		if len(d.Samples) < 5 {
			d.Samples = append(d.Samples, sample)
		}
	}

	for typ, byKey := range hits {
		s := side(typ)
		s.Matched = len(byKey)
		keys := make([]string, 0, len(byKey))
		for k := range byKey {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			h := byKey[k]
			if n := len(h.items); n > 1 {
				s.SplitExtra += n - 1
				s.Split.add(embyReconEntry{Name: h.items[0].Name, Year: h.items[0].ProductionYear, Key: k, Count: n})
			}
		}
		for _, d := range uncatDirs[typ] {
			s.UncategorizedBy = append(s.UncategorizedBy, *d)
		}
		sort.Slice(s.UncategorizedBy, func(i, j int) bool {
			a, b := s.UncategorizedBy[i], s.UncategorizedBy[j]
			if a.Count != b.Count {
				return a.Count > b.Count
			}
			return a.Dir < b.Dir
		})
	}

	// 反方向：台账有、Emby 没有对应类型的条目
	keys := make([]string, 0, len(ledger))
	for k := range ledger {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		e := ledger[k]
		typ, s := "Movie", &out.Movie
		if e.MediaType == "tv" {
			typ, s = "Series", &out.TV
		}
		if hits[typ][k] == nil {
			y, _ := strconv.Atoi(e.Year)
			s.Missing.add(embyReconEntry{Name: e.Title, Year: y, Key: k})
		}
	}

	out.MappingOK = out.Movie.Matched+out.TV.Matched+out.Movie.TypeMismatch.Count+out.TV.TypeMismatch.Count > 0 ||
		len(items) == 0 || len(ledger) == 0
	return out
}

// fetchEmbyReconItems 读全服务器的 Movie / Series（总览的 /Items/Counts 也是全服务器，口径要对齐）
func fetchEmbyReconItems(cfg embyRefreshCfg) ([]embyReconItem, error) {
	var all []embyReconItem
	for start := 0; start < localEmbyMaxItems; start += localEmbyPage {
		q := url.Values{
			"Recursive":              {"true"},
			"IncludeItemTypes":       {"Movie,Series"},
			"Fields":                 {"Path,ProductionYear,ProviderIds"},
			"SortBy":                 {"DateCreated,SortName"},
			"SortOrder":              {"Ascending"},
			"StartIndex":             {strconv.Itoa(start)},
			"Limit":                  {strconv.Itoa(localEmbyPage)},
			"EnableTotalRecordCount": {"false"},
			"EnableImages":           {"false"},
			"EnableUserData":         {"false"},
		}
		var page struct {
			Items []embyReconItem `json:"Items"`
		}
		if err := embyGetJSON(cfg.ServerURL, cfg.APIKey, "/Items", q, &page); err != nil {
			return nil, err
		}
		all = append(all, page.Items...)
		if len(page.Items) < localEmbyPage {
			break
		}
	}
	return all, nil
}

// ---- 路径映射推算 ----

// embyRootEvidence 推算依据：Emby 媒体库里的这个目录，对上了本地的那个目录
type embyRootEvidence struct {
	Location string `json:"location"`
	Local    string `json:"local"`
}

// suggestEmbyRoot 纯函数：按 Emby 媒体库的目录推算映射两侧该填什么，返回 Emby 那一侧的根与本地这一侧的子目录。
//
// 做法是拿 Emby 目录的尾巴去本地根下找：Emby 的 /mnt/strm/剧集 → 本地 /media/strm/剧集 存在 →
// Emby 根就是 /mnt/strm。尾巴取能对上的最长一段（本地若是 根/115/剧集，Emby 的 /data/115/剧集 推出 /data）。
// 各个媒体库各推一个，取票数最多的（同票取对上段数多的）；别的硬盘上的库对不上本地任何目录，不投票。
//
// 本地根下对不上时再往下找一层：subs 是本地根下的各个库目录（STRM 路径第一层是 115 媒体库目录名），
// Emby 直接从库目录挂进去时（Emby /Movies/电影 ⇄ 本地 /Movies/资源库/电影）推出子目录「资源库」。
// 根下对上了就不看子目录：两边都对得上时根下那个是现状，别替用户改。
// 还没对上时退一步：Emby 目录名与本地根（或某个库目录）同名，认为媒体库直接挂的就是它。
func suggestEmbyRoot(locations []string, localRoot string, subs []string, isDir func(string) bool) (string, string, []embyRootEvidence) {
	localRoot = strings.TrimRight(filepath.ToSlash(localRoot), "/")
	if localRoot == "" {
		return "", "", nil
	}
	type cand struct {
		root, sub    string
		votes, depth int
		ev           []embyRootEvidence
	}
	cands := map[string]*cand{}
	var order []string
	vote := func(root, sub string, depth int, ev embyRootEvidence) {
		key := root + "\x00" + sub
		c := cands[key]
		if c == nil {
			c = &cand{root: root, sub: sub}
			cands[key] = c
			order = append(order, key)
		}
		c.votes++
		c.depth += depth
		c.ev = append(c.ev, ev)
	}
	var locs []string
	for _, raw := range locations {
		if loc := strings.TrimRight(strings.ReplaceAll(raw, "\\", "/"), "/"); loc != "" {
			locs = append(locs, loc)
		}
	}
	byTail := func(sub string) {
		base := localRoot
		if sub != "" {
			base += "/" + sub
		}
		for _, loc := range locs {
			segs := strings.Split(loc, "/")
			for k := len(segs) - 1; k >= 1; k-- {
				prefix := strings.Join(segs[:len(segs)-k], "/")
				if strings.Trim(prefix, "/") == "" {
					continue // Emby 根成了「/」：映射存不下这种值，也不太可能是真的
				}
				suffix := strings.Join(segs[len(segs)-k:], "/")
				if local := base + "/" + suffix; isDir(local) {
					vote(prefix, sub, k, embyRootEvidence{Location: loc, Local: local})
					break
				}
			}
		}
	}
	byTail("")
	if len(cands) == 0 {
		for _, sub := range subs {
			if sub = embyLocalSub(sub); sub != "" {
				byTail(sub)
			}
		}
	}
	if len(cands) == 0 {
		for _, loc := range locs {
			if path.Base(loc) == path.Base(localRoot) {
				vote(loc, "", 0, embyRootEvidence{Location: loc, Local: localRoot})
				continue
			}
			for _, sub := range subs {
				if sub = embyLocalSub(sub); sub != "" && path.Base(loc) == path.Base(sub) {
					vote(loc, sub, 0, embyRootEvidence{Location: loc, Local: localRoot + "/" + sub})
					break
				}
			}
		}
	}
	var best *cand
	for _, k := range order {
		c := cands[k]
		if best == nil || c.votes > best.votes || (c.votes == best.votes && c.depth > best.depth) {
			best = c
		}
	}
	if best == nil {
		return "", "", nil
	}
	return best.root, best.sub, best.ev
}

// ledgerLibNames 台账里的库目录名（片目 key 的第一段，即 STRM 路径第一层），给推算往下找一层用
func ledgerLibNames() []string {
	seen := map[string]bool{}
	var out []string
	for key := range scanLedgerTitlesCached() {
		lib, _, _ := strings.Cut(key, "/")
		if lib != "" && !seen[lib] {
			seen[lib] = true
			out = append(out, lib)
		}
	}
	sort.Strings(out)
	return out
}

func embyReconIsDir(p string) bool {
	st, err := os.Stat(filepath.FromSlash(p))
	return err == nil && st.IsDir()
}

// embyPathSuggestion 当前映射与推算结果（两个接口共用）
func embyPathSuggestion(cfg embyRefreshCfg, rootSlash string) gin.H {
	currentLocal, current := embyPathRoots(cfg.PathMapping)
	libs, err := embyVirtualFolders(cfg)
	if err != nil {
		return gin.H{"local_root": rootSlash, "current": current, "current_local": currentLocal, "error": err.Error()}
	}
	var locs []string
	type libInfo struct {
		Name      string   `json:"name"`
		Locations []string `json:"locations"`
		UnderRoot bool     `json:"under_root"` // 按当前映射，这个库在不在本地媒体库根下
	}
	libOut := make([]libInfo, 0, len(libs))
	covered := 0
	for _, lib := range libs {
		li := libInfo{Name: lib.Name, Locations: lib.Locations}
		for _, loc := range lib.Locations {
			locs = append(locs, loc)
			if embyLocUnderRoot(cfg.PathMapping, loc, rootSlash) {
				li.UnderRoot = true
			}
		}
		if li.UnderRoot {
			covered++
		}
		libOut = append(libOut, li)
	}
	suggest, suggestSub, ev := suggestEmbyRoot(locs, rootSlash, ledgerLibNames(), embyReconIsDir)
	if ev == nil {
		ev = []embyRootEvidence{}
	}
	suggestLocal := ""
	if suggest != "" {
		suggestLocal = rootSlash
		if suggestSub != "" {
			suggestLocal += "/" + suggestSub
		}
	}
	return gin.H{
		"local_root": rootSlash, "current": current, "current_local": currentLocal,
		"suggest": suggest, "suggest_local": suggestLocal, "evidence": ev,
		"libraries": libOut, "covered": covered,
	}
}

// EmbyPathSuggest GET /emby/path-suggest：按 Emby 媒体库目录推算「Emby 媒体库目录」该填什么。
// 只给建议不保存：映射改错了 302 播放与删除联动都会出错，由用户在设置页确认
func (h *Handler) EmbyPathSuggest(c *gin.Context) {
	cfg, ok := loadEmbyRefreshCfg()
	if !ok {
		c.JSON(http.StatusOK, gin.H{"configured": false})
		return
	}
	root := strings.TrimRight(filepath.ToSlash(localMediaRoot()), "/")
	out := embyPathSuggestion(cfg, root)
	out["configured"] = true
	c.JSON(http.StatusOK, out)
}

// LocalTitlesReconcile GET /local/titles/reconcile：Emby 与海报墙的电影 / 剧集数逐项对账
func (h *Handler) LocalTitlesReconcile(c *gin.Context) {
	cfg, ok := loadEmbyRefreshCfg()
	root := strings.TrimRight(filepath.ToSlash(localMediaRoot()), "/")
	if !ok || root == "" {
		c.JSON(http.StatusOK, gin.H{"configured": false})
		return
	}
	items, err := fetchEmbyReconItems(cfg)
	if err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": "读取 Emby 条目失败：" + err.Error()})
		return
	}
	res := reconcileEmby(items, scanLedgerTitlesCached(), loadLibCategoryLayout(), root,
		func(p string) string { return embyPathToLocal(cfg.PathMapping, p) }, embyReconPathExists)
	out := gin.H{"configured": true, "local_root": root, "result": res}
	if !res.MappingOK {
		out["mapping"] = embyPathSuggestion(cfg, root)
	}
	c.JSON(http.StatusOK, out)
}
