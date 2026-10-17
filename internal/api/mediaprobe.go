package api

// ==================== 整理时读取媒体信息补全命名 ====================
//
// 文件名里没写的画质信息（分辨率 / 特效 / 视频编码 / 音频编码 / 帧率，蓝光 ISO 还有资源质量），
// 整理改名前按 115 直链读一点文件头补上（internal/mediaprobe，纯 Go、不靠 ffmpeg）。
// 设计见 docs/115-station-notes/MEDIA-PROBE-PLAN.md；做法参考 ffprobemeta 插件（只看思路）。
//
// 几条约束：
//   - 默认关（setting `media-probe`）。开着也只在命名模板用到了这些变量、文件名又没写全时才读
//     （needProbe），读一次 = 取一次直链（经 throttle115）+ 几次 CDN Range 请求（单文件最多 24 次、8 MB）。
//   - 结果按 sha1 落库（model.MediaProbe），重新整理、同一份文件再进来都不再读。
//   - 剧集同一个版本只读一集（probeSession 按「所在目录 + 文件名里的画质串 + 发布组 + 扩展名 + 体积相近」分组），
//     一部 153 集的番逐集读要 153 次取直链，整理全程还占着 taskMu（§6.12）。设置里可改成逐集。
//   - 读出来的值写成和 ParseResourceInfo 同样的写法（probeResource）：改名后再按新文件名解析要得到同样的值，
//     洗版、资源条件、库内体检都按文件名解析。唯一的例外是 UHD 蓝光的「UHD.BluRay」（维护者定的写法）。
//   - 洗版判定看的也是补全后的（probeJudgeName）：否则文件名没写 2160p 的原盘按「没分辨率」判，改名后又按 2160p 判，前后不一。

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"115-station/internal/mediaprobe"
	"115-station/internal/model"
)

// mediaProbeCfg setting `media-probe`
type mediaProbeCfg struct {
	Enabled bool `json:"enabled"`
	// Overwrite 为真时读到的一律盖过文件名里写的；默认只补文件名里没有的
	Overwrite bool `json:"overwrite"`
	// EachEpisode 剧集逐集读；默认同一个版本只读一集
	EachEpisode bool `json:"each_episode"`
}

func loadMediaProbeCfg() mediaProbeCfg {
	var c mediaProbeCfg
	if v := modelSettingValue("media-probe"); v != "" {
		json.Unmarshal([]byte(v), &c)
	}
	return c
}

const (
	probeFailRetry   = 7 * 24 * time.Hour // 读失败的过这么久才再试（取直链有成本）
	probeEntryMax    = 30                 // 一个条目最多读几个版本（分组之后）
	probeFailStop    = 3                  // 一轮里连续失败几次就不再读
	probeReqTimeout  = 20 * time.Second
	probeFileTimeout = 60 * time.Second
)

// mediaOverlay 读到的画质信息，挂在 ParsedName 上随整理流水线走（buildRenameContext 叠加）
type mediaOverlay struct {
	Res       ResourceInfo // 只填读到了的字段
	Overwrite bool
	Summary   string // 日志用：2160p · H265.10bit · DV.HDR10 · TrueHD.7.1.ATMOS
}

// apply 叠加到按文件名解析出的 ResourceInfo 上：默认只补空字段
func (o *mediaOverlay) apply(ri ResourceInfo) ResourceInfo {
	if o == nil {
		return ri
	}
	set := func(dst *string, v string) {
		if v != "" && (o.Overwrite || *dst == "") {
			*dst = v
		}
	}
	set(&ri.Pix, o.Res.Pix)
	set(&ri.Effect, o.Res.Effect)
	set(&ri.VideoEncode, o.Res.VideoEncode)
	set(&ri.AudioEncode, o.Res.AudioEncode)
	set(&ri.FPS, o.Res.FPS)
	set(&ri.Type, o.Res.Type)
	return ri
}

// ==================== 读到的 → 文件名写法 ====================

