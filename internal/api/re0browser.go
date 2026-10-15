package api

// ==================== RE0 网页通道：cloakbrowser 的下载、校验与启动 ====================
//
// RE0 停止开放 OpenAPI 申请（2026-10），新用户只能走网页：用账号密码在浏览器里真实登录，
// 再在页面上签到 / 查资源 / 解锁（站点做了加密会话绑定，接口不能脱离浏览器单独调）。
// 思路参考 p115strmhelper `helper/hdhive/browser.py`（只读）。
//
// 浏览器用 CloakBrowser（C++ 源码层改指纹的 Chromium，过 Cloudflare 比注入脚本稳），
// 二进制授权是「free to use, no redistribution」，所以**不打进镜像**：用户启用 RE0 网页登录时
// 由本站从官方地址下载到数据目录。下载 / 验签 / 启动参数对照它的 MIT 包装层
// （npm cloakbrowser 0.6.2 的 config.js / download.js / args.js）用 Go 重写：
//
//   - 下载：GitHub Releases `chromium-v{版本}/cloakbrowser-linux-{x64|arm64}.tar.gz`，备用 cloakbrowser.dev 同名路径
//   - 校验：同目录 SHA256SUMS + SHA256SUMS.sig（Ed25519，公钥写死在下面），manifest 的 version= 行必须等于请求的版本
//   - 启动：--fingerprint=<种子> --fingerprint-platform=windows，去掉 --enable-automation
//
// 不反复下载（维护者要求，200MB 一次）：装好后写 .verified 标记，启动 / 保存配置时先看标记；
// 下载中断留 .part，下次带 Range 续传；版本号写死，镜像升级换了版本才会再下一次，旧版本目录随后删掉。
// 访问 GitHub 默认走全局代理，配了 GitHub Token 时走 API 下载（匿名下载容易被限流）。

import (
	"archive/tar"
	"compress/gzip"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math/rand"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
)

const (
	// 与 npm cloakbrowser 0.6.2 的 PLATFORM_CHROMIUM_VERSIONS 一致；升级时两个都要核对 GitHub 上真有这个 release
	cloakVersionX64   = "146.0.7680.177.5"
	cloakVersionArm64 = "146.0.7680.177.3"
	// 官方 SHA256SUMS 的 Ed25519 签名公钥（config.js 的 BINARY_SIGNING_PUBKEYS）
	cloakSigningKey = "MKFKwIhUcKWq5xTuNA0Ovg99njcDEcEJvmWYYhApvaU="

	cloakGithubRepo     = "CloakHQ/cloakbrowser"
	cloakGithubDownload = "https://github.com/CloakHQ/cloakbrowser/releases/download"
	cloakMirrorDownload = "https://cloakbrowser.dev"
)

// 状态：missing 没装 / downloading / verifying / extracting / ready / error / unsupported（平台没有预编译包）
type cloakStatus struct {
	State     string `json:"state"`
	Version   string `json:"version"`
	Platform  string `json:"platform"`
	Path      string `json:"path,omitempty"`
	Done      int64  `json:"done"`
	Total     int64  `json:"total"`
	Speed     int64  `json:"speed"` // 字节 / 秒
	Source    string `json:"source,omitempty"`
	Error     string `json:"error,omitempty"`
	Reason    string `json:"reason,omitempty"` // 这次下载是谁触发的
	UpdatedAt int64  `json:"updated_at"`
}

var (
	cloakRoot string // <DATA_DIR>/cloakbrowser，re0BrowserInit 设

	cloakMu    sync.Mutex
	cloakState = cloakStatus{State: "missing"}
	cloakBusy  bool
)

// cloakPlatform 当前平台的包名后缀；只有 Linux 的 x64 / arm64 有预编译包（容器里只会是这两种）
func cloakPlatform() (string, bool) {
	if runtime.GOOS != "linux" {
		return runtime.GOOS + "-" + runtime.GOARCH, false
	}
	switch runtime.GOARCH {
	case "amd64":
		return "linux-x64", true
	case "arm64":
		return "linux-arm64", true
	}
	return "linux-" + runtime.GOARCH, false
}

var cloakVersionRe = regexp.MustCompile(`^[0-9]+(?:\.[0-9]+){3,4}$`)

