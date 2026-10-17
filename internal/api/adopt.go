package api

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"

	"github.com/gin-gonic/gin"

	"115-station/internal/model"
)

// ==================== 接管已有 STRM 目录：预演 ====================
//
// 用户从别的 STRM 项目换过来时，Emby 的条目挂在本地 STRM 的路径上：路径和文件名一个都不变、
// 只把内容换成本站直链并登记台账，观看记录与元数据才都保得住（ADOPT-STRM-PLAN.md）。
// 这里是第 2 期：只读预演。扫本地 STRM、取一次网盘清单、逐个对号分桶，出一份报告，
// **不写本地、不写台账**（取清单时顺手记的路径缓存除外，那是全量本来就做的）。
// 执行（登记台账 + 改写内容）是第 3 期。
//
// Emby 查询媒体源与播放时都现读 STRM 内容（2026-10-17 实测，ADOPT-STRM-PLAN.md §5.1），
// 所以接管只改内容就行，不用通知 Emby。

func init() {
	jobExecutors["adopt"] = execAdoptPreviewJob
}

// adoptJobParams 预演参数
type adoptJobParams struct {
	// ScanDir 去哪里找旧 STRM（容器内绝对路径），空 = 本地媒体库目录。
	// 旧目录还没挂到本地根下面时，用它看清楚旧文件的层级、算出该怎么挂
	ScanDir string `json:"scan_dir,omitempty"`
}

// localStrm 本地一个 .strm
type localStrm struct {
	Rel string     // 相对本地媒体库目录（斜杠）；不在它下面时是绝对路径
	Abs string     // 绝对路径（斜杠）
	Ref foreignRef // 内容指向哪个网盘文件
}

// 报告里的桶，顺序即界面上的顺序
const (
	adoptBucketAdopt    = "adopt"    // 原样接管：位置与文件名都是本站会写的
	adoptBucketRename   = "rename"   // 名字不同：目录对，文件名是本站两种写法以外的
	adoptBucketAlready  = "already"  // 台账里已经是这个文件（本站自己写的，或已接管过）
	adoptBucketMoved    = "moved"    // 位置不同：目录对不上，要先改挂载 / 本地根
	adoptBucketDup      = "dup"      // 重复：和另一个本地 STRM 指向同一个网盘文件
	adoptBucketMissing  = "missing"  // 网盘上没有（或不在配置的媒体库里）
	adoptBucketShare    = "share"    // 分享 STRM：不是本账号的文件
	adoptBucketUnknown  = "unknown"  // 认不出指向哪里
	adoptBucketNotLocal = "notlocal" // 网盘上有、本地没有 STRM（之后由普通全量补写）
)

var adoptBucketOrder = []string{
	adoptBucketAdopt, adoptBucketRename, adoptBucketAlready, adoptBucketMoved, adoptBucketDup,
	adoptBucketMissing, adoptBucketShare, adoptBucketUnknown, adoptBucketNotLocal,
}

// adoptSampleLimit 每个桶最多带几条样例（报告整份存进 TaskJob.Result）
const adoptSampleLimit = 50

type adoptSample struct {
	Local  string `json:"local,omitempty"`  // 本地 STRM（相对本地根）
	Expect string `json:"expect,omitempty"` // 本站会写成的位置（相对本地根）
	Remote string `json:"remote,omitempty"` // 网盘文件（库内相对路径）
	Note   string `json:"note,omitempty"`
}

type adoptBucket struct {
	Key     string        `json:"key"`
	Count   int           `json:"count"`
	Samples []adoptSample `json:"samples"`
}

// adoptShift 「位置不同」里一种前缀换算：本地 From 下面的东西，本站会写在 To 下面
type adoptShift struct {
	From  string `json:"from"`
	To    string `json:"to"`
	Count int    `json:"count"`
}

type adoptLibInfo struct {
	Cid    string `json:"cid"`
	Name   string `json:"name"`
	Videos int    `json:"videos"`
	Mode   string `json:"mode,omitempty"`
	Error  string `json:"error,omitempty"`
}

