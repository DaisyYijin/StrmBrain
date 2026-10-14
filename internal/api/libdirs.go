package api

import (
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	"115-station/internal/model"
)

// ==================== 媒体库目录表（2026-10-14，设计见 docs/115-station-notes/LIBRARY-DIRS-PLAN.md） ====================
//
// 一张表取代三样东西：二级分类规则（CategoryRule）、其他媒体库目录（setting library_extra_dirs）、
// 规则里的 library 键。每行 = 一个 115 目录 + 适用类型 + 条件 + 是否参与整理 + 是否兜底，行的顺序就是优先级。
// 识别只看表里有哪些目录（片目在它下一层，编译成 libCategoryLayout）；整理按顺序找第一个类型与条件都匹配的行。
//
// 启用前（setting library_dirs_active 不是 "1"）识别与整理都读老的两份配置，这里的代码只被测试用到：
// 每次提交 master 都会发 latest 镜像，半截切过去的话，用户在老界面改的规则新表看不到。
// 阶段 3 和新界面一起启用（迁移 + 打开关），见设计文档 §7。分派点：pickOrgTarget / classifyMedia / categoryDir /
// orgTargetOf / loadLibCategoryLayout。

// libDirTypeAll 对电影、剧集都适用
const libDirTypeAll = "all"

// mergeLayoutType 同一个目录有几行时的类型：一致取它，不一致或有 all 记空（按片目内容判断，同老的同名分类）
func mergeLayoutType(prev layoutEntry, seen bool, t string) string {
	if t == libDirTypeAll {
		t = ""
	}
	if seen && prev.Type != t {
		return ""
	}
	return t
}

// compileLibraryDirs 目录表 → 识别用的分类目录表。libNames 是已配置的各库目录名（一行都没有的库也要在：
// 它决定哪些第一段算「库」，不是库的才按老台账查 legacy）。
// 每行按库名编进 libs；库名认不出（LibName 为空）的行不参与识别。
// 有路径的行另编一份进 legacy，给老台账路径用（liblayout.go 的 legacy，设计文档 §4 R5）
func compileLibraryDirs(rows []model.LibraryDir, libNames []string) libCategoryLayout {
	l := newLibLayout()
	for _, n := range libNames {
		if n != "" {
			l.libs[n] = map[string]layoutEntry{}
		}
	}
	for _, r := range rows {
		if r.LibName == "" {
			continue
		}
		path := libSubPath(r.Path)
		m := l.libs[r.LibName]
		if m == nil {
			m = map[string]layoutEntry{}
			l.libs[r.LibName] = m
		}
		prev, seen := m[path]
		m[path] = layoutEntry{Type: mergeLayoutType(prev, seen, r.MediaType), Source: libDirSource(r)}
		if path != "" {
			prev, seen = l.legacy[path]
			l.legacy[path] = layoutEntry{Type: mergeLayoutType(prev, seen, r.MediaType), Source: libDirSource(r)}
		}
	}
	return l
}

func libDirSource(r model.LibraryDir) string {
	if r.Organize {
		return layoutSrcRule
	}
	return layoutSrcManual
}

// ---- 迁移规划（纯函数）----

// libDirMigration 迁移结果：新表的行 + 给人看的说明（认不出的库等）
type libDirMigration struct {
	Rows  []model.LibraryDir
	Notes []string
}