// probeResource 探测结果换成 ParseResourceInfo 的写法。disc 为真时（光盘镜像）主音轨按「最好的一条」挑
func probeResource(info *mediaprobe.Info) ResourceInfo {
	var ri ResourceInfo
	if info == nil {
		return ri
	}
	switch info.Disc {
	case "uhd-bluray":
		ri.Type = "UHD.BluRay"
	case "bluray":
		ri.Type = "BluRay"
	}
	if v := info.Video; v != nil {
		ri.Pix = probePix(v.Width, v.Height)
		ri.Effect = probeEffect(v)
		if c := probeVideoCodec[v.Codec]; c != "" {
			ri.VideoEncode = c
			if v.BitDepth >= 10 {
				ri.VideoEncode += "." + strconv.Itoa(v.BitDepth) + "bit"
			}
		}
		if v.FPS > 0 {
			ri.FPS = strconv.FormatFloat(v.FPS, 'f', -1, 64) + "FPS"
		}
	}
	if a := info.MainAudio(info.Disc == ""); a != nil {
		ri.AudioEncode = probeAudioLabel(a)
	}
	return ri
}

var probeVideoCodec = map[string]string{
	"hevc": "H265", "h264": "H264", "av1": "AV1", "vp9": "VP9", "vc1": "VC1", "mpeg2": "MPEG2",
}

// probePix 按宽度归档（裁过黑边的 1920×800 仍是 1080p，ffprobemeta 同思路），宽度读不到再看高度
func probePix(w, h int) string {
	switch {
	case w >= 6000:
		return "4320p"
	case w >= 3200:
		return "2160p"
	case w >= 2300:
		return "1440p"
	case w >= 1700:
		return "1080p"
	case w >= 1100:
		return "720p"
	case w > 0 || h > 0:
		if h >= 560 || (w >= 704 && h == 0) {
			return "576p"
		}
		return "480p"
	}
	return ""
}

// probeEffect DV / HDR10+ / HDR10 / HLG / SDR。传输特性没读到时：8 bit 的按 SDR，10 bit 以上的不下结论
func probeEffect(v *mediaprobe.Video) string {
	var parts []string
	if v.DV {
		parts = append(parts, "DV")
	}
	switch {
	case v.HDR10Plus:
		parts = append(parts, "HDR10+")
	case v.Transfer == "pq":
		parts = append(parts, "HDR10")
	case v.Transfer == "hlg":
		parts = append(parts, "HLG")
	case v.DV:
		// 档位 5 没有兼容层：只写 DV
	case v.Transfer == "sdr", v.Transfer == "" && v.BitDepth > 0 && v.BitDepth <= 8:
		parts = append(parts, "SDR")
	}
	return strings.Join(parts, ".")
}

// probeAudioLabel TrueHD.7.1.ATMOS / DTS-HD.MA.7.1 / DDP.5.1 …。文件名词表里没有的编码（Opus / MP3）不写
func probeAudioLabel(a *mediaprobe.Audio) string {
	label := map[string]string{
		"truehd": "TrueHD", "eac3": "DDP", "ac3": "DD", "dts": "DTS", "dtshd": "DTS-HD",
		"dtshd_ma": "DTS-HD.MA", "dtsx": "DTS.X", "aac": "AAC", "flac": "FLAC", "lpcm": "LPCM",
	}[a.Codec]
	if label == "" {
		return ""
	}
	if ch := map[int]string{1: "1.0", 2: "2.0", 3: "2.1", 5: "5.0", 6: "5.1", 7: "6.1", 8: "7.1"}[a.Channels]; ch != "" && a.Codec != "dtsx" {
		label += "." + ch
	}
	if a.Atmos {
		label += ".ATMOS"
	}
	return label
}

func probeSummary(ri ResourceInfo) string {
	var parts []string
	for _, v := range []string{ri.Type, ri.Pix, ri.VideoEncode, ri.Effect, ri.AudioEncode, ri.FPS} {
		if v != "" {
			parts = append(parts, v)
		}
	}
	return strings.Join(parts, " · ")
}