type adoptReport struct {
	LocalRoot    string         `json:"local_root"`
	ScanDir      string         `json:"scan_dir"`
	LocalStrm    int            `json:"local_strm"`
	RemoteVideos int            `json:"remote_videos"`
	Libs         []adoptLibInfo `json:"libs"`
	// Formats 各项目写法各几个（看着像谁写的）
	Formats map[string]int `json:"formats"`
	Buckets []*adoptBucket `json:"buckets"`
	// Shifts 「位置不同」按前缀换算归类，最多 5 种
	Shifts []adoptShift `json:"shifts"`
	// OtherAccount 「网盘上没有」里 pickcode 不是这个 115 账号的个数（换了账号时按路径对号）
	OtherAccount int `json:"other_account"`
	// Verdict ok = 可以接管；move = 多数位置对不上，先按 Shifts 改挂载；empty = 一个都没对上
	Verdict string `json:"verdict"`
}

func (r *adoptReport) bucket(key string) *adoptBucket {
	for _, b := range r.Buckets {
		if b.Key == key {
			return b
		}
	}
	b := &adoptBucket{Key: key, Samples: []adoptSample{}}
	r.Buckets = append(r.Buckets, b)
	return b
}

func (r *adoptReport) add(key string, s adoptSample) {
	b := r.bucket(key)
	b.Count++
	if len(b.Samples) < adoptSampleLimit {
		b.Samples = append(b.Samples, s)
	}
}

func (r *adoptReport) count(key string) int {
	for _, b := range r.Buckets {
		if b.Key == key {
			return b.Count
		}
	}
	return 0
}

// adoptRank 同一个网盘文件对上好几个本地 STRM 时留哪个：越小越优先
var adoptRank = map[string]int{
	adoptBucketAlready: 0, adoptBucketAdopt: 1, adoptBucketRename: 2, adoptBucketMoved: 3,
}

