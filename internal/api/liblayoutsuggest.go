package api

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode"

	"115-station/internal/model"

	"github.com/gin-gonic/gin"
)

// ==================== 媒体库布局：由 Emby 的条目结构推出「该登记哪些分类目录」 ====================
//
// 设计见 docs/115-station-notes/LIBRARY-LAYOUT-PLAN.md §3.3–§3.4、§4。
//
// 这不是按目录结构猜：Emby 是实际消费这些文件的一方，一个 Series 的路径就是片目目录，
// 一个 Movie 的路径是视频（或光盘目录），用户在 Emby 里看到的就是这样分的。但推出来的东西**只当建议**，
// 用户在预览里看过「会多认出哪些片目、哪些没有佐证、哪些已有片目的 key 会变」再点采纳，
// 采纳后写进其他媒体库目录（extradirs.go），运行时不依赖 Emby。
//
// 最容易认反的是电影目录里有几个 Movie 条目时：是「平铺」（视频直接放在分类目录里，一个视频一部），
// 还是「多版本」（同一部片的几个版本，Emby 实测一个版本一个 Movie 条目，AGENTS.md §6.16）。
// 认反的后果是整类认不出或一部拆成几张卡片，所以判定先看 TMDB 编号、再看片名，
// 和文件名给出的信号矛盾就不出建议，进「认不准」交给用户（judgeMovieDir）。

// ---- 平铺还是多版本 ----

type movieDirVerdict int

const (
	verdictTitle  movieDirVerdict = iota // 这个目录是一部片（单个条目，或同一部的几个版本）
	verdictFlat                          // 平铺：目录里每个视频各是一部
	verdictUnsure                        // 认不准
)

// movieDirItem 同一个目录下的一个 Movie 条目
type movieDirItem struct {
	Name string // Emby 的条目名
	Tmdb string // ProviderIds 里的 TMDB 编号，可能没有
}

// normMovieName 比片名用：去掉多版本后缀（「片名 - 4K」）、年份与画质串，只留字母数字，小写
func normMovieName(name string) string {
	if i := strings.Index(name, " - "); i > 0 {
		name = name[:i]
	}
	if t := parseFileName(name).Title; t != "" {
		name = t
	}
	var b strings.Builder
	for _, r := range strings.ToLower(name) {
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			b.WriteRune(r)
		}
	}
	return b.String()
}

// judgeMovieDir 一个目录下有几个 Movie 条目时，判它是平铺还是一部片（多版本）。
// stems 是台账里这个目录下直接放着的 STRM 名（去掉 .strm），可为空（对账不读台账文件名）。
//
//   - 全都有 TMDB 编号：两个以上不同的编号 = 平铺，全一样 = 多版本；
//   - 有缺的：按片名比，片名不同 = 平铺；但有编号的那几个互相不一样、片名却全一样 → 认不准；
//   - 文件名只能佐证「是同一部」：判成平铺、文件名解析出的片名却全一样（典型是多版本被刮成了不同的条目）→ 认不准。
//     反过来不当证据：同一部片的几个版本，文件名一个中文一个英文是常事。
func judgeMovieDir(items []movieDirItem, stems []string) (movieDirVerdict, string) {
	if len(items) <= 1 {
		return verdictTitle, ""
	}
	tmdbs, names := map[string]bool{}, map[string]bool{}
	all := true
	for _, it := range items {
		if it.Tmdb != "" {
			tmdbs[it.Tmdb] = true
		} else {
			all = false
		}
		names[normMovieName(it.Name)] = true
	}
	var v movieDirVerdict
	switch {
	case all && len(tmdbs) >= 2:
		v = verdictFlat
	case all:
		v = verdictTitle
	case len(names) >= 2:
		v = verdictFlat
	case len(tmdbs) >= 2:
		return verdictUnsure, "片名一样，TMDB 编号却不同（同名的不同影片，还是同一部被认错了？）"
	default:
		v = verdictTitle
	}
	if v == verdictFlat && len(stems) >= 2 {
		same := map[string]bool{}
		for _, s := range stems {
			same[normMovieName(s)] = true
		}
		if len(same) == 1 {
			return verdictUnsure, "Emby 里是几部不同的片，文件名却是同一部（多版本被刮成了不同条目？）"
		}
	}
	return v, ""
}

