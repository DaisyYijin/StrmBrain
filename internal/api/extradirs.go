package api

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"slices"
	"sort"
	"strings"

	"github.com/gin-gonic/gin"
)

// ==================== 其他媒体库目录（只用于识别，2026-10-12） ====================
//
// 海报墙、网盘文件页、对账都靠 libCategoryLayout 判断「哪一层是片目」，而它原来只认二级分类规则。
// 用户自己在媒体库里建的目录（不走整理，手动往里放片：影视/LGNB电影/LGNB中国香港电影）
// 和改分类规则之后留下的旧分类目录，里面的片子台账有、Emby 有，海报墙一部都不认。
//
// 加进分类规则能让它们被认出来，但分类规则同时决定新文件整理到哪 —— 没写条件的规则会把
// 以后整理的片子也吸进去。所以另存一份「其他媒体库目录」：loadLibCategoryLayout 把它当分类目录，
// classifyMedia（读 classifyRules）不读它，整理永远不会往里放新片。
//
// 写法与分类规则的名字相同：库内相对路径、不带库名、可以多级。Lib 为空时每个库下的同名目录都认；
// Lib 非空（2026-10-14 起）只对那个库生效，Dir 还可以为空 = 库根本身就是分类目录（liblayout.go）。
// 和分类规则同名时以规则为准（类型也按规则），保存时就丢掉。
//
// 字段是向后兼容地加的：老版本读到新数据时忽略 lib / source / at，Dir 为空的条目被 libSubPath 丢掉
// （少认），带 lib 的退化成对所有库生效（多库时可能多认）。都只影响「哪些算片目」，整理不读这张表。

// libExtraDirsKey setting key：JSON 数组 [{dir, type, lib?, source?, at?}]
const libExtraDirsKey = "library_extra_dirs"

// libExtraDir 一个其他媒体库目录。Type：movie / tv / auto（按片目里有没有集号判断，同「电影剧集同名分类」）
type libExtraDir struct {
	Dir  string `json:"dir"`
	Type string `json:"type"`
	// Lib 只对这个库（本地媒体树第一层的库目录名）生效；空 = 所有库
	Lib string `json:"lib,omitempty"`
	// Source 怎么来的：空 = 手动（老数据都是）；之后从 Emby / 整理记录的建议采纳的记 emby / record
	Source string `json:"source,omitempty"`
	// At 登记时间（RFC3339），给布局页看
	At string `json:"at,omitempty"`
}

// source 条目来源，空记作手动
func (d libExtraDir) source() string {
	if d.Source == "" {
		return layoutSrcManual
	}
	return d.Source
}

// layoutType layout 里存的类型：auto 记成空串，与同名分类的「按文件判断」同口径
func (d libExtraDir) layoutType() string {
	if d.Type == "movie" || d.Type == "tv" {
		return d.Type
	}
	return ""
}

// loadLibExtraDirs 读配置；解析不了当作没有（不影响分类规则那部分）
func loadLibExtraDirs() []libExtraDir {
	raw := strings.TrimSpace(settingValueCompat(libExtraDirsKey))
	if raw == "" {
		return nil
	}
	var out []libExtraDir
	if err := json.Unmarshal([]byte(raw), &out); err != nil {
		log.Printf("[媒体库] ✗ 其他媒体库目录配置解析失败，按没有处理: %v", err)
		return nil
	}
	return out
}

// withExtraDirs 把其他媒体库目录并进分类目录表。规则为准：和规则同名的不动，
// 限定库的条目也不许盖过规则（lookup 先查限定库的，放进去就等于盖过了）
func (l libCategoryLayout) withExtraDirs(extras []libExtraDir) libCategoryLayout {
	for _, d := range extras {
		dir := libSubPath(d.Dir)
		e := layoutEntry{Type: d.layoutType(), Source: d.source()}
		if d.Lib == "" {
			if dir == "" {
				continue
			}
			if _, ok := l.all[dir]; !ok {
				l.all[dir] = e
			}
			continue
		}
		if prev, ok := l.all[dir]; ok && prev.Source == layoutSrcRule {
			continue
		}
		if l.libs[d.Lib] == nil {
			l.libs[d.Lib] = map[string]layoutEntry{}
		}
		if _, ok := l.libs[d.Lib][dir]; !ok {
			l.libs[d.Lib][dir] = e
		}
	}
	return l
}