// planLibraryDirMigration 老配置 → 目录表。roots 是已配置的媒体库（第一个是主库，名字都要有）；
// videos 是台账全部视频的相对路径（第一段是库名，老台账可能不是），用来决定识别要展开到哪些库。
//
// 要逐条等价的两件事（设计文档 §6 G1 / G2）：
//   - 整理：老的 classifyTarget 按类型各自 priority 先命中先停，再取第一个 is_default，再落「电影|剧集/未分类」。
//     新表电影组在前、剧集组在后（类型互斥，交错无影响），is_default → Fallback，
//     没有 is_default 的类型在组末加一行「类型/未分类」兜底；分类名为空的规则落 电影 / 剧集（老 categoryDir 的退回）。
//   - 识别：老布局里规则与不限定库的其他目录对**所有库**生效。新表按库，所以凡是台账里某个库在这个路径下有视频的，
//     那个库补一行「仅识别」—— 对现有台账的识别结果不变；以后才在别的库出现的同名目录要用户自己加（海报墙会提示认不出）。
//   - 来源定库：老逻辑是「按全局规则算分类路径，库换成来源绑定的库」；新表来源定库时只在那个库的行里挑。
//     bound 是来源绑定过的库（cid：转存子目录绑定表 + 按来源入库过的整理记录），每一行参与整理的行都在它后面
//     紧跟一份复制到这些库的副本：只看库 L 的行时，顺序与条件和全局完全对应，第一个命中的正是老逻辑命中那条的同路径副本；
//     不看来源时，原行总排在副本前面、条件相同，副本永远轮不到，所以不改变非来源的结果。
func planLibraryDirMigration(rules []model.CategoryRule, extras []libExtraDir, roots []libRoot, videos []string, bound []string) (libDirMigration, error) {
	var out libDirMigration
	if len(roots) == 0 || roots[0].Cid == "" {
		return out, fmt.Errorf("未配置媒体库目录")
	}
	for _, r := range roots {
		if r.Name == "" {
			return out, fmt.Errorf("认不出媒体库 cid=%s 的目录名（115 暂时不可用？）", r.Cid)
		}
	}
	main := roots[0]

	// 每个库下有视频的目录（含各级上层），判断要不要给这个库补识别行
	hasVideo := map[string]map[string]bool{} // 库名 → 库内目录
	libByName := map[string]libRoot{}
	for _, r := range roots {
		hasVideo[r.Name] = map[string]bool{}
		libByName[r.Name] = r
	}
	for _, v := range videos {
		lib, rest := splitLibRel(v)
		m := hasVideo[lib]
		if m == nil {
			continue // 不是配置中的库（老台账、移出配置的库残留）：老台账靠 legacy 识别，不必展开
		}
		for d := layoutParent(rest); d != ""; d = layoutParent(d) {
			m[d] = true
		}
	}

	type key struct{ lib, path string }
	have := map[key]bool{}
	identical := map[string]bool{}
	identKey := func(r model.LibraryDir) string {
		return strings.Join([]string{r.LibCid, r.Path, r.MediaType, fmt.Sprint(r.Organize, r.Fallback), r.GenreIds, r.OriginalLanguage,
			r.OriginCountry, r.ProductionCountries, r.ReleaseYear, r.CustomRegex, r.Unsupported}, "\x1f")
	}
	add := func(r model.LibraryDir) {
		r.Priority = len(out.Rows) + 1
		out.Rows = append(out.Rows, r)
		have[key{r.LibName, r.Path}] = true
		identical[identKey(r)] = true
	}
	// addOrganized 参与整理的行，后面紧跟复制到各来源绑定库的副本
	var boundRoots []libRoot
	for _, cid := range bound {
		if name := rootName(roots, cid); name != "" {
			boundRoots = append(boundRoots, libRoot{Cid: cid, Name: name})
		} else {
			out.Notes = append(out.Notes, fmt.Sprintf("来源绑定的媒体库 cid=%s 不在媒体库配置里：没有为它复制目录", cid))
		}
	}
	addOrganized := func(r model.LibraryDir) {
		add(r)
		for _, b := range boundRoots {
			if b.Cid == r.LibCid {
				continue
			}
			c := r
			c.LibCid, c.LibName = b.Cid, b.Name
			c.Note = "迁移时为来源绑定的库复制（转存子目录绑定了这个库时，只在它的目录里挑）"
			if !identical[identKey(c)] {
				add(c)
			}
		}
	}

	// 1. 分类规则：参与整理的行
	byType := map[string][]model.CategoryRule{}
	for _, r := range rules {
		if r.MediaType == "movie" || r.MediaType == "tv" {
			byType[r.MediaType] = append(byType[r.MediaType], r)
		}
	}
	var organized []model.LibraryDir
	for _, mt := range []string{"movie", "tv"} {
		rs := byType[mt]
		sort.SliceStable(rs, func(i, j int) bool {
			if rs[i].Priority != rs[j].Priority {
				return rs[i].Priority < rs[j].Priority
			}
			return rs[i].ID < rs[j].ID
		})
		hasDefault := false
		for _, r := range rs {
			row := model.LibraryDir{
				Path: libSubPath(r.Name), MediaType: mt, Organize: true, Fallback: r.IsDefault,
				GenreIds: r.GenreIds, OriginalLanguage: r.OriginalLanguage, OriginCountry: r.OriginCountry,
				ProductionCountries: r.ProductionCountries, ReleaseYear: r.ReleaseYear, CustomRegex: r.CustomRegex,
				Unsupported: r.Unsupported,
			}
			if row.Path == "" {
				row.Path = mediaTypeCategory(mt) // 老 categoryDir：分类名为空退回媒体类型目录
			}
			// 老规则的 ext 只让规则不匹配任何条目（matchCategory），照这个意思记成认不出的条件
			if strings.TrimSpace(r.Ext) != "" && r.CustomRegex == "" && !hasCond(row) {
				row.Unsupported = joinCSV(row.Unsupported, "ext")
			}
			lib := strings.TrimSpace(r.Library)
			if cid, err := pickRuleLib(lib, roots); err == nil {
				row.LibCid = cid
				row.LibName = rootName(roots, cid)
			} else {
				// 老规则选中它时整理报错停下；新表照样：库名留空，阶段 2 的 pickDir 选中它时报错
				row.LibCid = lib
				out.Notes = append(out.Notes, fmt.Sprintf("分类「%s」指定的媒体库「%s」认不出：%v", row.Path, lib, err))
			}
			hasDefault = hasDefault || r.IsDefault
			organized = append(organized, row)
			addOrganized(row)
		}
		if !hasDefault {
			// 老 classifyTarget 的最后一档：没有兜底规则时落「类型/未分类」，进主库
			row := model.LibraryDir{
				Path: libSubPath(mediaTypeCategory(mt), "未分类"), MediaType: mt, Organize: true, Fallback: true,
				LibCid: main.Cid, LibName: main.Name, Note: "迁移时补的兜底（原来没有兜底规则时落这里）",
			}
			organized = append(organized, row)
			addOrganized(row)
		}
	}

	// 2. 规则目录在别的库里也有片：补仅识别的行（老布局里规则对所有库生效）
	for _, r := range organized {
		for _, root := range roots {
			if root.Name == r.LibName || !hasVideo[root.Name][r.Path] || have[key{root.Name, r.Path}] {
				continue
			}
			add(model.LibraryDir{Path: r.Path, MediaType: r.MediaType, LibCid: root.Cid, LibName: root.Name,
				Note: "迁移时补的识别行（这个库里也有这个分类目录）"})
		}
	}

	// 3. 其他媒体库目录：仅识别
	for _, d := range extras {
		mt := d.Type
		if mt != "movie" && mt != "tv" {
			mt = libDirTypeAll
		}
		path := libSubPath(d.Dir)
		row := model.LibraryDir{Path: path, MediaType: mt}
		if d.Lib != "" {
			// 限定库的（阶段 1 起才有）：原样，库名认不出也按名字留着（识别按名字编译）
			row.LibName = d.Lib
			if r, ok := libByName[d.Lib]; ok {
				row.LibCid = r.Cid
			}
			if !have[key{row.LibName, path}] {
				add(row)
			}
			continue
		}
		if path == "" {
			continue
		}
		placed := false
		for _, root := range roots {
			if !hasVideo[root.Name][path] {
				continue
			}
			placed = true
			if !have[key{root.Name, path}] {
				r := row
				r.LibCid, r.LibName = root.Cid, root.Name
				add(r)
			}
		}
		if !placed && !have[key{main.Name, path}] {
			// 哪个库里都还没片：放主库（老台账两段式的片也靠这一行进 legacy 认出来）
			row.LibCid, row.LibName = main.Cid, main.Name
			add(row)
		}
	}
	return out, nil
}

