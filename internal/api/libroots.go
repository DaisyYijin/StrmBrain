package api

import (
	"encoding/json"
	"fmt"
	"strings"
)

// ==================== 多个 115 媒体库（2026-10-12，第 1 期：同步） ====================
//
// 原来只有一个媒体库（full.cid），现在可以再加几个（full.extra_libs）。
// 主库仍是 full.cid：老配置不用迁移，回退到旧版本也只是少同步几个库；
// 第 2 期整理按分类规则分库时，没指定库的规则也落在主库。
//
// 本地这一侧天生就支持多库：STRM 路径是 LocalPath/<115 库目录名>/分类/片目，
// 台账 key、海报墙、Emby 路径映射都已经按「第一段是库名」处理。所以多库的硬约束只有一条：
// **库目录名不能重名**，否则两个库落进同一棵本地目录树（保存时 checkWorkspaceSlots 拦，
// 同步时 resolveLibRoots 再拦一道：网盘上事后改名也会撞上）。
//
// 读库清单一律走这里（fullSyncCfg.libCids / libCidsOf），别再自己 Unmarshal full 取 cid。

// libRootEntry full.extra_libs 的一项
type libRootEntry struct {
	Cid  string `json:"cid"`
	Path string `json:"path,omitempty"` // 只为界面回显，同步以 cid 为准
}

// libCidsOf 主库 + 附加库的 cid：去空、去网盘根、去重，主库在前
func libCidsOf(primary string, extras []libRootEntry) []string {
	seen := map[string]bool{}
	var out []string
	add := func(cid string) {
		cid = strings.TrimSpace(cid)
		if cid == "" || cid == "0" || seen[cid] {
			return
		}
		seen[cid] = true
		out = append(out, cid)
	}
	add(primary)
	for _, e := range extras {
		add(e.Cid)
	}
	return out
}

// libCids 这份配置里的全部媒体库（主库在前）
func (c fullSyncCfg) libCids() []string { return libCidsOf(c.Cid, c.ExtraLibs) }

// otherLibCids 附加媒体库
func (c fullSyncCfg) otherLibCids() []string {
	all := c.libCids()
	if len(all) > 0 && all[0] == strings.TrimSpace(c.Cid) {
		return all[1:]
	}
	return all
}

// parseFullCfg 解析 full 配置原文；解析不了按零值（没配）处理
func parseFullCfg(raw string) fullSyncCfg {
	var cfg fullSyncCfg
	_ = json.Unmarshal([]byte(raw), &cfg)
	return cfg
}

// otherLibCidsCompat 不挂 Handler 的地方用（读 settingValueCompat）
func otherLibCidsCompat() []string {
	return parseFullCfg(settingValueCompat("full")).otherLibCids()
}

// isOtherLibRoot cid 是不是附加媒体库根
func isOtherLibRoot(cid string) bool {
	for _, c := range otherLibCidsCompat() {
		if c == cid {
			return true
		}
	}
	return false
}

// libRoot 解析过的一个媒体库
type libRoot struct {
	Cid  string
	Name string // 115 目录名 = 本地第一层
	Abs  string // 网盘绝对路径
}

// resolveLibRoots 把库清单解析成名字与网盘路径（都走路径缓存，命中不打接口）。
//
// 只有一个库时沿用老行为：名字取不到也照常返回（库名为空的老部署照旧能用）。
// 多个库时名字是区分它们的唯一依据，取不到或重名一律报错，调用方据此停手，
// 不能把 B 库的内容按 A 库的位置去找
func resolveLibRoots(dirName, absPath func(cid string) string, cids []string) ([]libRoot, error) {
	out := make([]libRoot, 0, len(cids))
	byName := map[string]string{}
	for _, cid := range cids {
		r := libRoot{Cid: cid, Name: dirName(cid), Abs: absPath(cid)}
		if len(cids) > 1 {
			if r.Name == "" || r.Abs == "" {
				return nil, fmt.Errorf("媒体库目录（cid=%s）读不出名字或位置（可能已删除，或暂时连不上 115）", cid)
			}
			if prev, ok := byName[r.Name]; ok {
				return nil, fmt.Errorf("两个媒体库目录同名「%s」（cid=%s 与 %s）：本地第一层就是库目录名，同名会落进同一个本地目录，请在 115 上把其中一个改名", r.Name, prev, cid)
			}
			byName[r.Name] = cid
		}
		out = append(out, r)
	}
	return out, nil
}

// libRootNamed 按本地第一层的库名找库。只有一个库时不看名字（老台账可能不带库名、或库改过名）
func libRootNamed(roots []libRoot, name string) (libRoot, bool) {
	if len(roots) == 1 {
		return roots[0], true
	}
	for _, r := range roots {
		if r.Name == name {
			return r, true
		}
	}
	return libRoot{}, false
}

// splitLibRel 本地相对路径（第一段是库名）→ 库名 + 库内路径
func splitLibRel(rel string) (lib, rest string) {
	rel = strings.Trim(strings.ReplaceAll(rel, `\`, "/"), "/")
	lib, rest, _ = strings.Cut(rel, "/")
	return lib, rest
}

// resolveConfiguredLibRoots 当前配置的全部媒体库，按 Cookie 解析
func (h *Handler) resolveConfiguredLibRoots(cookie string) ([]libRoot, error) {
	cids := h.loadFullSyncCfg().libCids()
	if len(cids) == 0 {
		return nil, fmt.Errorf("未配置媒体库目录")
	}
	return resolveLibRoots(func(cid string) string {
		if info, err := get115DirInfo(cookie, cid); err == nil {
			return info.n
		}
		return ""
	}, func(cid string) string { return absPathOf(cookie, cid) }, cids)
}

// panDirOfLocal 本地相对目录（第一段是库名）→ 网盘绝对路径。
// 第一段对上哪个库就用哪个库；都对不上（监控目录不是本地媒体根、老台账不带库名）退回主库、整段照用，
// 与只有一个库时的老行为一致。按整段比库名：原来按字符串前缀剥，「影视2/…」会被当成「影视」剥成「2/…」
func panDirOfLocal(roots []libRoot, relDir string) string {
	relDir = strings.Trim(strings.ReplaceAll(relDir, `\`, "/"), "/")
	if relDir == "." {
		relDir = ""
	}
	if len(roots) == 0 {
		return ""
	}
	root, rest := roots[0], relDir
	lib, r := splitLibRel(relDir)
	for _, x := range roots {
		if x.Name != "" && x.Name == lib {
			root, rest = x, r
			break
		}
	}
	abs := strings.TrimSuffix(root.Abs, "/")
	if abs == "" {
		return ""
	}
	if rest != "" {
		abs += "/" + rest
	}
	return abs
}
