package api

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"path"
	"strconv"
	"strings"
	"time"

	"115-station/internal/config"
	"115-station/internal/model"

	"gorm.io/gorm"
)

// ==================== 附加账号的库（2026-10-20，MULTI-ACCOUNT-PLAN.md §5 / §14 / §15，阶段 3） ====================
//
// 附加账号的目录可以挂成库，一律按「我自己整理」处理：本站只同步 STRM、播放、刮削，不往里整理、不改网盘。
// 按 §14 的最坏情况设计，不依赖「fid / 事件 id 跨账号唯一」「pickcode 能跨账号取直链」这些没验证过的事：
//
//   - 库存在 setting acct_libs（老版本读不到，回退时就像没有这些库），库 key = a{账号}:{cid}；
//   - 直链 /d/{账号}~{pickcode}（主号不变），302 按账号段取那个账号的 Cookie；
//   - 台账 FileID 存 a{账号}:{fid}、PickCode 存 {账号}~{pickcode}。两种都不是合法的 115 id：
//     万一被哪条只认主号的老代码拿去调 115 接口，只会报错，不会动到主号的文件；
//   - 洗版 / sha1 去重、订阅「库里已有」、深度删除、元数据回传都排除这些行（ledgerMainOnlySQL / isAcctLedgerFid）；
//   - 不写 PathCache（按路径查时不分账号）。
//
// 只靠全量同步更新（没有生活事件增量），另有 acctLibSyncTick 每 6 小时补一次只同步它们的全量。

const acctLibsKey = "acct_libs"

// acctLibEntry acct_libs 的一项
type acctLibEntry struct {
	Account uint   `json:"account"`
	Cid     string `json:"cid"`
	Path    string `json:"path,omitempty"` // 选目录那一刻的网盘路径，只给界面显示
	Local   string `json:"local"`          // 本地目录名，必填（这类库没有路径缓存，认不出 115 目录名）
}

// key 这个库在 lib_opts、库卡片里的 key
func (e acctLibEntry) key() string { return acctLibKey(e.Account, e.Cid) }

// acctLibKey a{账号}:{cid}
func acctLibKey(acct uint, cid string) string {
	return fmt.Sprintf("a%d:%s", acct, strings.TrimSpace(cid))
}

// splitAcctLibKey 反过来拆；不是这种 key 返回 ok=false
func splitAcctLibKey(key string) (acct uint, cid string, ok bool) {
	if !strings.HasPrefix(key, "a") {
		return 0, "", false
	}
	a, c, found := strings.Cut(key[1:], ":")
	if !found || c == "" {
		return 0, "", false
	}
	n, err := strconv.ParseUint(a, 10, 32)
	if err != nil || n == 0 {
		return 0, "", false
	}
	return uint(n), c, true
}

// isAcctLibKey 是不是附加账号的库 key
func isAcctLibKey(key string) bool {
	_, _, ok := splitAcctLibKey(key)
	return ok
}

func parseAcctLibs(raw string) []acctLibEntry {
	var out []acctLibEntry
	if strings.TrimSpace(raw) == "" {
		return nil
	}
	if err := json.Unmarshal([]byte(raw), &out); err != nil {
		log.Printf("[媒体库] ✗ 附加账号的库配置解析失败，按没有处理: %v", err)
		return nil
	}
	keep := out[:0]
	for _, e := range out {
		e.Cid, e.Local = strings.TrimSpace(e.Cid), strings.TrimSpace(e.Local)
		if e.Account == acctMain || e.Cid == "" || e.Cid == "0" || e.Local == "" {
			continue
		}
		keep = append(keep, e)
	}
	return keep
}

// loadAcctLibs 当前配置的附加账号的库
func loadAcctLibs() []acctLibEntry { return parseAcctLibs(settingValueCompat(acctLibsKey)) }

// acctLibKeysOf 一组附加账号库的 key
func acctLibKeysOf(list []acctLibEntry) []string {
	out := make([]string, 0, len(list))
	for _, e := range list {
		out = append(out, e.key())
	}
	return out
}

// allLibKeys 主号的库 cid + 附加账号库的 key（lib_opts 保存时用：只留还在配置里的库）
func allLibKeys(fullRaw, acctRaw string) []string {
	return append(parseFullCfg(fullRaw).libCids(), acctLibKeysOf(parseAcctLibs(acctRaw))...)
}

// acctLibRoots 附加账号的库，当成 libRoot：Self、没有网盘路径（Abs 空 = 本站不往它的网盘上写任何东西）
func acctLibRoots(list []acctLibEntry) []libRoot {
	out := make([]libRoot, 0, len(list))
	for _, e := range list {
		out = append(out, libRoot{Cid: e.key(), Name: e.Local, PanName: path.Base(strings.TrimRight(e.Path, "/")), Self: true, Account: e.Account})
	}
	return out
}

// acctLibNamed 本地第一层是不是附加账号的库（元数据回传、海报墙上传据此跳过：这些库的网盘本站不写）
func acctLibNamed(list []acctLibEntry, name string) bool {
	for _, e := range list {
		if e.Local == name {
			return true
		}
	}
	return false
}

// ---- 直链与台账里的账号段 ----

// encodePlayID 直链里的播放标识：主号就是 pickcode，附加账号是 {账号}~{pickcode}
func encodePlayID(acct uint, pickcode string) string {
	if acct == acctMain || pickcode == "" {
		return pickcode
	}
	return fmt.Sprintf("%d~%s", acct, pickcode)
}