// ---- 由 Emby 条目推建议 ----

// layoutSuggestion 一条建议：把库 Lib 里的 Dir 登记成分类目录（Dir 为空 = 库根本身）
type layoutSuggestion struct {
	Lib     string   `json:"lib"`
	Dir     string   `json:"dir"`
	Type    string   `json:"type"` // movie / tv / auto
	Source  string   `json:"source"`
	Titles  int      `json:"titles"` // Emby 给出的片目数（佐证）
	Samples []string `json:"samples"`
	keys    []string // 佐证片目的 key（预览用来判断新认出的片目有没有佐证）
}

// layoutUnsure 认不准的目录：说明原因，交给用户手动登记
type layoutUnsure struct {
	Lib     string   `json:"lib"`
	Dir     string   `json:"dir"`
	Reason  string   `json:"reason"`
	Samples []string `json:"samples"`
}

// layoutSuggestResult 建议推导结果
type layoutSuggestResult struct {
	// MappingOK 至少有一个 Emby 条目换到本地后对上了台账；false 时整个来源不出建议（与对账同口径）
	MappingOK   bool               `json:"mapping_ok"`
	Suggestions []layoutSuggestion `json:"suggestions"`
	Unsure      []layoutUnsure     `json:"unsure"`
	Dismissed   int                `json:"dismissed"` // 被用户忽略、这次没列出的建议数
}

const layoutSampleMax = 5

// layoutLedgerIndex 台账视频的几种索引（推建议、预览共用）
type layoutLedgerIndex struct {
	videos    []string            // 全部视频的相对路径
	videoSet  map[string]bool     // 视频路径
	dirSet    map[string]bool     // 视频的各级上层目录
	stemsByIn map[string][]string // 目录 → 直接放在里面的 STRM 名（去掉 .strm）
}

func newLayoutLedgerIndex(videos []string) layoutLedgerIndex {
	ix := layoutLedgerIndex{videos: videos, videoSet: map[string]bool{}, dirSet: map[string]bool{}, stemsByIn: map[string][]string{}}
	for _, v := range videos {
		v = strings.Trim(v, "/")
		ix.videoSet[v] = true
		dir := path.Dir(v)
		ix.stemsByIn[dir] = append(ix.stemsByIn[dir], strings.TrimSuffix(path.Base(v), ".strm"))
		for d := dir; d != "." && d != "" && !ix.dirSet[d]; d = path.Dir(d) {
			ix.dirSet[d] = true
		}
	}
	return ix
}

// layoutCatKey (库, 库内目录) 的 map key
func layoutCatKey(lib, dir string) string { return lib + "/" + dir }