// hasCond 行上写了任何匹配条件（不含正则）
func hasCond(r model.LibraryDir) bool {
	for _, v := range []string{r.GenreIds, r.OriginalLanguage, r.OriginCountry, r.ProductionCountries, r.ReleaseYear} {
		if strings.TrimSpace(v) != "" {
			return true
		}
	}
	return false
}

func joinCSV(a, b string) string {
	if a == "" {
		return b
	}
	return a + "," + b
}

func rootName(roots []libRoot, cid string) string {
	for _, r := range roots {
		if r.Cid == cid {
			return r.Name
		}
	}
	return ""
}

// ---- 整理：选目录（阶段 2，随 libraryDirsActive 启用）----

// matchLibraryDir 行的条件是否命中条目：与老分类规则同一套语义（matchCategory），没有 ext
func matchLibraryDir(r *model.LibraryDir, media *TmdbMedia) bool {
	return matchCategory(&model.CategoryRule{
		GenreIds: r.GenreIds, OriginalLanguage: r.OriginalLanguage, OriginCountry: r.OriginCountry,
		ProductionCountries: r.ProductionCountries, ReleaseYear: r.ReleaseYear, CustomRegex: r.CustomRegex,
		Unsupported: r.Unsupported,
	}, media)
}

// libDirTypeOK 行的类型对这个媒体类型适用
func libDirTypeOK(r *model.LibraryDir, mediaType string) bool {
	return r.MediaType == mediaType || r.MediaType == libDirTypeAll
}