// cloakVersion 要用的版本：环境变量 CLOAKBROWSER_VERSION 可以钉别的版本（同官方包装层），否则按平台写死的
func cloakVersion() string {
	if v := strings.TrimSpace(os.Getenv("CLOAKBROWSER_VERSION")); cloakVersionRe.MatchString(v) {
		return v
	}
	if runtime.GOARCH == "arm64" {
		return cloakVersionArm64
	}
	return cloakVersionX64
}

func cloakBinDir(v string) string  { return filepath.Join(cloakRoot, "chromium-"+v) }
func cloakBinPath(v string) string { return filepath.Join(cloakBinDir(v), "chrome") }
func cloakMarker(v string) string  { return filepath.Join(cloakBinDir(v), ".verified") }
func cloakArchive(tag string) string {
	return "cloakbrowser-" + tag + ".tar.gz"
}

// cloakInstalled 本地有没有装好（校验通过的标记 + 可执行文件都在）。只看本地，不发请求
func cloakInstalled() (string, bool) {
	// 自己指定浏览器（官方包装层同名环境变量）：调试或自备浏览器时用，不下载
	if p := strings.TrimSpace(os.Getenv("CLOAKBROWSER_BINARY_PATH")); p != "" {
		if st, err := os.Stat(p); err == nil && !st.IsDir() {
			return p, true
		}
		return p, false
	}
	if cloakRoot == "" {
		return "", false
	}
	v := cloakVersion()
	bin := cloakBinPath(v)
	if _, err := os.Stat(cloakMarker(v)); err != nil {
		return bin, false
	}
	st, err := os.Stat(bin)
	if err != nil || st.IsDir() || st.Mode()&0o111 == 0 {
		return bin, false
	}
	return bin, true
}

// cloakSnapshot 当前状态（给接口与日志）
func cloakSnapshot() cloakStatus {
	cloakMu.Lock()
	s := cloakState
	busy := cloakBusy
	cloakMu.Unlock()
	tag, ok := cloakPlatform()
	s.Platform, s.Version = tag, cloakVersion()
	if !ok && os.Getenv("CLOAKBROWSER_BINARY_PATH") == "" {
		s.State = "unsupported"
		s.Error = "这个平台没有 CloakBrowser 预编译包（只有 Linux x64 / arm64），请用官方 Docker 镜像运行"
		return s
	}
	if !busy {
		if bin, ok := cloakInstalled(); ok {
			s.State, s.Path = "ready", bin
		} else if s.State == "ready" || s.State == "" {
			s.State = "missing"
		}
	}
	return s
}

func cloakSet(fn func(s *cloakStatus)) {
	cloakMu.Lock()
	fn(&cloakState)
	cloakState.UpdatedAt = time.Now().Unix()
	cloakMu.Unlock()
}

// re0BrowserInit 启动时调：记下数据目录、清掉上次没解压完的临时目录、报告本地状态；
// 配了网页登录而本地没装时在后台下载
func re0BrowserInit(dataDir string) {
	cloakRoot = filepath.Join(dataDir, "cloakbrowser")
	if ents, err := os.ReadDir(cloakRoot); err == nil {
		for _, e := range ents {
			if e.IsDir() && strings.HasSuffix(e.Name(), ".tmp") {
				os.RemoveAll(filepath.Join(cloakRoot, e.Name()))
			}
		}
	}
	tag, ok := cloakPlatform()
	if !ok {
		if re0WebConfigured(loadRe0Cfg()) || dianConfigured(loadDianCfg()) {
			log.Printf("[RE0浏览器] ✗ 平台 %s 没有 CloakBrowser 预编译包，RE0 网页登录与癫影不可用", tag)
		}
		return
	}
	if bin, ok := cloakInstalled(); ok {
		log.Printf("[RE0浏览器] ✓ 本地已有 CloakBrowser %s（%s），不需要下载", cloakVersion(), bin)
		cloakPruneOld(cloakVersion())
		return
	}
	if !re0WebConfigured(loadRe0Cfg()) && !dianConfigured(loadDianCfg()) {
		log.Printf("[RE0浏览器] ○ 本地没有 CloakBrowser %s；未配置 RE0 网页登录与癫影，暂不下载", cloakVersion())
		return
	}
	ensureCloak("启动时检查：已配置 RE0 网页登录或癫影、本地没有浏览器")
}