// suggestLayoutFromEmby 纯函数：Emby 的 Movie / Series × 台账视频 × 当前布局 → 建议与认不准的目录。
// toLocal 把 Emby 路径换成本地路径；dismissed 是用户忽略过的（layoutCatKey）
func suggestLayoutFromEmby(items []embyReconItem, layout libCategoryLayout, ix layoutLedgerIndex,
	rootSlash string, toLocal func(string) string, dismissed map[string]bool) layoutSuggestResult {
	rootSlash = strings.TrimRight(filepath.ToSlash(rootSlash), "/")
	out := layoutSuggestResult{Suggestions: []layoutSuggestion{}, Unsure: []layoutUnsure{}}

	type evidence struct {
		key, name, typ string
		solo           bool
	}
	type catAgg struct {
		lib, dir string
		ev       []evidence
		types    map[string]bool
		unsure   string
	}
	cats := map[string]*catAgg{}
	var order []string
	addEv := func(titleKey, name, typ string, solo bool) {
		// 分类目录 = 片目的上一层（平铺片目的上一层就是它所在的目录）；为库目录本身时 dir 为空 = 库根
		parent := path.Dir(titleKey)
		if parent == "." {
			return // 片目直接在本地根下：没有库名，不归任何库
		}
		lib, dir := splitLibRel(parent)
		k := layoutCatKey(lib, dir)
		c := cats[k]
		if c == nil {
			c = &catAgg{lib: lib, dir: dir, types: map[string]bool{}}
			cats[k] = c
			order = append(order, k)
		}
		c.ev = append(c.ev, evidence{key: titleKey, name: name, typ: typ, solo: solo})
		c.types[typ] = true
	}
	markUnsure := func(lib, dir, reason, sample string) {
		k := layoutCatKey(lib, dir)
		c := cats[k]
		if c == nil {
			c = &catAgg{lib: lib, dir: dir, types: map[string]bool{}}
			cats[k] = c
			order = append(order, k)
		}
		if c.unsure == "" {
			c.unsure = reason
		}
		if sample != "" {
			c.ev = append(c.ev, evidence{name: sample})
		}
	}

	type movieAt struct {
		item embyReconItem
		rel  string
	}
	moviesIn := map[string][]movieAt{}
	var movieDirs []string
	mapped := false
	for _, it := range items {
		if it.Type != "Movie" && it.Type != "Series" {
			continue // Video（家庭视频）、BoxSet、Folder 不当证据
		}
		local := strings.TrimRight(filepath.ToSlash(toLocal(it.Path)), "/")
		if local == "" || !strings.HasPrefix(local, rootSlash+"/") {
			continue
		}
		rel := strings.TrimPrefix(local, rootSlash+"/")
		file := ix.videoSet[rel]
		if !file && !ix.dirSet[rel] {
			continue // 台账里没有：Emby 残留，或不是本站同步的
		}
		mapped = true
		if it.Type == "Series" || !file {
			// Series 的路径就是片目目录；Movie 指向目录的是光盘结构，同样是片目目录
			if _, ok := layout.titleAt(rel + "/_"); ok {
				continue
			}
			typ := "movie"
			if it.Type == "Series" {
				typ = "tv"
			}
			if !strings.Contains(rel, "/") {
				continue // 库目录本身就是一部剧：库下面没有片目这一层，登记什么都不对
			}
			addEv(rel, it.Name, typ, false)
			continue
		}
		if _, ok := layout.titleAt(rel); ok {
			continue
		}
		dir := path.Dir(rel)
		if moviesIn[dir] == nil {
			movieDirs = append(movieDirs, dir)
		}
		moviesIn[dir] = append(moviesIn[dir], movieAt{item: it, rel: rel})
	}
	out.MappingOK = mapped || len(items) == 0

	sort.Strings(movieDirs)
	for _, dir := range movieDirs {
		ms := moviesIn[dir]
		di := make([]movieDirItem, 0, len(ms))
		for _, m := range ms {
			di = append(di, movieDirItem{Name: m.item.Name, Tmdb: providerID(m.item.ProviderIds, "Tmdb")})
		}
		v, why := judgeMovieDir(di, ix.stemsByIn[dir])
		if !strings.Contains(dir, "/") {
			v = verdictFlat // 视频直接放在库根下：只能是平铺，库根就是分类目录
		}
		// 电影条目的文件名带集号：剧集被当成电影库扫了，这个分类目录不出建议（§4.2）
		for _, m := range ms {
			if parseFileName(strings.TrimSuffix(path.Base(m.rel), ".strm")).Episode > 0 {
				cat := dir // 平铺：分类目录就是它；一部片：分类目录是上一层（dir 至少两段，见上）
				if v != verdictFlat {
					cat = path.Dir(dir)
				}
				lib, d := splitLibRel(cat)
				markUnsure(lib, d, "Emby 把带集号的视频认成了电影（剧集目录放进了电影库？）", m.item.Name)
				v, why = verdictUnsure, ""
				break
			}
		}
		switch v {
		case verdictUnsure:
			if why != "" {
				lib, d := splitLibRel(dir)
				markUnsure(lib, d, why, ms[0].item.Name)
			}
		case verdictFlat:
			for _, m := range ms {
				addEv(m.rel, m.item.Name, "movie", true)
			}
		default:
			addEv(dir, ms[0].item.Name, "movie", false)
		}
	}

	// 一个目录同时被认成片目和分类目录（目录里既有直接放的电影、又有片目子目录等）：认不准
	titleDirs := map[string]bool{}
	for _, k := range order {
		for _, e := range cats[k].ev {
			if e.key != "" && !e.solo {
				titleDirs[e.key] = true
			}
		}
	}
	// 两边都要标：被当成分类的那个目录，以及把它当成片目的上一层（登记上一层就等于把它定成了片目）
	catFull := map[string]bool{}
	fullOf := func(c *catAgg) string {
		if c.dir == "" {
			return c.lib
		}
		return c.lib + "/" + c.dir
	}
	for _, k := range order {
		catFull[fullOf(cats[k])] = true
	}
	for _, k := range order {
		c := cats[k]
		if c.unsure != "" {
			continue
		}
		if titleDirs[fullOf(c)] {
			c.unsure = "这个目录在 Emby 里既是一部片、又装着别的片"
			continue
		}
		for _, e := range c.ev {
			if e.key != "" && !e.solo && catFull[e.key] {
				c.unsure = "下面的「" + path.Base(e.key) + "」在 Emby 里既是一部片、又装着别的片"
				break
			}
		}
	}

	for _, k := range order {
		c := cats[k]
		var samples []string
		var keys []string
		for _, e := range c.ev {
			if e.key != "" {
				keys = append(keys, e.key)
			}
			if len(samples) < layoutSampleMax && e.name != "" {
				samples = append(samples, e.name)
			}
		}
		if c.unsure != "" {
			out.Unsure = append(out.Unsure, layoutUnsure{Lib: c.lib, Dir: c.dir, Reason: c.unsure, Samples: samples})
			continue
		}
		if _, ok := layout.lookup(c.lib, c.dir); ok {
			continue // 已经登记了（片目没认出来是别的原因）
		}
		if dismissed[k] {
			out.Dismissed++
			continue
		}
		typ := "auto"
		if len(c.types) == 1 {
			for t := range c.types {
				typ = t
			}
		}
		out.Suggestions = append(out.Suggestions, layoutSuggestion{
			Lib: c.lib, Dir: c.dir, Type: typ, Source: "emby", Titles: len(keys), Samples: samples, keys: keys,
		})
	}
	sort.SliceStable(out.Suggestions, func(i, j int) bool {
		a, b := out.Suggestions[i], out.Suggestions[j]
		if a.Titles != b.Titles {
			return a.Titles > b.Titles
		}
		return layoutCatKey(a.Lib, a.Dir) < layoutCatKey(b.Lib, b.Dir)
	})
	sort.SliceStable(out.Unsure, func(i, j int) bool {
		return layoutCatKey(out.Unsure[i].Lib, out.Unsure[i].Dir) < layoutCatKey(out.Unsure[j].Lib, out.Unsure[j].Dir)
	})
	return out
}

