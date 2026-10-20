package api

import (
	"fmt"
	"log"
	"sync"
	"time"
)

// ==================== 别的账号的生活事件增量（2026-10-20，MULTI-ACCOUNT-V2-PLAN.md §10.7，V2 阶段 6） ====================
//
// 每个挂着媒体库的账号都走生活事件增量，主流程照旧是 executeIncrementalSyncWith，只换一份按账号的依赖（realIncrDeps.acct）：
//   - 路径：不读写 PathCache（按路径查时不分账号，两个账号都有「/影视」会串），一轮里现查、记在 absMemo；
//     拿不到旧位置的改名 / 删除回退重遍历或按回收站、名字认（和主号缓存没命中时一样）；
//   - 游标与通道降级状态：setting 的 key 带账号（incr-cursor@2、life-endpoint@2）；
//   - 事件表：SyncEvent.Account 分开，EventID 带 a{账号}: 前缀（跨账号会不会撞没验证过）；
//   - 「115 生活」开关每个账号进程内开一次，不动主号的门禁状态与状态页快照。
//
// 轮询：跟在主号那一轮后面、同一把 taskMu 里跑，每个账号默认 60 秒一轮（共用一条 115 节流队列）。
// 每 6 小时一次的补全量（acctLibSyncTick）留着兜底。
//
// 没用真的两个账号跑过（维护者只有一个 115 账号），出问题先看日志里 [同步#N] 的账号号。

// acctIncrInterval 别的账号多久轮一次
const acctIncrInterval = 60 * time.Second

// acctRuntimeKey 运行态水位按账号分开存
func acctRuntimeKey(acct uint, key string) string {
	if acct == acctMain {
		return key
	}
	switch key {
	case "incr-cursor", "life-endpoint", "incr-last":
		return fmt.Sprintf("%s@%d", key, acct)
	}
	return key
}

// incrAcctOf 这份依赖是哪个账号的（测试的桩没有这个方法 = 主号）
func incrAcctOf(d incrDeps) uint {
	if a, ok := d.(interface{ account() uint }); ok {
		return a.account()
	}
	return acctMain
}

func (d *realIncrDeps) account() uint { return d.acct }

// acctAbs 别的账号里一个目录的绝对路径：一轮里查过的复用，不写 PathCache
func (d *realIncrDeps) acctAbs(cid string) string {
	if cid == "" || cid == "0" {
		return ""
	}
	if d.absMemo == nil {
		d.absMemo = map[string]string{}
	}
	if p, ok := d.absMemo[cid]; ok {
		return p
	}
	p, err := acctDirAbs(d.cookie, cid)
	if err != nil {
		p = ""
	}
	d.absMemo[cid] = p
	return p
}

// acctEventID 别的账号的事件在表里的 id
func acctEventID(acct uint, id string) string {
	if acct == acctMain {
		return id
	}
	return fmt.Sprintf("a%d:%s", acct, id)
}

var (
	acctGateMu   sync.Mutex
	acctGateDone = map[uint]bool{}
)

// acctLifeGate 打开一个账号的「115 生活」事件记录（进程内一次）
func acctLifeGate(acct uint, cookie string) {
	acctGateMu.Lock()
	done := acctGateDone[acct]
	acctGateDone[acct] = true
	acctGateMu.Unlock()
	if done {
		return
	}
	if err := enable115Life(cookie); err != nil {
		log.Printf("[同步] ⚠ 账号 %d 的网盘事件流开关打开失败（%v），它的增量可能一直没有内容", acct, err)
	}
}

// newAcctIncrDeps 别的账号的增量依赖
func (h *Handler) newAcctIncrDeps(acct uint) (*realIncrDeps, error) {
	cookie, err := h.cookieOf(acct)
	if err != nil {
		return nil, err
	}
	ops, err := h.opsFor(acct)
	if err != nil {
		return nil, err
	}
	return &realIncrDeps{h: h, cookie: cookie, ops: ops, acct: acct, absMemo: map[string]string{}}, nil
}

// incrAccounts 挂着媒体库的别的账号
func incrAccounts() []uint {
	seen := map[uint]bool{}
	var out []uint
	for _, l := range loadLibraries() {
		if l.Account != acctMain && !seen[l.Account] {
			seen[l.Account] = true
			out = append(out, l.Account)
		}
	}
	return out
}

var acctIncrLast = struct {
	sync.Mutex
	at map[uint]time.Time
}{at: map[uint]time.Time{}}

// runAcctIncrRounds 别的账号各跑一轮（到间隔了的）。调用方持有 taskMu
func (h *Handler) runAcctIncrRounds(p incrParams) {
	for _, acct := range incrAccounts() {
		if _, yield := taskMu.YieldRequested(); yield {
			return // 有人在排队：剩下的账号下一轮再说
		}
		acctIncrLast.Lock()
		due := time.Since(acctIncrLast.at[acct]) >= acctIncrInterval
		if due {
			acctIncrLast.at[acct] = time.Now()
		}
		acctIncrLast.Unlock()
		if !due {
			continue
		}
		d, err := h.newAcctIncrDeps(acct)
		if err != nil {
			vlog("[轮询] ○ 账号 %d 的增量跳过：%v", acct, err)
			continue
		}
		if _, err := h.executeIncrementalSyncWith(d, p); err != nil {
			log.Printf("[轮询] 账号 %d 增量同步失败: %v", acct, err)
		}
	}
}