// decodePlayID 拆播放标识。账号段不是正整数时返回空 pickcode（当成无效标识）
func decodePlayID(id string) (uint, string) {
	a, pc, ok := strings.Cut(id, "~")
	if !ok {
		return acctMain, id
	}
	n, err := strconv.ParseUint(a, 10, 32)
	if err != nil {
		return acctMain, ""
	}
	return uint(n), pc
}

// acctLedgerFid 台账里的 FileID：附加账号加 a{账号}: 前缀（fid 跨账号是否唯一没验证过，§14）
func acctLedgerFid(acct uint, fid string) string {
	if acct == acctMain || fid == "" {
		return fid
	}
	return fmt.Sprintf("a%d:%s", acct, fid)
}

// isAcctLedgerFid 台账行是不是附加账号的（主号的 fid 是纯数字）
func isAcctLedgerFid(fid string) bool { return isAcctLibKey(fid) }

// ledgerMainOnlySQL 只看主号台账行的条件（洗版、sha1 去重、订阅「库里已有」：不做跨账号去重）
const ledgerMainOnlySQL = "file_id NOT LIKE 'a%:%'"

// dropAcctLedgerRows 去掉附加账号的台账行，返回去掉了几行（深度删除等会改网盘的入口用）
func dropAcctLedgerRows(rows []model.SyncedFile) ([]model.SyncedFile, int) {
	keep := rows[:0:0]
	n := 0
	for _, r := range rows {
		if isAcctLedgerFid(r.FileID) {
			n++
			continue
		}
		keep = append(keep, r)
	}
	return keep, n
}

// acctCookieByCfg 附加账号的 Cookie（302 不挂 Handler，直接读配置文件与库）
func acctCookieByCfg(db *gorm.DB, cfg *config.Config, acct uint) (string, error) {
	if db == nil || cfg == nil {
		return "", errors.New("配置未就绪")
	}
	var s model.Storage
	if err := db.Where("id = ? AND type = ? AND main = ?", acct, "115", false).First(&s).Error; err != nil {
		return "", fmt.Errorf("直链指向的附加账号 id=%d 已删除", acct)
	}
	if ck, err := cfg.LoadCookieOf(acct); err == nil && ck != "" {
		return ck, nil
	}
	if s.Cookie != "" {
		return s.Cookie, nil
	}
	return "", fmt.Errorf("附加账号「%s」没有登录", s.Name)
}

// firstRelSeg 本地相对路径的第一段（库名）
func firstRelSeg(rel string) string {
	lib, _ := splitLibRel(strings.ReplaceAll(rel, "\\", "/"))
	return lib
}

// ---- 定时补同步 ----

// acctLibSyncEvery 附加账号的库多久补一次全量（没有生活事件增量，只靠它与手动 / 定时全量）。
// 快速模式一个库几次请求，6 小时一次对风控没有压力；LitePan 的 STRM 任务默认扫描间隔也是 360 分钟
const acctLibSyncEvery = 6 * time.Hour

// acctLibsSyncedKey 上次同步过附加账号的库的时间（unix 秒），重启后接着算，别一重启就扫一遍
const acctLibsSyncedKey = "acct_libs_synced_at"

func (h *Handler) markAcctLibsSynced(t time.Time) {
	if h.Config == nil {
		return
	}
	if err := h.Config.SaveSetting(acctLibsSyncedKey, strconv.FormatInt(t.Unix(), 10)); err != nil {
		log.Printf("[同步] ○ 记录附加账号的库同步时间失败（下一轮定时会早一点再扫）: %v", err)
	}
}

func (h *Handler) acctLibsSyncedAt() time.Time {
	n, err := strconv.ParseInt(strings.TrimSpace(h.getSettingValue(acctLibsSyncedKey)), 10, 64)
	if err != nil || n <= 0 {
		return time.Time{}
	}
	return time.Unix(n, 0)
}

// acctLibSyncTick 调度器每分钟调一次：有附加账号的库、离上次同步超过 acctLibSyncEvery 就入队一次只同步它们的全量
func (h *Handler) acctLibSyncTick(now time.Time) {
	if len(loadAcctLibs()) == 0 {
		return
	}
	if last := h.acctLibsSyncedAt(); !last.IsZero() && now.Sub(last) < acctLibSyncEvery {
		return
	}
	h.enqueueAcctLibSync("定时同步附加账号的库", "cron", jobPriorityBackground)
}

// enqueueAcctLibSync 入队一次只同步附加账号的库的全量（去重键单独一个，和主号的全量互不合并）
func (h *Handler) enqueueAcctLibSync(title, source string, priority int) (model.TaskJob, error) {
	p := h.fullParamsFromConfig()
	job, err := enqueueJob(h.DB, jobSpec{Kind: "full", Title: title, DedupeKey: "full:acct",
		Source: source, Priority: priority, Params: jobParams{Sync: &syncJobParams{
			Cid: p.Cid, LocalPath: p.LocalPath, VideoExt: p.VideoExt, ImageExt: p.ImageExt, DataExt: p.DataExt,
			Mode: p.Mode, AcctOnly: true,
		}}})
	if err != nil {
		log.Printf("[同步] ✗ 附加账号的库同步入队失败: %v", err)
	}
	return job, err
}