// ==================== 要不要读 ====================

// probeVars 命名模板变量 → 对应的 ResourceInfo 字段
var probeVars = []struct {
	tpl   string
	field func(*ResourceInfo) *string
}{
	{"{resource_pix", func(r *ResourceInfo) *string { return &r.Pix }},
	{"{resource_effect", func(r *ResourceInfo) *string { return &r.Effect }},
	{"{video_encode", func(r *ResourceInfo) *string { return &r.VideoEncode }},
	{"{audio_encode", func(r *ResourceInfo) *string { return &r.AudioEncode }},
	{"{fps", func(r *ResourceInfo) *string { return &r.FPS }},
	{"{resource_type", func(r *ResourceInfo) *string { return &r.Type }},
}

// probeTemplates 这类影视用的两条模板（目录模板里也可能写了 {resource_pix}）
func probeTemplates(mediaType string) string {
	if renameTpl == nil {
		return ""
	}
	if mediaType == "movie" {
		return renameTpl.MovieFolder + renameTpl.MovieFile
	}
	return renameTpl.TVFolder + renameTpl.TVFile
}

// needProbe 模板用到了能补的变量、文件名又没写全（覆盖模式只看模板）。资源质量只对 ISO 有意义
func needProbe(name, mediaType string, cfg mediaProbeCfg) bool {
	if !cfg.Enabled || !isProbeable(name) {
		return false
	}
	tpl := probeTemplates(mediaType)
	ri := ParseResourceInfo(name)
	iso := strings.EqualFold(pathExt(name), ".iso")
	for _, v := range probeVars {
		if !strings.Contains(tpl, v.tpl) {
			continue
		}
		if v.tpl == "{resource_type" && !iso {
			continue
		}
		if cfg.Overwrite || *v.field(&ri) == "" {
			return true
		}
	}
	return false
}

// isProbeable 能读的格式（AVI / RMVB / WMV / FLV 这些老格式不读，文件名本来就写了）
func isProbeable(name string) bool {
	switch strings.ToLower(pathExt(name)) {
	case ".mkv", ".webm", ".mp4", ".m4v", ".mov", ".iso", ".ts", ".m2ts", ".mts":
		return true
	}
	return false
}

// ==================== 洗版判定用的名字 ====================

// probeJudgeName 洗版 / 同集多份排名按文件名判，这里把补全后的画质串塞进名字里：
// 插在发布组（结尾的 -GROUP）前面，发布组规则照常命中；覆盖模式先去掉文件名里被盖掉的分辨率 / 编码 / 帧率
func probeJudgeName(name string, ov *mediaOverlay) string {
	if ov == nil {
		return name
	}
	base := ParseResourceInfo(name)
	merged := ov.apply(base)
	var tokens []string
	stem, ext := name, pathExt(name)
	stem = strings.TrimSuffix(stem, ext)
	add := func(old, cur string, re interface{ ReplaceAllString(string, string) string }) {
		if cur == "" || cur == old {
			return
		}
		if old != "" && re != nil {
			stem = re.ReplaceAllString(stem, "")
		}
		tokens = append(tokens, cur)
	}
	add(base.Type, merged.Type, nil)
	add(base.Pix, merged.Pix, rePix)
	add(base.VideoEncode, merged.VideoEncode, reVideo)
	add(base.Effect, merged.Effect, nil)
	add(base.AudioEncode, merged.AudioEncode, nil)
	add(base.FPS, merged.FPS, reFPS)
	if len(tokens) == 0 {
		return name
	}
	ins := "." + strings.Join(tokens, ".")
	if m := reTeam.FindStringIndex(stem); m != nil {
		return stem[:m[0]] + ins + stem[m[0]:] + ext
	}
	return stem + ins + ext
}

// ==================== 读 ====================