// ensureCloak 本地没有就在后台下载，已装好 / 正在下载时什么都不做（幂等，随便调）。返回是否已经可用
func ensureCloak(reason string) bool {
	if cloakRoot == "" {
		return false
	}
	if _, ok := cloakPlatform(); !ok {
		return false
	}
	if _, ok := cloakInstalled(); ok {
		return true
	}
	cloakMu.Lock()
	if cloakBusy {
		cloakMu.Unlock()
		return false
	}
	cloakBusy = true
	cloakState = cloakStatus{State: "downloading", Reason: reason, UpdatedAt: time.Now().Unix()}
	cloakMu.Unlock()
	go func() {
		defer func() {
			if r := recover(); r != nil {
				cloakSet(func(s *cloakStatus) { s.State, s.Error = "error", fmt.Sprint(r) })
				log.Printf("[RE0浏览器] ✗ 下载过程异常: %v", r)
			}
			cloakMu.Lock()
			cloakBusy = false
			cloakMu.Unlock()
		}()
		log.Printf("[RE0浏览器] ⇣ 开始准备 CloakBrowser %s（%s）", cloakVersion(), reason)
		start := time.Now()
		if err := cloakInstall(); err != nil {
			cloakSet(func(s *cloakStatus) { s.State, s.Error = "error", err.Error() })
			log.Printf("[RE0浏览器] ✗ CloakBrowser 准备失败（用时 %s）: %v", time.Since(start).Round(time.Second), err)
			return
		}
		bin, _ := cloakInstalled()
		cloakSet(func(s *cloakStatus) { s.State, s.Path, s.Error = "ready", bin, "" })
		log.Printf("[RE0浏览器] ✓ CloakBrowser %s 已就绪（%s，用时 %s）", cloakVersion(), bin, time.Since(start).Round(time.Second))
		cloakPruneOld(cloakVersion())
	}()
	return false
}

// cloakReinstall 删掉本地的再下（界面「重新下载」，浏览器坏了时用）。在用的浏览器先关掉
func cloakReinstall(reason string) error {
	cloakMu.Lock()
	busy := cloakBusy
	cloakMu.Unlock()
	if busy {
		return fmt.Errorf("正在下载中")
	}
	re0CloseBrowser("重新下载浏览器")
	v := cloakVersion()
	if err := os.RemoveAll(cloakBinDir(v)); err != nil {
		return fmt.Errorf("删除旧浏览器失败: %v", err)
	}
	log.Printf("[RE0浏览器] ○ 已删除本地 CloakBrowser %s，重新下载", v)
	ensureCloak(reason)
	return nil
}

// cloakPruneOld 删掉别的版本（镜像升级换了版本号后，旧的 200MB 留着没用）
func cloakPruneOld(keep string) {
	ents, err := os.ReadDir(cloakRoot)
	if err != nil {
		return
	}
	for _, e := range ents {
		name := e.Name()
		if !e.IsDir() || !strings.HasPrefix(name, "chromium-") || name == "chromium-"+keep {
			continue
		}
		if err := os.RemoveAll(filepath.Join(cloakRoot, name)); err == nil {
			log.Printf("[RE0浏览器] ○ 删除旧版本浏览器 %s", name)
		}
	}
	// 别的版本留下的半截下载
	for _, e := range ents {
		name := e.Name()
		if !e.IsDir() && strings.HasSuffix(name, ".part") && !strings.Contains(name, keep) {
			os.Remove(filepath.Join(cloakRoot, name))
		}
	}
}

// ==================== 下载 ====================

// cloakSource 一个下载来源：manifest 与包必须出自同一来源（签名覆盖的是那份 manifest 的原始字节）
type cloakSource struct {
	Name        string
	SumsURL     string
	SigURL      string
	ArchiveURL  string
	ArchiveSize int64
	viaAPI      bool // GitHub API 资产地址：要带 Accept: application/octet-stream
}