// planAdopt 对号分桶（纯函数，不读写任何东西）。
// remote 只放视频；ledger 是台账里视频的 fid → 相对路径；localRoot 用来算「位置不同」的绝对前缀
func planAdopt(locals []localStrm, remote []remoteFile, ledger map[string]string, localRoot string) adoptReport {
	rep := adoptReport{LocalStrm: len(locals), RemoteVideos: len(remote), Formats: map[string]int{}, Shifts: []adoptShift{}}
	for _, k := range adoptBucketOrder {
		rep.bucket(k)
	}
	localRoot = strings.TrimRight(filepath.ToSlash(localRoot), "/")

	byPC := make(map[string]int, len(remote))
	byFid := make(map[string]int, len(remote))
	byPath := make(map[string]int, len(remote))
	for i, f := range remote {
		if f.PickCode != "" {
			byPC[strings.ToLower(f.PickCode)] = i
		}
		byFid[f.Fid] = i
		byPath[path.Join(f.Path, f.Name)] = i
	}
	// 账号不动点：pickcode 对不上时用来判断是不是另一个 115 账号的文件
	acct := ""
	for _, f := range remote {
		if sp, err := stablePoint115(f.PickCode); err == nil {
			acct = sp
			break
		}
	}

	sorted := append([]localStrm(nil), locals...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Rel < sorted[j].Rel })

	type cand struct {
		l      localStrm
		bucket string
	}
	groups := map[int][]cand{}
	var order []int
	for _, l := range sorted {
		rep.Formats[l.Ref.Format]++
		switch l.Ref.Kind {
		case "share":
			rep.add(adoptBucketShare, adoptSample{Local: l.Rel})
			continue
		case "unknown":
			rep.add(adoptBucketUnknown, adoptSample{Local: l.Rel})
			continue
		}
		idx, ok := adoptMatch(l.Ref, byPC, byFid, byPath)
		if !ok {
			s := adoptSample{Local: l.Rel}
			if l.Ref.PickCode != "" && acct != "" {
				if sp, err := stablePoint115(l.Ref.PickCode); err == nil && sp != acct {
					rep.OtherAccount++
					s.Note = "不是这个 115 账号的文件"
				}
			}
			rep.add(adoptBucketMissing, s)
			continue
		}
		f := remote[idx]
		b := adoptBucketMoved
		base := path.Base(l.Rel)
		switch {
		case ledger[f.Fid] == l.Rel:
			b = adoptBucketAlready
		case !path.IsAbs(l.Rel) && path.Dir(l.Rel) == f.Path:
			if base == strmNameOf(f.Name) || base == legacyStrmNameOf(f.Name) {
				b = adoptBucketAdopt
			} else {
				b = adoptBucketRename
			}
		}
		if _, seen := groups[idx]; !seen {
			order = append(order, idx)
		}
		groups[idx] = append(groups[idx], cand{l, b})
	}

	shifts := map[[2]string]int{}
	for _, idx := range order {
		f := remote[idx]
		cs := groups[idx]
		// 同一档里本站现在的写法（stem.strm）优先于旧写法（stem.mkv.strm）：留下的那份以后不用再改名
		score := func(c cand) int {
			s := adoptRank[c.bucket] * 2
			if path.Base(c.l.Rel) != strmNameOf(f.Name) {
				s++
			}
			return s
		}
		sort.SliceStable(cs, func(i, j int) bool { return score(cs[i]) < score(cs[j]) })
		win := cs[0]
		expect := path.Join(f.Path, strmNameOf(f.Name))
		remoteRel := path.Join(f.Path, f.Name)
		s := adoptSample{Local: win.l.Rel, Expect: expect, Remote: remoteRel}
		if win.bucket == adoptBucketMoved {
			from, to := adoptPathShift(path.Dir(win.l.Abs), path.Join(localRoot, f.Path))
			shifts[[2]string{from, to}]++
		}
		rep.add(win.bucket, s)
		for _, c := range cs[1:] {
			rep.add(adoptBucketDup, adoptSample{Local: c.l.Rel, Remote: remoteRel, Note: "与 " + win.l.Rel + " 指向同一个网盘文件"})
		}
	}
	for i, f := range remote {
		if _, ok := groups[i]; !ok {
			rep.add(adoptBucketNotLocal, adoptSample{Expect: path.Join(f.Path, strmNameOf(f.Name)), Remote: path.Join(f.Path, f.Name)})
		}
	}

	for k, n := range shifts {
		rep.Shifts = append(rep.Shifts, adoptShift{From: k[0], To: k[1], Count: n})
	}
	sort.Slice(rep.Shifts, func(i, j int) bool {
		if rep.Shifts[i].Count != rep.Shifts[j].Count {
			return rep.Shifts[i].Count > rep.Shifts[j].Count
		}
		return rep.Shifts[i].From < rep.Shifts[j].From
	})
	if len(rep.Shifts) > 5 {
		rep.Shifts = rep.Shifts[:5]
	}

	kept := rep.count(adoptBucketAdopt) + rep.count(adoptBucketRename) + rep.count(adoptBucketAlready)
	moved := rep.count(adoptBucketMoved)
	switch {
	case kept == 0 && moved == 0:
		rep.Verdict = "empty"
	case moved > kept:
		rep.Verdict = "move"
	default:
		rep.Verdict = "ok"
	}
	return rep
}

// adoptMatch pickcode → 文件 id → 网盘路径，依次对号。
// 路径按后缀对：内容里的路径是网盘完整路径（带前缀、可能从更上层开始），
// 清单里是「库目录名/库内路径」，取最长的一段能对上的后缀
func adoptMatch(ref foreignRef, byPC, byFid, byPath map[string]int) (int, bool) {
	if ref.PickCode != "" {
		if i, ok := byPC[ref.PickCode]; ok {
			return i, true
		}
	}
	if ref.FileID != "" {
		if i, ok := byFid[ref.FileID]; ok {
			return i, true
		}
	}
	if ref.PanPath != "" {
		segs := strings.Split(strings.Trim(ref.PanPath, "/"), "/")
		for i := 0; i < len(segs); i++ {
			if idx, ok := byPath[strings.Join(segs[i:], "/")]; ok {
				return idx, true
			}
		}
	}
	return 0, false
}