// pickDir 按目录表选整理去处（设计文档 §3.3）：
//  1. 候选 = 参与整理、类型相符的行；src 非空（来源子目录绑定了库）时只看那个库的行；
//  2. 按顺序第一个条件命中的（兜底行同样先按自己的条件参与）；
//  3. 都没中：按顺序第一个兜底行；
//  4. 还没有：报错，不落表外的目录（§4 R2 / R3）。
//
// 选中的行库认不出（迁移时 library 写的库已不在配置里）也报错停下，同老规则（pickRuleLib）
func pickDir(rows []model.LibraryDir, media *TmdbMedia, src string) (model.LibraryDir, error) {
	mt := "movie"
	if media.MediaType == "tv" {
		mt = "tv"
	}
	var cands []*model.LibraryDir
	for i := range rows {
		r := &rows[i]
		if r.Organize && libDirTypeOK(r, mt) && (src == "" || r.LibCid == src) {
			cands = append(cands, r)
		}
	}
	sort.SliceStable(cands, func(i, j int) bool { return cands[i].Priority < cands[j].Priority })
	var hit *model.LibraryDir
	for _, r := range cands {
		if matchLibraryDir(r, media) {
			hit = r
			break
		}
	}
	if hit == nil {
		for _, r := range cands {
			if r.Fallback {
				hit = r
				break
			}
		}
	}
	if hit == nil {
		where := "媒体库目录表里"
		if src != "" {
			where = "来源绑定的媒体库里"
		}
		return model.LibraryDir{}, fmt.Errorf("%s没有能放%s的目录（也没有兜底目录）：到「账号与媒体库 → 媒体库目录」添加", where, mediaTypeLabel(mt))
	}
	if hit.LibName == "" {
		return *hit, fmt.Errorf("目录「%s」所属的媒体库「%s」不在媒体库配置里（到「账号与媒体库」添加附加媒体库，或改这一行的媒体库）", hit.Path, hit.LibCid)
	}
	return *hit, nil
}

func mediaTypeLabel(mt string) string {
	if mt == "tv" {
		return "剧集"
	}
	return "电影"
}

// ---- 保存校验（阶段 3 的保存接口用）----

// libDirCheck 校验结果：Errors 不让存，Warnings 只提示
type libDirCheck struct {
	Errors   []string `json:"errors"`
	Warnings []string `json:"warnings"`
}

// libDirLabel 给人看的一行：「库 / 路径」，库根写「库（库根）」
func libDirLabel(r *model.LibraryDir) string {
	lib := r.LibName
	if lib == "" {
		lib = r.LibCid
	}
	if r.Path == "" {
		return lib + "（库根）"
	}
	return lib + " / " + r.Path
}