// probeSession 一次整理（或一次重新整理）里的读取：缓存、分组、日志
type probeSession struct {
	ops       *pan115Ops
	cfg       mediaProbeCfg
	mediaType string
	onLog     func(string)
	groups    []probeGroup
	byFid     map[string]*mediaOverlay // 同一个文件在一轮里会被问好几次（主视频算目录名、逐集算落点）
	probed    int
	reused    int
	fails     int // 连续失败次数
}

type probeGroup struct {
	key  string
	size int64
	ov   *mediaOverlay
}

func newProbeSession(ops *pan115Ops, mediaType string, onLog func(string)) *probeSession {
	cfg := loadMediaProbeCfg()
	if !cfg.Enabled || ops == nil {
		return nil
	}
	if onLog == nil {
		onLog = func(string) {}
	}
	return &probeSession{ops: ops, cfg: cfg, mediaType: mediaType, onLog: onLog}
}

// overlayFor 一个视频的画质补全（不需要 / 读不到返回 nil）。name 是判断「文件名写没写」用的名字
// （重新整理传原始文件名，资源变量一向按原名算）
func (s *probeSession) overlayFor(f remoteFile, name string) *mediaOverlay {
	if s == nil || f.PickCode == "" || !needProbe(name, s.mediaType, s.cfg) {
		return nil
	}
	if ov, ok := s.byFid[f.Fid]; ok {
		return ov
	}
	ov := s.lookup(f, name)
	if s.byFid == nil {
		s.byFid = map[string]*mediaOverlay{}
	}
	s.byFid[f.Fid] = ov
	return ov
}

func (s *probeSession) lookup(f remoteFile, name string) *mediaOverlay {
	key := probeGroupKey(f, name)
	if !s.cfg.EachEpisode && s.mediaType == "tv" {
		for _, g := range s.groups {
			if g.key == key && sizeNear(g.size, f.Size) {
				s.reused++
				return g.ov
			}
		}
	}
	// 连着失败几次多半是 CDN 连不上或被风控：这一轮别再试，每次都要等超时，整理一直占着 taskMu
	if s.probed >= probeEntryMax || s.fails >= probeFailStop {
		return nil
	}
	s.probed++
	info, err := probeFileFn(s.ops, f, name)
	if err != nil {
		if !errors.Is(err, errProbeRecentFail) { // 查缓存得知的失败没发请求，不算
			s.fails++
		}
		if s.fails == probeFailStop {
			s.onLog(fmt.Sprintf("○ 读取媒体信息连续失败 %d 次，这一轮余下的按文件名整理", probeFailStop))
		}
		s.onLog(fmt.Sprintf("○ 读取媒体信息失败 %s: %v（按文件名整理）", shortLogName(name), err))
		s.groups = append(s.groups, probeGroup{key: key, size: f.Size}) // 同一版本的其他集也别再读
		return nil
	}
	s.fails = 0
	ri := probeResource(info)
	ov := &mediaOverlay{Res: ri, Overwrite: s.cfg.Overwrite, Summary: probeSummary(ri)}
	if ov.Summary == "" {
		ov = nil
	} else {
		s.onLog(fmt.Sprintf("✦ 读取媒体信息 %s: %s", shortLogName(name), ov.Summary))
	}
	s.groups = append(s.groups, probeGroup{key: key, size: f.Size, ov: ov})
	return ov
}

// redoProbeHook 重新整理 / 指定季集用的读取：没开读取媒体信息时返回 nil
func redoProbeHook(ops *pan115Ops, mediaType string) func(orgRecordFile, string) *mediaOverlay {
	s := newProbeSession(ops, mediaType, func(m string) { log.Printf("[整理] %s", m) })
	if s == nil {
		return nil
	}
	return func(f orgRecordFile, orig string) *mediaOverlay {
		return s.overlayFor(remoteFile{Fid: f.Fid, Name: f.Name, Path: f.Dir, Size: f.Size, PickCode: f.PickCode, Sha1: f.Sha1}, orig)
	}
}