// adoptPathShift 两个目录去掉末尾相同的几段，剩下的就是「本地这里 → 本站那里」的前缀换算
func adoptPathShift(localDir, expectDir string) (from, to string) {
	a := strings.Split(strings.Trim(localDir, "/"), "/")
	b := strings.Split(strings.Trim(expectDir, "/"), "/")
	for len(a) > 0 && len(b) > 0 && a[len(a)-1] == b[len(b)-1] {
		a, b = a[:len(a)-1], b[:len(b)-1]
	}
	return "/" + strings.Join(a, "/"), "/" + strings.Join(b, "/")
}

// adoptStrmReadLimit 读 STRM 内容的上限：正常的直链不过几百字节，读多了只会拖慢万级目录的扫描
const adoptStrmReadLimit = 8 << 10

// scanLocalStrms 遍历 scanDir 下所有 .strm（跳过隐藏目录与群晖的 @eaDir），Rel 相对 localRoot
func scanLocalStrms(scanDir, localRoot string, progress func(int)) ([]localStrm, error) {
	root := filepath.Clean(localRoot)
	var out []localStrm
	err := filepath.WalkDir(scanDir, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			if p == scanDir {
				return err
			}
			log.Printf("[接管] ○ 读不了 %s，跳过: %v", p, err)
			if d != nil && d.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		name := d.Name()
		if d.IsDir() {
			if p != scanDir && (strings.HasPrefix(name, ".") || name == "@eaDir") {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.EqualFold(filepath.Ext(name), ".strm") {
			return nil
		}
		content, err := readHead(p, adoptStrmReadLimit)
		if err != nil {
			log.Printf("[接管] ○ 读不了 %s，跳过: %v", p, err)
			return nil
		}
		abs := filepath.ToSlash(p)
		rel := abs
		if r, err := filepath.Rel(root, p); err == nil && r != ".." && !strings.HasPrefix(r, ".."+string(filepath.Separator)) {
			rel = filepath.ToSlash(r)
		}
		out = append(out, localStrm{Rel: rel, Abs: abs, Ref: parseForeignStrm(content)})
		if progress != nil && len(out)%500 == 0 {
			progress(len(out))
		}
		return nil
	})
	return out, err
}

func readHead(p string, limit int64) (string, error) {
	f, err := os.Open(p)
	if err != nil {
		return "", err
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, limit))
	return string(b), err
}

