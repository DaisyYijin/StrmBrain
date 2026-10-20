package api

import (
	"errors"
	"fmt"
	"log"
	"path"
	"strings"

	"115-station/internal/model"
)

// ==================== 库的本地目录名（2026-10-20，MULTI-ACCOUNT-PLAN.md §3） ====================
//
// 本地 STRM 的第一层原来就是 115 上库目录的名字，每次同步现读。于是：读不出名字多库就整轮停下；
// 两个库同名只能去 115 上改名；115 上一改名，本地第一层跟着变、规则里的 library 认不出（librariesfix.go 那一套）。
//
// 现在每个库有自己的本地目录名，存在 lib_opts.libs[cid].local（**不放 full**：full 被几个页面整存整取，
// 新字段会被没刷新过的页面冲掉，onLibsSaved 踩过）。它是 STRM 第一层、台账路径第一段、规则 library 认的名字，
// 定下来以后 115 上改名只影响显示的网盘路径。
//
//   - 空 = 老行为（现读 115 目录名），老配置、回退到老版本都照常工作；
//   - 启动时、保存媒体库后由 FreezeLibLocalNames 把空的补上（只读缓存与配置，零 115 请求）；
//   - 只在这个库的台账一行都没有时能改（改名等于搬整棵 STRM 树、改写台账与整理记录，Emby 里的路径全变）。
//
// 取库名一律经 libDirName / libLocalName，别再直接拿 get115DirInfo 的名字当本地第一层。

// libLocalName 配置里这个库的本地目录名；没定过返回空
func libLocalName(cid string) string {
	return strings.TrimSpace(loadLibOpts().of(cid).Local)
}

// libDirName 库的本地第一层：定过本地名用它，否则现读 115 目录名（老行为）。读不出返回空
func libDirName(cookie, cid string) string {
	if n := libLocalName(cid); n != "" {
		return n
	}
	if cookie == "" || cid == "" {
		return ""
	}
	if info, err := get115DirInfo(cookie, cid); err == nil {
		return info.n
	}
	return ""
}

// libLocalBadChars Windows / SMB 挂载下不能出现在目录名里的字符（本地 STRM 根常是这类挂载）
const libLocalBadChars = `/\:*?"<>|`

// checkLibLocalName 用户填的本地目录名合不合法。从 115 带过来的名字不过这一道（老部署一直这么用着）
func checkLibLocalName(name string) error {
	if name == "" {
		return errors.New("本地目录名不能为空")
	}
	if name == "." || name == ".." {
		return fmt.Errorf("本地目录名不能是「%s」", name)
	}
	if strings.ContainsAny(name, libLocalBadChars) {
		return fmt.Errorf("本地目录名「%s」不能带这些字符：%s", name, libLocalBadChars)
	}
	if strings.HasSuffix(name, ".") {
		return fmt.Errorf("本地目录名「%s」不能以点结尾（Windows 会把它吞掉）", name)
	}
	return nil
}

// libLocalKey 比较重名用：大小写不敏感（Windows / SMB 下「Movies」和「movies」是同一个目录）
func libLocalKey(name string) string { return strings.ToLower(strings.TrimSpace(name)) }

// ledgerHasLib 台账里有没有以 name 为第一段的行（这个本地目录下已经有同步 / 整理进来的东西）
func ledgerHasLib(name string) bool {
	name = strings.TrimSpace(name)
	if name == "" || model.DB == nil {
		return false
	}
	var n int64
	model.DB.Model(&model.SyncedFile{}).Where(`rel_path LIKE ? ESCAPE '\'`, likeEscape(name)+"/%").Limit(1).Count(&n)
	return n > 0
}