// cachedProbeHook 只查缓存、不发请求的版本（纯判定 / 预览用）
func cachedProbeHook(mediaType string) func(orgRecordFile, string) *mediaOverlay {
	cfg := loadMediaProbeCfg()
	if !cfg.Enabled || model.DB == nil {
		return nil
	}
	return func(f orgRecordFile, orig string) *mediaOverlay {
		if !needProbe(orig, mediaType, cfg) {
			return nil
		}
		info, ok := cachedProbe(probeKey(remoteFile{PickCode: f.PickCode, Sha1: f.Sha1}))
		if !ok || info == nil {
			return nil
		}
		ri := probeResource(info)
		if s := probeSummary(ri); s != "" {
			return &mediaOverlay{Res: ri, Overwrite: cfg.Overwrite, Summary: s}
		}
		return nil
	}
}

// done 收尾日志：同版本沿用了几集
func (s *probeSession) done() {
	if s != nil && s.reused > 0 {
		s.onLog(fmt.Sprintf("○ 读取媒体信息：同一版本的其余 %d 集沿用读到的结果", s.reused))
		s.reused = 0
	}
}

// probeGroupKey 同一个版本：同一个目录、文件名里的画质串与发布组一样、扩展名一样
func probeGroupKey(f remoteFile, name string) string {
	ri := ParseResourceInfo(name)
	return strings.Join([]string{f.Path, strings.ToLower(pathExt(name)), ri.Pix, ri.Type, ri.Source, ri.Effect,
		ri.VideoEncode, ri.AudioEncode, ri.Team}, "|")
}

// sizeNear 体积在 0.6–1.6 倍之间：文件名一个画质词都没写时，同目录里的 4K 版与 1080p 版靠体积分开
func sizeNear(a, b int64) bool {
	if a <= 0 || b <= 0 {
		return a == b
	}
	r := float64(b) / float64(a)
	return r >= 0.6 && r <= 1.6
}

func probeKey(f remoteFile) string {
	if f.Sha1 != "" {
		return strings.ToUpper(f.Sha1)
	}
	return "pc:" + f.PickCode
}

// errProbeRecentFail 近期读过、失败了，还不到重试的时候
var errProbeRecentFail = errors.New("近期读过失败，暂不重试")

// probeFileFn 测试替换成不碰网络的
var probeFileFn = probeFile

// probeFile 读一个文件：先查缓存，没有才取直链读
func probeFile(ops *pan115Ops, f remoteFile, name string) (*mediaprobe.Info, error) {
	key := probeKey(f)
	if info, ok := cachedProbe(key); ok {
		if info != nil {
			return info, nil
		}
		return nil, errProbeRecentFail
	}
	info, st, err := probeRemote(ops, f, name)
	row := model.MediaProbe{Key: key, Name: truncateStr(name, 500), Blocks: st.Requests, Bytes: st.Bytes, ProbedAt: time.Now()}
	if err != nil {
		row.Err = truncateStr(err.Error(), 250)
	} else if b, mErr := json.Marshal(info); mErr == nil {
		row.Info = string(b)
	}
	if model.DB != nil {
		model.DB.Save(&row)
	}
	if err == nil {
		log.Printf("[探测] ✓ %s：%s（%d 次请求、%d KB）", shortLogName(name), probeSummary(probeResource(info)), st.Requests, st.Bytes>>10)
	} else {
		log.Printf("[探测] ✗ %s：%v（%d 次请求）", shortLogName(name), err, st.Requests)
	}
	return info, err
}