func execAdoptPreviewJob(h *Handler, job *model.TaskJob) (jobOutcome, error) {
	var params adoptJobParams
	if p := decodeJobParams(job).Adopt; p != nil {
		params = *p
	}
	cfg := h.loadFullSyncCfg()
	cids := cfg.libCids()
	if len(cids) == 0 {
		return jobOutcome{}, errors.New("还没配置 115 媒体库目录，先到「账号与媒体库 → 媒体库」配好")
	}
	localRoot := cfg.LocalPath
	if localRoot == "" {
		localRoot = defaultLocalPath
	}
	scanDir := strings.TrimSpace(params.ScanDir)
	if scanDir == "" {
		scanDir = localRoot
	}
	if st, err := os.Stat(scanDir); err != nil || !st.IsDir() {
		return jobOutcome{}, fmt.Errorf("读不了目录 %s（容器里没挂上？）", scanDir)
	}

	setJobProgress("扫描本地 STRM", progressKeep, progressKeep, "")
	locals, err := scanLocalStrms(scanDir, localRoot, func(n int) {
		setJobProgress("扫描本地 STRM", n, progressKeep, "")
	})
	if err != nil {
		return jobOutcome{}, fmt.Errorf("扫描 %s 失败: %w", scanDir, err)
	}
	// 一个都没有时不下任何结论：多半是挂载没就绪，或者旧 STRM 不在这个目录下（同 strmmigrate.go 的守卫）
	if len(locals) == 0 {
		return jobOutcome{}, fmt.Errorf("%s 下一个 .strm 都没找到：容器里没挂上，或者旧 STRM 不在这个目录下", scanDir)
	}

	ops, err := h.newPan115Ops()
	if err != nil {
		return jobOutcome{}, err
	}
	cookie, _ := h.get115Cookie()
	vexts := buildExtSet(cfg.VideoExt)
	if len(vexts) == 0 {
		vexts = videoExts
	}
	filter := &syncFilter{videoExts: vexts, assetExts: map[string]bool{}}

	var remote []remoteFile
	var libs []adoptLibInfo
	failed := 0
	for i, cid := range cids {
		info := adoptLibInfo{Cid: cid}
		if di, err := get115DirInfo(cookie, cid); err == nil {
			info.Name = di.n
		}
		if info.Name == "" {
			info.Error = "读不出媒体库目录名（可能已删除，或暂时连不上 115）"
			libs = append(libs, info)
			failed++
			continue
		}
		setJobProgress(fmt.Sprintf("取网盘清单 %d/%d：%s", i+1, len(cids), info.Name), progressKeep, progressKeep, "")
		skipCids, _ := h.orgSkipCids(cid)
		var videos []remoteFile
		// 预演只要视频，固定先试快速模式（请求少两个数量级），不可用时 collectSyncFiles 自己降级
		mode, _, err := h.collectSyncFiles(ops, cookie, "fast", cid, info.Name, &videos, nil, filter, skipCids, nil)
		info.Mode = mode
		if err != nil {
			info.Error = err.Error()
			failed++
		} else {
			info.Videos = len(videos)
			remote = append(remote, videos...)
		}
		libs = append(libs, info)
	}
	if failed == len(cids) {
		msgs := make([]string, 0, len(libs))
		for _, l := range libs {
			msgs = append(msgs, firstNonEmpty(l.Name, "cid="+l.Cid)+"："+l.Error)
		}
		return jobOutcome{}, fmt.Errorf("取不到网盘清单：%s", strings.Join(msgs, "；"))
	}

	var rows []model.SyncedFile
	if err := h.DB.Select("file_id", "rel_path").Where("kind = ?", "video").Find(&rows).Error; err != nil {
		return jobOutcome{}, fmt.Errorf("读台账失败: %w", err)
	}
	ledger := make(map[string]string, len(rows))
	for _, r := range rows {
		ledger[r.FileID] = r.RelPath
	}

	rep := planAdopt(locals, remote, ledger, localRoot)
	rep.LocalRoot = filepath.ToSlash(localRoot)
	rep.ScanDir = filepath.ToSlash(scanDir)
	rep.Libs = libs

	msg := fmt.Sprintf("本地 STRM %d 个：可原样接管 %d、名字不同 %d、已在台账 %d、位置不同 %d、重复 %d、网盘上没有 %d、分享 %d、认不出 %d；网盘上有、本地没有 %d",
		rep.LocalStrm, rep.count(adoptBucketAdopt), rep.count(adoptBucketRename), rep.count(adoptBucketAlready),
		rep.count(adoptBucketMoved), rep.count(adoptBucketDup), rep.count(adoptBucketMissing),
		rep.count(adoptBucketShare), rep.count(adoptBucketUnknown), rep.count(adoptBucketNotLocal))
	if failed > 0 {
		msg += fmt.Sprintf("；%d 个库没取到清单", failed)
	}
	log.Printf("[接管] ✓ 预演完成（%s）：%s", rep.ScanDir, msg)
	return jobOutcome{Message: msg, Result: rep, Partial: failed > 0}, nil
}

// AdoptPreview POST /sync/adopt/preview  body: {"scan_dir": ""}
// 入队一个只读预演，结果在任务详情的 result 里（adoptReport）
func (h *Handler) AdoptPreview(c *gin.Context) {
	var req adoptJobParams
	_ = c.ShouldBindJSON(&req)
	req.ScanDir = strings.TrimSpace(req.ScanDir)
	if req.ScanDir != "" && !filepath.IsAbs(req.ScanDir) && !strings.HasPrefix(req.ScanDir, "/") {
		c.JSON(http.StatusBadRequest, gin.H{"error": "扫描目录要写容器里的绝对路径"})
		return
	}
	job, err := enqueueJob(h.DB, jobSpec{Kind: "adopt", Title: "接管已有 STRM：预演", DedupeKey: "adopt",
		Source: "web", Priority: jobPriorityManual, Params: jobParams{Adopt: &req}})
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	h.queuedReply(c, job, "接管预演")
}
