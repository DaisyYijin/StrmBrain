package api

import (
	"net/http"
	"sort"
	"strings"

	"115-station/internal/model"

	"github.com/gin-gonic/gin"
)

// ==================== 媒体库布局总表（布局页，LIBRARY-LAYOUT-PLAN.md §3.7 / §6 第 5 条） ====================
//
// 把编译好的布局逐条列出来：哪个库、哪个目录、什么类型、从哪来（分类规则 / 手动 / Emby 建议 / 整理记录建议）、
// 底下认出几部片目。分类规则来的只读（改规则去自动整理页），其余在布局页增删。
//
// 规则条目另带两种提示（只提示，不改识别）：首次启动写入的默认分类同样进布局，
// 不用自动整理的用户自己恰好有同名目录、结构却不同时会被按规则误认 ——
//   - empty：台账里一部片目都没有（多数默认分类对不用整理的用户都是这样），界面灰显、折到末尾；
//   - never_organized：有片目，但没有任何一条成功的整理记录落在它下面（片是用户自己放的），提醒核对结构。

// layoutRow 布局总表的一行
type layoutRow struct {
	Lib    string `json:"lib"` // 空 = 对所有库生效
	Dir    string `json:"dir"` // 空 = 库根本身（只有 Lib 非空时出现）
	Type   string `json:"type"`
	Source string `json:"source"`
	At     string `json:"at,omitempty"`
	Titles int    `json:"titles"`
	Hint   string `json:"hint,omitempty"` // empty / never_organized（只给规则条目）
}

// resolve 库 lib 里的 dir 命中的是哪一条：返回条目的作用范围（限定库时是库名，对所有库生效时为空）
func (l libCategoryLayout) resolve(lib, dir string) (scope string, ok bool) {
	if lib != "" {
		if _, hit := l.libs[lib][dir]; hit {
			return lib, true
		}
	}
	if dir == "" {
		return "", false
	}
	_, hit := l.all[dir]
	return "", hit
}

// buildLayoutRows 纯函数：布局 × 台账片目 × 整理记录落点 → 总表。
// extras 用来补登记时间；organized 是成功整理记录的 TargetDir（库内路径）
func buildLayoutRows(layout libCategoryLayout, extras []libExtraDir, titles map[string]*ledgerTitleEntry, organized []string) []layoutRow {
	counts := map[string]int{}
	for _, e := range titles {
		if scope, ok := layout.resolve(e.LibName, e.Category); ok {
			counts[layoutCatKey(scope, e.Category)]++
		}
	}
	at := map[string]string{}
	for _, d := range extras {
		at[layoutCatKey(d.Lib, libSubPath(d.Dir))] = d.At
	}
	typ := func(t string) string {
		if t == "" {
			return "auto"
		}
		return t
	}
	var rows []layoutRow
	for dir, e := range layout.all {
		rows = append(rows, layoutRow{Dir: dir, Type: typ(e.Type), Source: e.Source, At: at[layoutCatKey("", dir)], Titles: counts[layoutCatKey("", dir)]})
	}
	for lib, m := range layout.libs {
		for dir, e := range m {
			rows = append(rows, layoutRow{Lib: lib, Dir: dir, Type: typ(e.Type), Source: e.Source, At: at[layoutCatKey(lib, dir)], Titles: counts[layoutCatKey(lib, dir)]})
		}
	}
	for i := range rows {
		r := &rows[i]
		if r.Source != layoutSrcRule {
			continue
		}
		if r.Titles == 0 {
			r.Hint = "empty"
			continue
		}
		hit := false
		for _, t := range organized {
			if strings.HasPrefix(t, r.Dir+"/") {
				hit = true
				break
			}
		}
		if !hit {
			r.Hint = "never_organized"
		}
	}
	// 有片目的在前（按库、目录），台账里没有片的规则目录折到末尾
	sort.Slice(rows, func(i, j int) bool {
		a, b := rows[i], rows[j]
		if (a.Hint == "empty") != (b.Hint == "empty") {
			return b.Hint == "empty"
		}
		if a.Lib != b.Lib {
			return a.Lib < b.Lib
		}
		return a.Dir < b.Dir
	})
	return rows
}

// LibraryLayout GET /library/layout：布局总表 + 认不出片目的视频 + 可选的库名
func (h *Handler) LibraryLayout(c *gin.Context) {
	var organized []string
	h.DB.Model(&model.OrganizeRecord{}).Where("status = ? AND target_dir <> ''", "success").Distinct().Pluck("target_dir", &organized)
	titles, unclaimed := scanLedgerCached(c.Query("refresh") == "1")
	rows := buildLayoutRows(loadLibCategoryLayout(), loadLibExtraDirs(), titles, organized)
	c.JSON(http.StatusOK, gin.H{
		"rows": rows, "libs": ledgerTopNames(), "unclaimed": unclaimed, "titles": len(titles),
	})
}
