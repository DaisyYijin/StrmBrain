package api

import (
	"fmt"
	"sort"
	"strings"

	"115-station/internal/model"
)

// ==================== 媒体库目录表（2026-10-14，设计见 docs/115-station-notes/LIBRARY-DIRS-PLAN.md） ====================
//
// 一张表取代三样东西：二级分类规则（CategoryRule）、其他媒体库目录（setting library_extra_dirs）、
// 规则里的 library 键。每行 = 一个 115 目录 + 适用类型 + 条件 + 是否参与整理 + 是否兜底，行的顺序就是优先级。
// 识别只看表里有哪些目录（片目在它下一层，编译成 libCategoryLayout）；整理按顺序找第一个类型与条件都匹配的行。
//
// 阶段 1（本文件）只有编译与迁移规划，**还没接上**：识别与整理仍读老的两份配置。
// 每次提交 master 都会发 latest 镜像，半截切过去的话，用户在老界面改的规则新表看不到。
// 阶段 3 和新界面一起启用（迁移 + 切识别 + 切整理），见设计文档 §7。

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
func planLibraryDirMigration(rules []model.CategoryRule, extras []libExtraDir, roots []libRoot, videos []string) (libDirMigration, error) {
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
	add := func(r model.LibraryDir) {
		r.Priority = len(out.Rows) + 1
		out.Rows = append(out.Rows, r)
		have[key{r.LibName, r.Path}] = true
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
			add(row)
		}
		if !hasDefault {
			// 老 classifyTarget 的最后一档：没有兜底规则时落「类型/未分类」，进主库
			row := model.LibraryDir{
				Path: libSubPath(mediaTypeCategory(mt), "未分类"), MediaType: mt, Organize: true, Fallback: true,
				LibCid: main.Cid, LibName: main.Name, Note: "迁移时补的兜底（原来没有兜底规则时落这里）",
			}
			organized = append(organized, row)
			add(row)
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