// ---- 采纳前预览 ----

// layoutPreview 「当前布局 + 这几条」会怎样（§3.4）
type layoutPreview struct {
	// NewTitles 新认出的片目数；其中 Corroborated 有佐证（Emby 条目对得上或片目里有 NFO），其余没有
	NewTitles       int      `json:"new_titles"`
	Corroborated    int      `json:"corroborated"`
	NewSamples      []string `json:"new_samples"`
	Uncorroborated  []string `json:"uncorroborated"` // 没有佐证的样例：多半是花絮、空壳目录，采纳后会变成「片目」
	UncorroboratedN int      `json:"uncorroborated_n"`
	// Changed 已有片目的 key 会变（新分类比现有的更深）：按片目 key 记的状态（补刮记账、背景图缺口）会作废
	Changed        int      `json:"changed"`
	ChangedSamples []string `json:"changed_samples"`
	// Lost 删掉条目后认不出的片目
	Lost        int      `json:"lost"`
	LostSamples []string `json:"lost_samples"`
	// QueuedScrapes 排着的刮削任务：它们参数里的片目 key 可能对不上新布局
	QueuedScrapes int `json:"queued_scrapes"`
}

const layoutPreviewSampleMax = 10

// previewLayout 纯函数：同一份台账按新旧两张布局各认一遍。
// evidence 是有 Emby 佐证的片目 key；hasNfo 判断片目里有没有 NFO（测试里替换）
func previewLayout(before, after libCategoryLayout, ix layoutLedgerIndex, evidence map[string]bool, hasNfo func(key string) bool) layoutPreview {
	var p layoutPreview
	// 新认出 = 新布局下有、旧布局下没有的片目 key（含 key 变了之后的新 key）；
	// 改 key = 旧片目的视频到了新布局下归到别的 key；认不出 = 旧片目的视频新布局下哪都不归
	oldKeys, nextKeys := map[string]bool{}, map[string]bool{}
	changed, lost := map[string]bool{}, map[string]bool{}
	for _, v := range ix.videos {
		o, okO := before.titleAt(v)
		n, okN := after.titleAt(v)
		if okO {
			oldKeys[o.Key] = true
		}
		if okN {
			nextKeys[n.Key] = true
		}
		switch {
		case okO && !okN:
			lost[o.Key] = true
		case okO && okN && o.Key != n.Key:
			changed[o.Key] = true
		}
	}
	newKeys := map[string]bool{}
	for k := range nextKeys {
		if !oldKeys[k] {
			newKeys[k] = true
		}
	}
	for _, k := range sortedKeys(newKeys) {
		p.NewTitles++
		if len(p.NewSamples) < layoutPreviewSampleMax {
			p.NewSamples = append(p.NewSamples, k)
		}
		if evidence[k] || (hasNfo != nil && hasNfo(k)) {
			p.Corroborated++
			continue
		}
		p.UncorroboratedN++
		if len(p.Uncorroborated) < layoutPreviewSampleMax {
			p.Uncorroborated = append(p.Uncorroborated, k)
		}
	}
	for _, k := range sortedKeys(changed) {
		p.Changed++
		if len(p.ChangedSamples) < layoutPreviewSampleMax {
			p.ChangedSamples = append(p.ChangedSamples, k)
		}
	}
	for _, k := range sortedKeys(lost) {
		p.Lost++
		if len(p.LostSamples) < layoutPreviewSampleMax {
			p.LostSamples = append(p.LostSamples, k)
		}
	}
	return p
}

func sortedKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// localTitleHasNfo 片目在本地有没有 NFO：目录片目看目录第一层的任何 .nfo，平铺片目看同名 .nfo。只读本地
func localTitleHasNfo(root, key string) bool {
	if root == "" {
		return false
	}
	if isSoloTitleKey(key) {
		_, err := os.Stat(filepath.Join(root, filepath.FromSlash(strings.TrimSuffix(key, ".strm")+".nfo")))
		return err == nil
	}
	ents, err := os.ReadDir(filepath.Join(root, filepath.FromSlash(key)))
	if err != nil {
		return false
	}
	for _, e := range ents {
		if !e.IsDir() && strings.EqualFold(filepath.Ext(e.Name()), ".nfo") {
			return true
		}
	}
	return false
}

// ---- 忽略的建议 ----

const layoutDismissedKey = "library_layout_dismissed"

type layoutDirRef struct {
	Lib string `json:"lib"`
	Dir string `json:"dir"`
}

func loadLayoutDismissed() map[string]bool {
	out := map[string]bool{}
	raw := strings.TrimSpace(settingValueCompat(layoutDismissedKey))
	if raw == "" {
		return out
	}
	var refs []layoutDirRef
	if err := json.Unmarshal([]byte(raw), &refs); err != nil {
		log.Printf("[媒体库] ✗ 忽略的布局建议解析失败，按没有处理: %v", err)
		return out
	}
	for _, r := range refs {
		out[layoutCatKey(r.Lib, r.Dir)] = true
	}
	return out
}

func (h *Handler) saveLayoutDismissed(m map[string]bool) error {
	refs := make([]layoutDirRef, 0, len(m))
	for _, k := range sortedKeys(m) {
		lib, dir, _ := strings.Cut(k, "/")
		refs = append(refs, layoutDirRef{Lib: lib, Dir: dir})
	}
	b, _ := json.Marshal(refs)
	return h.Config.SaveSetting(layoutDismissedKey, string(b))
}