// cachedProbe 查缓存。ok 为真：成功的结果（info 非 nil），或近期失败过、还不到重试的时候（info 为 nil）
func cachedProbe(key string) (*mediaprobe.Info, bool) {
	if model.DB == nil {
		return nil, false
	}
	var row model.MediaProbe
	if model.DB.Where(model.MediaProbe{Key: key}).Limit(1).Find(&row).Error != nil || row.Key == "" {
		return nil, false
	}
	if row.Info != "" {
		var info mediaprobe.Info
		if json.Unmarshal([]byte(row.Info), &info) == nil {
			return &info, true
		}
		return nil, false
	}
	return nil, time.Since(row.ProbedAt) < probeFailRetry
}

// probeRemote 取直链、按块读
func probeRemote(ops *pan115Ops, f remoteFile, name string) (*mediaprobe.Info, mediaprobe.Stats, error) {
	if f.Size <= 0 {
		return nil, mediaprobe.Stats{}, errors.New("文件大小未知")
	}
	u, headers, err := ops.downloadURLFull(f.PickCode, "")
	if err != nil {
		return nil, mediaprobe.Stats{}, fmt.Errorf("取直链失败: %w", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), probeFileTimeout)
	defer cancel()
	src := &dlRangeReader{ctx: ctx, url: u, headers: headers, loginCookie: ops.cookieForDL()}
	return mediaprobe.Probe(src, f.Size, name, mediaprobe.DefaultLimits)
}

// dlRangeReader 用 115 直链实现 io.ReaderAt：每次一个 Range 请求。
// 直链绑定签发时的 UA；Cookie 的组合照 downloadAssetBytes 的顺序试，试出能用的就一直用它
type dlRangeReader struct {
	ctx         context.Context
	url         string
	headers     map[string]string
	loginCookie string

	once    sync.Once
	cookies []string
	pick    int // 用 cookies 里的哪一个；-1 = 还没试出来
}

var probeHTTP = &http.Client{Timeout: probeReqTimeout}

func (d *dlRangeReader) ReadAt(p []byte, off int64) (int, error) {
	d.once.Do(func() {
		set := d.headers["Cookie"]
		combined := strings.Trim(set+"; "+d.loginCookie, "; ")
		for _, c := range []string{set, "", combined, d.loginCookie} {
			if !containsStr(d.cookies, c) {
				d.cookies = append(d.cookies, c)
			}
		}
		d.pick = -1
	})
	try := d.cookies
	if d.pick >= 0 {
		try = []string{d.cookies[d.pick]}
	}
	var lastErr error
	for i, c := range try {
		n, status, err := d.get(p, off, c)
		if err == nil || (errors.Is(err, io.EOF) && n > 0) {
			if d.pick < 0 {
				d.pick = i
			}
			return n, err
		}
		lastErr = err
		if status != http.StatusForbidden {
			break
		}
	}
	return 0, lastErr
}

func (d *dlRangeReader) get(p []byte, off int64, cookie string) (int, int, error) {
	req, err := http.NewRequestWithContext(d.ctx, http.MethodGet, d.url, nil)
	if err != nil {
		return 0, 0, err
	}
	ua := d.headers["User-Agent"]
	if ua == "" {
		ua = ua115Unified()
	}
	req.Header.Set("User-Agent", ua)
	if cookie != "" {
		req.Header.Set("Cookie", cookie)
	}
	req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", off, off+int64(len(p))-1))
	resp, err := probeHTTP.Do(req)
	if err != nil {
		return 0, 0, err
	}
	defer resp.Body.Close()
	switch resp.StatusCode {
	case http.StatusPartialContent:
	case http.StatusOK:
		// 不认 Range 的话整份文件都会传过来：只在从头读时接受，读够就关
		if off != 0 {
			return 0, resp.StatusCode, errors.New("直链不支持分段读取")
		}
	default:
		return 0, resp.StatusCode, fmt.Errorf("直链返回 HTTP %d", resp.StatusCode)
	}
	n, err := io.ReadFull(resp.Body, p)
	if err == io.ErrUnexpectedEOF || err == io.EOF {
		if n > 0 {
			return n, resp.StatusCode, io.EOF
		}
	}
	return n, resp.StatusCode, err
}