// splitExtraPath 反斜杠换正斜杠、去首尾斜杠与空段；带 . / .. 的拒收
func splitExtraPath(dir string) ([]string, error) {
	segs := strings.Split(strings.ReplaceAll(strings.TrimSpace(dir), `\`, "/"), "/")
	parts := make([]string, 0, len(segs))
	for _, s := range segs {
		s = strings.TrimSpace(s)
		switch s {
		case "":
			continue
		case ".", "..":
			return nil, fmt.Errorf("目录「%s」不能包含 . 或 ..", dir)
		}
		parts = append(parts, s)
	}
	return parts, nil
}

// normExtraLib 规范化限定的库名：只能是一段（本地媒体树第一层）；known 非空时必须是其中之一，防手误
func normExtraLib(lib string, known []string) (string, error) {
	parts, err := splitExtraPath(lib)
	if err != nil {
		return "", err
	}
	if len(parts) != 1 {
		return "", fmt.Errorf("库目录名「%s」只能是一层目录名", lib)
	}
	if len(known) > 0 && !slices.Contains(known, parts[0]) {
		return "", fmt.Errorf("认不出库目录「%s」（台账里没有这个库）", parts[0])
	}
	return parts[0], nil
}

// normExtraDir 规范化一个目录：反斜杠换正斜杠、去首尾斜杠与空段；带 . / .. 的拒收。
// 不限定库时，第一段是库名就剥掉（用户常从对账或文件页抄带库名的路径；不剥的话 titleOf 按「不带库名」那一轮
// 也能认出片目，但片目的库名就丢了，换算网盘路径会错）；限定了库的，dir 本来就是库内路径，可以为空（库根）
func normExtraDir(dir, lib string, libs []string) (string, error) {
	parts, err := splitExtraPath(dir)
	if err != nil {
		return "", err
	}
	if lib != "" {
		return strings.Join(parts, "/"), nil
	}
	if len(parts) > 1 {
		for _, lib := range libs {
			if parts[0] == lib {
				parts = parts[1:]
				break
			}
		}
	}
	if len(parts) == 0 {
		return "", fmt.Errorf("目录不能为空，也不能是媒体库根")
	}
	return strings.Join(parts, "/"), nil
}

// normExtraDirs 规范化整份清单：去重（同一个库 + 目录，后来的覆盖类型）、丢掉和分类规则同名的（skipped 说明原因）。
// libs 用来剥掉不限定库的目录前面误带的库名；tops 是台账里实际有的库名，限定库时据此校验（空 = 不校验）
func normExtraDirs(in []libExtraDir, rules libCategoryLayout, libs, tops []string) (out []libExtraDir, skipped []string, err error) {
	idx := map[string]int{}
	for _, d := range in {
		lib := ""
		if strings.TrimSpace(d.Lib) != "" {
			if lib, err = normExtraLib(d.Lib, tops); err != nil {
				return nil, nil, err
			}
		}
		dir, e := normExtraDir(d.Dir, lib, libs)
		if e != nil {
			return nil, nil, e
		}
		typ := d.Type
		if typ != "movie" && typ != "tv" {
			typ = "auto"
		}
		if _, ok := rules.lookup("", dir); ok {
			skipped = append(skipped, fmt.Sprintf("「%s」已经是分类规则里的分类，不需要再加", dir))
			continue
		}
		k := lib + "/" + dir // 库名不含 /，拼起来不会撞
		if i, ok := idx[k]; ok {
			out[i].Type = typ
			continue
		}
		idx[k] = len(out)
		out = append(out, libExtraDir{Dir: dir, Type: typ, Lib: lib, Source: d.Source, At: d.At})
	}
	return out, skipped, nil
}

// forgetLibLayoutCaches 分类目录变了（分类规则 / 其他媒体库目录）：台账聚合与海报墙快照都作废，
// 否则改完要等半分钟才看得到；海报墙的 Emby 快照也按片目 key 归，一并标过期
func forgetLibLayoutCaches() {
	forgetLedgerTitles()
	forgetLocalTitles()
	staleLocalEmby()
}

// extraDirLibs 限定库的条目可以写哪些库名：台账里有的，加上已经登记着的。
// 后者是为了库从台账里消失（移出配置、清理了残留）之后，追加别的条目时整份清单不至于因为旧条目校验不过而存不进去
func extraDirLibs() []string {
	out := ledgerTopNames()
	for _, d := range loadLibExtraDirs() {
		if d.Lib != "" && !slices.Contains(out, d.Lib) {
			out = append(out, d.Lib)
		}
	}
	return out
}

// saveLibExtraDirs 落盘并清缓存
func (h *Handler) saveLibExtraDirs(dirs []libExtraDir) error {
	if dirs == nil {
		dirs = []libExtraDir{}
	}
	b, _ := json.Marshal(dirs)
	if err := h.Config.SaveSetting(libExtraDirsKey, string(b)); err != nil {
		return err
	}
	forgetLibLayoutCaches()
	return nil
}

// addLibExtraDirs 往现有清单里追加（对账一键加入、删分类规则时保留都走它）
func (h *Handler) addLibExtraDirs(add []libExtraDir) (out []libExtraDir, skipped []string, err error) {
	all := append(append([]libExtraDir{}, loadLibExtraDirs()...), add...)
	out, skipped, err = normExtraDirs(all, loadRuleLayout(), ledgerLibNames(), extraDirLibs())
	if err != nil {
		return nil, nil, err
	}
	if err = h.saveLibExtraDirs(out); err != nil {
		return nil, nil, err
	}
	return out, skipped, nil
}

// GetLibExtraDirs GET /library/extra-dirs
func (h *Handler) GetLibExtraDirs(c *gin.Context) {
	dirs := loadLibExtraDirs()
	if dirs == nil {
		dirs = []libExtraDir{}
	}
	c.JSON(http.StatusOK, gin.H{"dirs": dirs, "libs": ledgerLibNames()})
}

// SaveLibExtraDirs POST /library/extra-dirs：整份替换
func (h *Handler) SaveLibExtraDirs(c *gin.Context) {
	var req struct {
		Dirs []libExtraDir `json:"dirs"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "参数错误"})
		return
	}
	out, skipped, err := normExtraDirs(req.Dirs, loadRuleLayout(), ledgerLibNames(), extraDirLibs())
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	if err := h.saveLibExtraDirs(out); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "保存失败: " + err.Error()})
		return
	}
	log.Printf("[媒体库] ✓ 其他媒体库目录已保存：%d 个", len(out))
	c.JSON(http.StatusOK, gin.H{"dirs": out, "skipped": skipped})
}