// libLocalCands 一个库的本地名候选（零 115 请求）：路径缓存里 115 现在的名字、配置里存的显示路径的目录名
func libLocalCands(s libNameSource) []string {
	var out []string
	add := func(n string) {
		n = strings.TrimSpace(n)
		if n == "" || n == "/" || n == "." {
			return
		}
		for _, x := range out {
			if x == n {
				return
			}
		}
		out = append(out, n)
	}
	if s.CachedOK {
		n := s.Cached.Name
		if n == "" && s.Cached.Path != "" {
			n = path.Base(s.Cached.Path)
		}
		add(n)
	}
	if p := strings.TrimRight(strings.TrimSpace(s.Shown), "/"); strings.HasPrefix(p, "/") {
		add(path.Base(p))
	}
	return out
}

// planLibLocalFreeze 给还没定本地名的库定下来（纯函数）。返回 cid → 名字，只含这次要补的。
//
// 候选里优先挑台账里已经在用的：库在 115 上改过名、改名后还没同步过时，本地树还在旧名下，
// 按缓存里的新名定下来就等于把这个库挪到一棵空树上。都没在用就取第一个（缓存里的现名）。
// 定出来的名字与别的库撞了（大小写不敏感）就这一个不定，留着老行为，打日志等用户处理
func planLibLocalFreeze(sources []libNameSource, opts libOpts, ledgerHas func(string) bool) (map[string]string, []string) {
	taken := map[string]string{}
	for _, s := range sources {
		if n := strings.TrimSpace(opts.of(s.Cid).Local); n != "" {
			taken[libLocalKey(n)] = s.Cid
		}
	}
	out := map[string]string{}
	var warns []string
	for _, s := range sources {
		if strings.TrimSpace(opts.of(s.Cid).Local) != "" {
			continue
		}
		cands := libLocalCands(s)
		if len(cands) == 0 {
			continue // 名字还没进缓存：下次（同步过一次、打开过网盘文件页）再定
		}
		pick := cands[0]
		for _, c := range cands {
			if ledgerHas(c) {
				pick = c
				break
			}
		}
		if prev, ok := taken[libLocalKey(pick)]; ok {
			warns = append(warns, fmt.Sprintf("媒体库 cid=%s 的目录名「%s」与 cid=%s 撞名，本地目录名先不定，请到「账号与媒体库 → 媒体库」给其中一个改名", s.Cid, pick, prev))
			continue
		}
		taken[libLocalKey(pick)] = s.Cid
		out[s.Cid] = pick
	}
	return out, warns
}

// FreezeLibLocalNames 把还没定本地名的库定下来（幂等，零 115 请求）。启动时、保存媒体库配置后调用
func (h *Handler) FreezeLibLocalNames() {
	if h.Config == nil {
		return
	}
	fullRaw := h.getSettingValue("full")
	// 固化的是老配置里的本地名（媒体库表之前那一套，libstore.go 迁移时读它）
	sources := legacyLibNameSources(fullRaw)
	if len(sources) == 0 {
		return
	}
	optsRaw := h.getSettingValue(libOptsKey)
	opts := parseLibOpts(optsRaw)
	add, warns := planLibLocalFreeze(sources, opts, ledgerHasLib)
	for _, w := range warns {
		log.Printf("[媒体库] ⚠ %s", w)
	}
	if len(add) == 0 {
		return
	}
	if opts.Libs == nil {
		opts.Libs = map[string]libOpt{}
	}
	for cid, name := range add {
		o := opts.Libs[cid]
		o.Local = name
		opts.Libs[cid] = o
	}
	// 附加账号的库的选项（层级、刮削）也要留着（acctlibs.go）
	out := marshalLibOpts(opts, allLibKeys(fullRaw, h.getSettingValue(acctLibsKey)))
	if err := h.Config.SaveSetting(libOptsKey, out); err != nil {
		log.Printf("[媒体库] ✗ 记下本地目录名失败（下次启动再试，期间照旧按 115 目录名）: %v", err)
		return
	}
	for cid, name := range add {
		log.Printf("[媒体库] ✓ 媒体库 cid=%s 的本地目录名定为「%s」：以后 115 上改名不影响本地", cid, name)
	}
	forgetLedgerTitles()
	forgetLocalTitles()
}