// ---- 接口 ----

// 建议要读一遍全服务器的 Movie / Series：一分钟内复用，布局变了作废（forgetLibLayoutCaches）
var layoutSuggestCache struct {
	sync.Mutex
	at  time.Time
	res *layoutSuggestResult
}

const layoutSuggestTTL = time.Minute

func forgetLayoutSuggest() {
	layoutSuggestCache.Lock()
	layoutSuggestCache.res = nil
	layoutSuggestCache.Unlock()
}

// ledgerVideoPaths 台账全部视频的相对路径
func ledgerVideoPaths() []string {
	var rels []string
	if model.DB != nil {
		model.DB.Model(&model.SyncedFile{}).Where("kind = ?", "video").Pluck("rel_path", &rels)
	}
	return rels
}

// layoutSuggestNow 当前的建议（Emby 没配时 configured=false）
func layoutSuggestNow(refresh bool) (res *layoutSuggestResult, configured bool, err error) {
	cfg, ok := loadEmbyRefreshCfg()
	root := strings.TrimRight(filepath.ToSlash(localMediaRoot()), "/")
	if !ok || root == "" {
		return nil, false, nil
	}
	layoutSuggestCache.Lock()
	defer layoutSuggestCache.Unlock()
	if !refresh && layoutSuggestCache.res != nil && time.Since(layoutSuggestCache.at) < layoutSuggestTTL {
		return layoutSuggestCache.res, true, nil
	}
	items, err := fetchEmbyReconItems(cfg)
	if err != nil {
		return nil, true, err
	}
	r := suggestLayoutFromEmby(items, loadLibCategoryLayout(), newLayoutLedgerIndex(ledgerVideoPaths()), root,
		func(p string) string { return embyPathToLocal(cfg.PathMapping, p) }, loadLayoutDismissed())
	layoutSuggestCache.res, layoutSuggestCache.at = &r, time.Now()
	return &r, true, nil
}

// LibraryLayoutSuggest GET /library/layout/suggest?refresh=1
func (h *Handler) LibraryLayoutSuggest(c *gin.Context) {
	res, configured, err := layoutSuggestNow(c.Query("refresh") == "1")
	if !configured {
		c.JSON(http.StatusOK, gin.H{"configured": false})
		return
	}
	if err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": "读取 Emby 条目失败：" + err.Error()})
		return
	}
	out := gin.H{"configured": true, "result": res}
	if !res.MappingOK {
		cfg, _ := loadEmbyRefreshCfg()
		out["mapping"] = embyPathSuggestion(cfg, strings.TrimRight(filepath.ToSlash(localMediaRoot()), "/"))
	}
	c.JSON(http.StatusOK, out)
}

// layoutChangeReq 预览 / 采纳的请求：要加的与要删的条目（删只认其他媒体库目录里已有的，规则改不了）
type layoutChangeReq struct {
	Add    []libExtraDir `json:"add"`
	Remove []libExtraDir `json:"remove"`
	Source string        `json:"source"` // 采纳：emby / record / manual
}

// applyLayoutChange 现有的其他媒体库目录 + 改动 → 新清单（未归一）
func applyLayoutChange(cur []libExtraDir, req layoutChangeReq) []libExtraDir {
	drop := map[string]bool{}
	for _, d := range req.Remove {
		drop[layoutCatKey(strings.TrimSpace(d.Lib), libSubPath(d.Dir))] = true
	}
	out := make([]libExtraDir, 0, len(cur)+len(req.Add))
	for _, d := range cur {
		if !drop[layoutCatKey(d.Lib, libSubPath(d.Dir))] {
			out = append(out, d)
		}
	}
	return append(out, req.Add...)
}

// layoutEvidenceKeys 建议里有 Emby 佐证的片目 key（没配 Emby / 读失败时为空，预览只靠 NFO）
func layoutEvidenceKeys() map[string]bool {
	out := map[string]bool{}
	res, _, err := layoutSuggestNow(false)
	if err != nil || res == nil {
		return out
	}
	for _, s := range res.Suggestions {
		for _, k := range s.keys {
			out[k] = true
		}
	}
	return out
}