// AddLibExtraDirs POST /library/extra-dirs/add：追加
func (h *Handler) AddLibExtraDirs(c *gin.Context) {
	var req struct {
		Dirs []libExtraDir `json:"dirs"`
	}
	if err := c.ShouldBindJSON(&req); err != nil || len(req.Dirs) == 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "参数错误"})
		return
	}
	out, skipped, err := h.addLibExtraDirs(req.Dirs)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	log.Printf("[媒体库] ✓ 其他媒体库目录加入 %d 个，现共 %d 个", len(req.Dirs)-len(skipped), len(out))
	c.JSON(http.StatusOK, gin.H{"dirs": out, "skipped": skipped})
}

// ---- 删分类规则时的提示 ----

// categoryDrop 新的分类规则里没有了、底下还有片目的分类目录
type categoryDrop struct {
	Dir    string   `json:"dir"`
	Type   string   `json:"type"` // movie / tv / auto
	Titles int      `json:"titles"`
	Sample []string `json:"samples"`
}

// categoryDrops 纯函数：旧分类目录表 → 新规则 + 其他媒体库目录合成的表，哪些分类目录消失了、各有几部片。
// ledger 是按旧布局聚合的片目（Category 就是旧的分类目录）
func categoryDrops(oldLayout, newLayout libCategoryLayout, ledger map[string]*ledgerTitleEntry) []categoryDrop {
	byDir := map[string]*categoryDrop{}
	keys := make([]string, 0, len(ledger))
	for k := range ledger {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		e := ledger[k]
		if _, still := newLayout.lookup(e.LibName, e.Category); still {
			continue
		}
		old, was := oldLayout.lookup(e.LibName, e.Category)
		if !was {
			continue // 其他媒体库目录里的片，不归这次规则修改管
		}
		mt := old.Type
		d := byDir[e.Category]
		if d == nil {
			typ := mt
			if typ == "" {
				typ = "auto"
			}
			d = &categoryDrop{Dir: e.Category, Type: typ}
			byDir[e.Category] = d
		}
		d.Titles++
		if len(d.Sample) < 3 {
			d.Sample = append(d.Sample, e.Title)
		}
	}
	out := make([]categoryDrop, 0, len(byDir))
	for _, d := range byDir {
		out = append(out, *d)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Titles != out[j].Titles {
			return out[i].Titles > out[j].Titles
		}
		return out[i].Dir < out[j].Dir
	})
	return out
}

// PreviewCategories POST /scrape/categories/preview：保存分类规则之前，看看会有哪些分类目录连同里面的片
// 从海报墙上消失，界面据此问用户要不要留作其他媒体库目录
func (h *Handler) PreviewCategories(c *gin.Context) {
	var req struct {
		Yaml string `json:"yaml"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "参数错误"})
		return
	}
	rows, err := parseCategoryYAML(req.Yaml)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "YAML 解析失败: " + err.Error()})
		return
	}
	newLayout := buildLibCategoryLayout(rows).withExtraDirs(loadLibExtraDirs())
	c.JSON(http.StatusOK, gin.H{"dropped": categoryDrops(loadRuleLayout(), newLayout, scanLedgerTitles())})
}