func cloakInstall() error {
	tag, _ := cloakPlatform()
	v := cloakVersion()
	if err := os.MkdirAll(cloakRoot, 0o755); err != nil {
		return fmt.Errorf("建目录失败: %v", err)
	}
	client := githubHTTPClient(0)
	sources := cloakSources(client, v, tag)
	var errs []string
	for _, src := range sources {
		log.Printf("[RE0浏览器] 尝试来源：%s", src.Name)
		cloakSet(func(s *cloakStatus) { s.Source, s.Error = src.Name, "" })
		err := cloakInstallFrom(client, src, v, tag)
		if err == nil {
			return nil
		}
		log.Printf("[RE0浏览器] ✗ 来源 %s 失败: %v", src.Name, err)
		errs = append(errs, src.Name+"："+err.Error())
	}
	return errors.New(strings.Join(errs, "；"))
}

// cloakSources 下载来源，按顺序试：GitHub API（配了 Token 时，限额高）→ GitHub 直链 → cloakbrowser.dev
func cloakSources(client *http.Client, v, tag string) []cloakSource {
	archive := cloakArchive(tag)
	direct := cloakGithubDownload + "/chromium-v" + v
	mirror := cloakMirrorDownload + "/chromium-v" + v
	var out []cloakSource
	if githubToken() != "" {
		if src, err := cloakAPISource(client, v, archive); err == nil {
			out = append(out, src)
		} else {
			log.Printf("[RE0浏览器] ○ 用 GitHub API 查 release 失败，改用直链: %v", err)
		}
	}
	out = append(out,
		cloakSource{Name: "GitHub Releases", SumsURL: direct + "/SHA256SUMS", SigURL: direct + "/SHA256SUMS.sig", ArchiveURL: direct + "/" + archive},
		cloakSource{Name: "cloakbrowser.dev", SumsURL: mirror + "/SHA256SUMS", SigURL: mirror + "/SHA256SUMS.sig", ArchiveURL: mirror + "/" + archive},
	)
	return out
}