// validateLibraryDirs 纯函数（设计文档 §4 R2 / R9 / R12）：
//   - 电影、剧集各至少一个参与整理、库认得出的兜底行；
//   - 每个来源绑定的库（bound，cid）对电影、剧集各有自己的兜底行（来源定库时只在那个库里挑）；
//   - 参与整理的行要选库；
//   - 完全相同的行（库、路径、类型、开关、条件都一样）只能有一行；
//
// 提示：没写任何条件、又不是兜底的参与整理行，会吞掉排在它后面同类型的行；库认不出的行。
func validateLibraryDirs(rows []model.LibraryDir, bound []string) libDirCheck {
	out := libDirCheck{Errors: []string{}, Warnings: []string{}}
	sorted := append([]model.LibraryDir(nil), rows...)
	sort.SliceStable(sorted, func(i, j int) bool { return sorted[i].Priority < sorted[j].Priority })

	hasFallback := func(mt, lib string) bool {
		for i := range sorted {
			r := &sorted[i]
			if r.Organize && r.Fallback && r.LibName != "" && libDirTypeOK(r, mt) && (lib == "" || r.LibCid == lib) {
				return true
			}
		}
		return false
	}
	for _, mt := range []string{"movie", "tv"} {
		if !hasFallback(mt, "") {
			out.Errors = append(out.Errors, fmt.Sprintf("没有能放%s的兜底目录：条件都不匹配的%s会无处可放", mediaTypeLabel(mt), mediaTypeLabel(mt)))
		}
		for _, lib := range bound {
			if !hasFallback(mt, lib) {
				out.Errors = append(out.Errors, fmt.Sprintf("媒体库 cid=%s 绑定了转存子目录，但没有能放%s的兜底目录", lib, mediaTypeLabel(mt)))
			}
		}
	}

	seen := map[string]int{}
	for i := range sorted {
		r := &sorted[i]
		if r.Organize && strings.TrimSpace(r.LibCid) == "" {
			out.Errors = append(out.Errors, fmt.Sprintf("第 %d 行「%s」参与整理但没选媒体库", i+1, r.Path))
		}
		k := strings.Join([]string{r.LibCid, libSubPath(r.Path), r.MediaType, fmt.Sprint(r.Organize, r.Fallback),
			r.GenreIds, r.OriginalLanguage, r.OriginCountry, r.ProductionCountries, r.ReleaseYear, r.CustomRegex, r.Unsupported}, "\x1f")
		if j, dup := seen[k]; dup {
			out.Errors = append(out.Errors, fmt.Sprintf("第 %d 行和第 %d 行「%s」完全相同", j+1, i+1, libDirLabel(r)))
		} else {
			seen[k] = i
		}
		if r.LibName == "" && r.LibCid != "" {
			out.Warnings = append(out.Warnings, fmt.Sprintf("第 %d 行「%s」所属的媒体库不在配置里：不参与识别，整理选中它时会报错", i+1, libDirLabel(r)))
		}
		if r.Organize && !r.Fallback && r.CustomRegex == "" && r.Unsupported == "" && !hasCond(*r) {
			later := 0
			for j := i + 1; j < len(sorted); j++ {
				o := &sorted[j]
				if o.Organize && (o.MediaType == r.MediaType || o.MediaType == libDirTypeAll || r.MediaType == libDirTypeAll) {
					later++
				}
			}
			if later > 0 {
				out.Warnings = append(out.Warnings, fmt.Sprintf("第 %d 行「%s」没写条件，排在它后面的 %d 行同类型目录永远轮不到", i+1, libDirLabel(r), later))
			}
		}
	}
	return out
}

// ---- 启用开关与缓存 ----

// libraryDirsActiveKey setting：值为 "1" 时识别与整理都用目录表。阶段 3 迁移成功后才写，之前一律走老配置
const libraryDirsActiveKey = "library_dirs_active"

// 目录表缓存：整理循环每个条目都要查一次（同老 classifyRules 的 10 秒）。改表时 forgetLibraryDirs
var libDirsCache struct {
	sync.Mutex
	at     time.Time
	active bool
	rows   []model.LibraryDir
}

const libDirsTTL = 10 * time.Second

// libraryDirsState 是否启用了目录表、以及当前的行（按 Priority 排好；调用方只读）
func libraryDirsState() (bool, []model.LibraryDir) {
	libDirsCache.Lock()
	defer libDirsCache.Unlock()
	if !libDirsCache.at.IsZero() && time.Since(libDirsCache.at) < libDirsTTL {
		return libDirsCache.active, libDirsCache.rows
	}
	active := strings.TrimSpace(settingValueCompat(libraryDirsActiveKey)) == "1"
	var rows []model.LibraryDir
	if active && model.DB != nil {
		model.DB.Order("priority ASC, id ASC").Find(&rows)
	}
	libDirsCache.active, libDirsCache.rows, libDirsCache.at = active, rows, time.Now()
	return active, rows
}

func libraryDirsActive() bool {
	active, _ := libraryDirsState()
	return active
}

// forgetLibraryDirs 目录表或开关变了：整理与识别的缓存都作废
func forgetLibraryDirs() {
	libDirsCache.Lock()
	libDirsCache.at = time.Time{}
	libDirsCache.Unlock()
	forgetLibLayoutCaches()
}

// libDirLibNames 目录表里出现的库名（编译识别用：哪些第一段算库）
func libDirLibNames(rows []model.LibraryDir) []string {
	seen := map[string]bool{}
	var out []string
	for _, r := range rows {
		if r.LibName != "" && !seen[r.LibName] {
			seen[r.LibName] = true
			out = append(out, r.LibName)
		}
	}
	return out
}