// LibraryLayoutPreview POST /library/layout/preview：只算不存
func (h *Handler) LibraryLayoutPreview(c *gin.Context) {
	var req layoutChangeReq
	if err := c.ShouldBindJSON(&req); err != nil || len(req.Add)+len(req.Remove) == 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "参数错误"})
		return
	}
	next, skipped, err := normExtraDirs(applyLayoutChange(loadLibExtraDirs(), req), loadRuleLayout(), ledgerLibNames(), extraDirLibs())
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	root := localMediaRoot()
	p := previewLayout(loadLibCategoryLayout(), loadRuleLayout().withExtraDirs(next), newLayoutLedgerIndex(ledgerVideoPaths()),
		layoutEvidenceKeys(), func(k string) bool { return localTitleHasNfo(root, k) })
	var queued int64
	h.DB.Model(&model.TaskJob{}).Where("kind = ? AND status = ?", "scrape", jobQueued).Count(&queued)
	p.QueuedScrapes = int(queued)
	c.JSON(http.StatusOK, gin.H{"preview": p, "skipped": skipped})
}

// LibraryLayoutAdopt POST /library/layout/adopt：采纳建议 / 手动登记，写进其他媒体库目录
func (h *Handler) LibraryLayoutAdopt(c *gin.Context) {
	var req layoutChangeReq
	if err := c.ShouldBindJSON(&req); err != nil || len(req.Add)+len(req.Remove) == 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "参数错误"})
		return
	}
	src := req.Source
	switch src {
	case "", layoutSrcManual:
		src = ""
	case "emby", "record":
	default:
		c.JSON(http.StatusBadRequest, gin.H{"error": "来源不认识：" + src})
		return
	}
	now := time.Now().Format(time.RFC3339)
	for i := range req.Add {
		req.Add[i].Source, req.Add[i].At = src, now
	}
	out, skipped, err := normExtraDirs(applyLayoutChange(loadLibExtraDirs(), req), loadRuleLayout(), ledgerLibNames(), extraDirLibs())
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	if err := h.saveLibExtraDirs(out); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "保存失败: " + err.Error()})
		return
	}
	// 采纳了就不再算「忽略过」
	if dis := loadLayoutDismissed(); len(dis) > 0 {
		changed := false
		for _, d := range req.Add {
			if k := layoutCatKey(strings.TrimSpace(d.Lib), libSubPath(d.Dir)); dis[k] {
				delete(dis, k)
				changed = true
			}
		}
		if changed {
			_ = h.saveLayoutDismissed(dis)
		}
	}
	log.Printf("[媒体库] ✓ 布局：加入 %d 个、移除 %d 个分类目录（来源 %s），现共 %d 个其他媒体库目录",
		len(req.Add)-len(skipped), len(req.Remove), layoutSrcLabel(src), len(out))
	c.JSON(http.StatusOK, gin.H{"dirs": out, "skipped": skipped})
}

func layoutSrcLabel(src string) string {
	switch src {
	case "emby":
		return "Emby 建议"
	case "record":
		return "整理记录建议"
	}
	return "手动"
}

// LibraryLayoutDismiss POST /library/layout/dismiss {lib, dir, undo}：忽略 / 取消忽略一条建议
func (h *Handler) LibraryLayoutDismiss(c *gin.Context) {
	var req struct {
		Lib  string `json:"lib"`
		Dir  string `json:"dir"`
		Undo bool   `json:"undo"`
	}
	if err := c.ShouldBindJSON(&req); err != nil || strings.TrimSpace(req.Lib) == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "参数错误"})
		return
	}
	dis := loadLayoutDismissed()
	k := layoutCatKey(strings.TrimSpace(req.Lib), libSubPath(req.Dir))
	if req.Undo {
		delete(dis, k)
	} else {
		dis[k] = true
	}
	if err := h.saveLayoutDismissed(dis); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "保存失败: " + err.Error()})
		return
	}
	forgetLayoutSuggest()
	c.JSON(http.StatusOK, gin.H{"ok": true})
}