// cloakAPISource 用 GitHub API 读 release 的资产地址（带 Token 下载走 API 地址，不受匿名下载的限流）
func cloakAPISource(client *http.Client, v, archive string) (cloakSource, error) {
	req, _ := http.NewRequest(http.MethodGet, "https://api.github.com/repos/"+cloakGithubRepo+"/releases/tags/chromium-v"+v, nil)
	githubAuth(req)
	req.Header.Set("Accept", "application/vnd.github+json")
	resp, err := client.Do(req)
	if err != nil {
		return cloakSource{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 300))
		return cloakSource{}, fmt.Errorf("HTTP %d %s", resp.StatusCode, strings.TrimSpace(string(b)))
	}
	var rel struct {
		Assets []struct {
			Name string `json:"name"`
			URL  string `json:"url"`
			Size int64  `json:"size"`
		} `json:"assets"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&rel); err != nil {
		return cloakSource{}, err
	}
	src := cloakSource{Name: "GitHub API（Token）", viaAPI: true}
	for _, a := range rel.Assets {
		switch a.Name {
		case "SHA256SUMS":
			src.SumsURL = a.URL
		case "SHA256SUMS.sig":
			src.SigURL = a.URL
		case archive:
			src.ArchiveURL, src.ArchiveSize = a.URL, a.Size
		}
	}
	if src.SumsURL == "" || src.SigURL == "" || src.ArchiveURL == "" {
		return cloakSource{}, fmt.Errorf("release chromium-v%s 里没有 %s 或签名文件", v, archive)
	}
	return src, nil
}

func cloakInstallFrom(client *http.Client, src cloakSource, v, tag string) error {
	// 1. 先拿签名过的 manifest：验不过就不下 200MB
	sums, err := cloakFetchSmall(client, src, src.SumsURL)
	if err != nil {
		return fmt.Errorf("读 SHA256SUMS 失败: %v", err)
	}
	sig, err := cloakFetchSmall(client, src, src.SigURL)
	if err != nil {
		return fmt.Errorf("读 SHA256SUMS.sig 失败: %v", err)
	}
	if err := cloakVerifySignature(sums, sig); err != nil {
		return err
	}
	text := string(sums)
	if got := cloakManifestVersion(text); got != v {
		return fmt.Errorf("签名清单声明的版本是 %q，要的是 %s，拒绝使用（可能是降级攻击）", got, v)
	}
	archive := cloakArchive(tag)
	want := cloakParseChecksums(text)[archive]
	if want == "" {
		return fmt.Errorf("签名清单里没有 %s", archive)
	}
	log.Printf("[RE0浏览器] ✓ SHA256SUMS 签名校验通过（Ed25519），%s 期望 sha256=%s…", archive, want[:16])

	// 2. 下载（断点续传）
	part := filepath.Join(cloakRoot, "_download-"+v+"-"+tag+".tar.gz.part")
	if err := cloakDownload(client, src, part); err != nil {
		return err
	}

	// 3. 核对 sha256
	cloakSet(func(s *cloakStatus) { s.State = "verifying" })
	got, err := sha256File(part)
	if err != nil {
		return err
	}
	if got != want {
		os.Remove(part) // 下坏了：留着续传只会一直坏
		return fmt.Errorf("sha256 不符（下载到 %s…，期望 %s…），已删除，下次重新下载", got[:16], want[:16])
	}
	log.Printf("[RE0浏览器] ✓ sha256 校验通过")

	// 4. 解压到临时目录，成功了再改名，避免半截目录被当成装好
	cloakSet(func(s *cloakStatus) { s.State = "extracting" })
	dst := cloakBinDir(v)
	tmp := dst + ".tmp"
	os.RemoveAll(tmp)
	n, err := cloakExtract(part, tmp)
	if err != nil {
		os.RemoveAll(tmp)
		return fmt.Errorf("解压失败: %v", err)
	}
	cloakFlatten(tmp)
	bin := filepath.Join(tmp, "chrome")
	if _, err := os.Stat(bin); err != nil {
		os.RemoveAll(tmp)
		return fmt.Errorf("解压后没找到 chrome 可执行文件")
	}
	os.Chmod(bin, 0o755)
	os.RemoveAll(dst)
	if err := os.Rename(tmp, dst); err != nil {
		os.RemoveAll(tmp)
		return fmt.Errorf("安装失败: %v", err)
	}
	if err := os.WriteFile(cloakMarker(v), []byte(v+" "+want+"\n"), 0o644); err != nil {
		return fmt.Errorf("写校验标记失败: %v", err)
	}
	os.Remove(part)
	log.Printf("[RE0浏览器] ✓ 解压完成：%d 个文件 → %s", n, dst)
	return nil
}

func cloakFetchSmall(client *http.Client, src cloakSource, u string) ([]byte, error) {
	req, _ := http.NewRequest(http.MethodGet, u, nil)
	cloakReqHeaders(req, src)
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return io.ReadAll(io.LimitReader(resp.Body, 64<<10))
}

func cloakReqHeaders(req *http.Request, src cloakSource) {
	req.Header.Set("User-Agent", "115-station/"+buildVersion)
	if src.viaAPI {
		githubAuth(req)
		req.Header.Set("Accept", "application/octet-stream")
	} else if strings.HasPrefix(req.URL.Host, "github.com") {
		githubAuth(req)
	}
}

// cloakDownload 下到 part 文件，已有的部分用 Range 续上（服务器不认 Range 就从头来）
func cloakDownload(client *http.Client, src cloakSource, part string) error {
	var have int64
	if st, err := os.Stat(part); err == nil {
		have = st.Size()
	}
	req, _ := http.NewRequest(http.MethodGet, src.ArchiveURL, nil)
	cloakReqHeaders(req, src)
	if have > 0 {
		req.Header.Set("Range", "bytes="+strconv.FormatInt(have, 10)+"-")
	}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("连接下载地址失败: %v", err)
	}
	defer resp.Body.Close()
	flag := os.O_CREATE | os.O_WRONLY
	switch {
	case resp.StatusCode == http.StatusPartialContent && have > 0:
		flag |= os.O_APPEND
		log.Printf("[RE0浏览器] ↻ 断点续传：已有 %s，从这里接着下", formatBytes(have))
	case resp.StatusCode == http.StatusRequestedRangeNotSatisfiable && have > 0:
		// 已经下全了（上次下完没来得及校验就退出）
		log.Printf("[RE0浏览器] ○ 本地已有完整的下载包（%s），直接校验", formatBytes(have))
		return nil
	case resp.StatusCode == http.StatusOK:
		flag |= os.O_TRUNC
		if have > 0 {
			log.Printf("[RE0浏览器] ○ 服务器不支持续传，从头下载")
		}
		have = 0
	default:
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 300))
		return fmt.Errorf("下载返回 HTTP %d %s", resp.StatusCode, strings.TrimSpace(string(b)))
	}
	total := have + resp.ContentLength
	if resp.ContentLength <= 0 {
		total = src.ArchiveSize
	}
	f, err := os.OpenFile(part, flag, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	log.Printf("[RE0浏览器] ⇣ 下载 %s（共 %s）", req.URL.Redacted(), formatBytes(total))
	cloakSet(func(s *cloakStatus) { s.State, s.Done, s.Total, s.Speed = "downloading", have, total, 0 })

	buf := make([]byte, 256<<10)
	done := have
	lastLog, lastAt, lastBytes := -1, time.Now(), done
	nextPct := 0
	for {
		n, rerr := resp.Body.Read(buf)
		if n > 0 {
			if _, werr := f.Write(buf[:n]); werr != nil {
				return fmt.Errorf("写入失败（磁盘满了？）: %v", werr)
			}
			done += int64(n)
		}
		if now := time.Now(); now.Sub(lastAt) >= time.Second || rerr != nil {
			speed := int64(float64(done-lastBytes) / now.Sub(lastAt).Seconds())
			lastAt, lastBytes = now, done
			cloakSet(func(s *cloakStatus) { s.Done, s.Speed = done, speed })
			if total > 0 {
				pct := int(done * 100 / total)
				if pct >= nextPct && pct != lastLog {
					log.Printf("[RE0浏览器] ⇣ %d%%（%s / %s，%s/s）", pct, formatBytes(done), formatBytes(total), formatBytes(speed))
					lastLog = pct
					nextPct = pct/10*10 + 10
				}
			}
		}
		if rerr == io.EOF {
			break
		}
		if rerr != nil {
			return fmt.Errorf("下载中断（已下 %s，下次接着下）: %v", formatBytes(done), rerr)
		}
	}
	if total > 0 && done < total {
		return fmt.Errorf("下载不完整（%s / %s，下次接着下）", formatBytes(done), formatBytes(total))
	}
	log.Printf("[RE0浏览器] ✓ 下载完成 %s", formatBytes(done))
	return nil
}

// cloakVerifySignature 用写死的公钥核 SHA256SUMS 的 Ed25519 签名（.sig 是 base64 文本）
func cloakVerifySignature(manifest, sigB64 []byte) error {
	sig, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(sigB64)))
	if err != nil || len(sig) != ed25519.SignatureSize {
		return fmt.Errorf("SHA256SUMS.sig 格式不对")
	}
	key, err := base64.StdEncoding.DecodeString(cloakSigningKey)
	if err != nil || len(key) != ed25519.PublicKeySize {
		return fmt.Errorf("内置公钥无效")
	}
	if !ed25519.Verify(ed25519.PublicKey(key), manifest, sig) {
		return fmt.Errorf("SHA256SUMS 签名校验失败，拒绝使用这个来源的文件")
	}
	return nil
}

func cloakManifestVersion(text string) string {
	for _, line := range strings.Split(text, "\n") {
		if v, ok := strings.CutPrefix(strings.TrimSpace(line), "version="); ok {
			return strings.TrimSpace(v)
		}
	}
	return ""
}

var cloakSumLineRe = regexp.MustCompile(`(?i)^([a-f0-9]{64})\s+\*?(.+)$`)

func cloakParseChecksums(text string) map[string]string {
	out := map[string]string{}
	for _, line := range strings.Split(text, "\n") {
		if m := cloakSumLineRe.FindStringSubmatch(strings.TrimSpace(line)); m != nil {
			out[strings.TrimSpace(m[2])] = strings.ToLower(m[1])
		}
	}
	return out
}

func sha256File(p string) (string, error) {
	f, err := os.Open(p)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// cloakExtract 解 tar.gz。绝对路径与带 .. 的条目跳过（同官方包装层），符号链接只认指向包内的
func cloakExtract(archive, dst string) (int, error) {
	f, err := os.Open(archive)
	if err != nil {
		return 0, err
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		return 0, err
	}
	defer gz.Close()
	tr := tar.NewReader(gz)
	n := 0
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			return n, nil
		}
		if err != nil {
			return n, err
		}
		name := filepath.Clean(hdr.Name)
		if filepath.IsAbs(hdr.Name) || name == ".." || strings.HasPrefix(name, ".."+string(filepath.Separator)) {
			log.Printf("[RE0浏览器] ○ 跳过可疑的包内路径: %s", hdr.Name)
			continue
		}
		target := filepath.Join(dst, name)
		switch hdr.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(target, 0o755); err != nil {
				return n, err
			}
		case tar.TypeReg:
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return n, err
			}
			out, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, os.FileMode(hdr.Mode)&0o777|0o600)
			if err != nil {
				return n, err
			}
			if _, err := io.Copy(out, tr); err != nil {
				out.Close()
				return n, err
			}
			out.Close()
			n++
		case tar.TypeSymlink:
			link := filepath.Clean(filepath.Join(filepath.Dir(name), hdr.Linkname))
			if filepath.IsAbs(hdr.Linkname) || strings.HasPrefix(link, "..") {
				log.Printf("[RE0浏览器] ○ 跳过指向包外的链接: %s → %s", hdr.Name, hdr.Linkname)
				continue
			}
			os.MkdirAll(filepath.Dir(target), 0o755)
			os.Remove(target)
			if err := os.Symlink(hdr.Linkname, target); err != nil {
				return n, err
			}
		}
	}
}

// cloakFlatten 包里只有一个顶层目录时上提一层
func cloakFlatten(dir string) {
	ents, err := os.ReadDir(dir)
	if err != nil || len(ents) != 1 || !ents[0].IsDir() {
		return
	}
	sub := filepath.Join(dir, ents[0].Name())
	kids, err := os.ReadDir(sub)
	if err != nil {
		return
	}
	for _, k := range kids {
		os.Rename(filepath.Join(sub, k.Name()), filepath.Join(dir, k.Name()))
	}
	os.Remove(sub)
}

// cloakSeed 指纹种子：持久化，重启浏览器不换。RE0 的会话绑定很可能连着指纹，换了就得重新登录
func cloakSeed() string {
	p := filepath.Join(cloakRoot, "fingerprint-seed")
	if b, err := os.ReadFile(p); err == nil {
		if s := strings.TrimSpace(string(b)); s != "" {
			return s
		}
	}
	s := strconv.Itoa(10000 + rand.Intn(90000))
	os.MkdirAll(cloakRoot, 0o755)
	os.WriteFile(p, []byte(s), 0o644)
	return s
}

func cloakProfileDir() string { return filepath.Join(cloakRoot, "profile") }

// ==================== HTTP 接口 ====================

// Re0BrowserStatus GET /re0/browser —— 浏览器状态与下载进度（只看本地，不发请求）
func (h *Handler) Re0BrowserStatus(c *gin.Context) {
	re0BrMu.Lock()
	running := re0Br != nil
	re0BrMu.Unlock()
	out := gin.H{"browser": cloakSnapshot(), "running": running, "channel": re0Channel(loadRe0Cfg())}
	if op := re0OpSnapshot(); op != nil {
		out["op"] = op // 正在做的网页操作走到哪一步
	}
	c.JSON(http.StatusOK, out)
}

// Re0BrowserDownload POST /re0/browser/download {force} —— 本地没有就下载；force=true 删掉本地的重下
func (h *Handler) Re0BrowserDownload(c *gin.Context) {
	var req struct {
		Force bool `json:"force"`
	}
	_ = c.ShouldBindJSON(&req)
	if _, ok := cloakPlatform(); !ok {
		c.JSON(http.StatusBadRequest, gin.H{"error": cloakSnapshot().Error})
		return
	}
	if req.Force {
		if err := cloakReinstall("界面「重新下载」"); err != nil {
			c.JSON(http.StatusConflict, gin.H{"error": err.Error()})
			return
		}
	} else if ensureCloak("界面「下载浏览器」") {
		c.JSON(http.StatusOK, gin.H{"message": "本地已有浏览器，不需要下载", "browser": cloakSnapshot()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"message": "已开始下载", "browser": cloakSnapshot()})
}
