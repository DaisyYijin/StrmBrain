// Package mediaprobe 不借助 ffmpeg，按需读视频文件里描述流信息的那几段，认出编码、分辨率、HDR、音轨。
//
// 起因是整理改名时补全画质变量（docs/115-station-notes/MEDIA-PROBE-PLAN.md）：文件在 115 上，
// 手里只有直链，镜像也不带 ffmpeg（AGENTS §6.16），所以只能自己解析容器，用 Range 读需要的几段：
//   - MKV / WebM：EBML 头部的 Tracks（不在头部就按 SeekHead 跳）
//   - MP4 / MOV：moov 盒子（在文件尾部就按盒子偏移跳过去）
//   - TS / M2TS：开头几 MB 里的 PAT / PMT、视频参数集、各条音轨的第一帧
//   - 蓝光 ISO：UDF 目录 → BDMV/PLAYLIST/*.mpls 的 STN 表，再读主片段 m2ts 开头补声道与全景声
//     （ffprobemeta 插件用的 `ffprobe bluray:` 只能开本地路径）
//   - DVD ISO：VIDEO_TS 里主标题集的 IFO
//
// 包本身不碰网络，读数据走 io.ReaderAt，调用方用直链实现、测试用字节切片。
package mediaprobe

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"strings"
)

// Info 探测结果。字段都是「读到了才填」，读不到的保持零值，调用方据此决定补不补
type Info struct {
	Container string  `json:"container"`       // mkv / mp4 / bluray / dvd
	Disc      string  `json:"disc,omitempty"`  // 光盘镜像：bluray / uhd-bluray / dvd
	Video     *Video  `json:"video,omitempty"` // 主视频轨（多条时取画面最大的）
	Audios    []Audio `json:"audios,omitempty"`
}

// Video 视频轨
type Video struct {
	Codec     string  `json:"codec"` // h264 / hevc / av1 / vp9 / vc1 / mpeg2
	Width     int     `json:"width,omitempty"`
	Height    int     `json:"height,omitempty"`
	BitDepth  int     `json:"bit_depth,omitempty"`
	FPS       float64 `json:"fps,omitempty"`
	Transfer  string  `json:"transfer,omitempty"` // pq / hlg / sdr（明确写了 BT.709 等）；空 = 没读到
	DV        bool    `json:"dv,omitempty"`
	DVProfile int     `json:"dv_profile,omitempty"`
	HDR10Plus bool    `json:"hdr10plus,omitempty"`
}

// Audio 音轨
type Audio struct {
	Codec    string `json:"codec"` // truehd / eac3 / ac3 / dts / dtshd_ma / dtshd / dtsx / aac / flac / lpcm / opus / mp3
	Channels int    `json:"channels,omitempty"`
	Atmos    bool   `json:"atmos,omitempty"`
	Lang     string `json:"lang,omitempty"`
	Default  bool   `json:"default,omitempty"`
	Name     string `json:"name,omitempty"` // 轨道名（MKV），只拿来认 Atmos / DTS-HD MA 这类容器里看不出来的

	pid   int    // 蓝光 STN 里这条流的 PID：去 m2ts 里读它的帧头
	track uint64 // MKV 的 TrackNumber：去 Cluster 里读它的帧
}

// ErrUnsupported 认不出容器或不支持的格式（AVI / RMVB 等）
var ErrUnsupported = errors.New("不支持的格式")

// ErrBudget 读的数据超过上限：多半是结构不常见（MP4 的 moov 极大、ISO 播放列表上千个），放弃比硬读安全
var ErrBudget = errors.New("读取量超过上限")

// Limits 读取上限
type Limits struct {
	BlockSize   int64 // 缓存的块大小：零碎的小读取按块对齐、一次取一块
	MaxRequests int   // 最多向底层要几次（直链上就是几次 Range 请求）；连续缺的几块合成一次
	MaxBytes    int64 // 最多读多少字节
}

// DefaultLimits 单文件最多 24 次请求、8 MB（TS 要读几 MB 才见得到视频参数集与各条音轨的帧）
var DefaultLimits = Limits{BlockSize: 256 << 10, MaxRequests: 24, MaxBytes: 8 << 20}

// Stats 这次探测实际读了多少
type Stats struct {
	Requests int
	Bytes    int64
}

// Probe 探测一个文件。name 只用来按扩展名兜底判断格式
func Probe(src io.ReaderAt, size int64, name string, lim Limits) (*Info, Stats, error) {
	if lim.BlockSize <= 0 {
		lim = DefaultLimits
	}
	r := newBlockReader(src, size, lim)
	info, err := probe(r, name)
	return info, Stats{Requests: r.fetched, Bytes: r.bytes}, err
}

func probe(r *blockReader, name string) (*Info, error) {
	head, err := r.read(0, 64)
	if err != nil {
		return nil, err
	}
	ext := strings.ToLower(name)
	if i := strings.LastIndex(ext, "."); i >= 0 {
		ext = ext[i:]
	} else {
		ext = ""
	}
	switch {
	case len(head) >= 4 && bytes.Equal(head[:4], []byte{0x1A, 0x45, 0xDF, 0xA3}):
		return probeMKV(r)
	case len(head) >= 12 && isMP4Head(head):
		return probeMP4(r)
	case ext == ".iso" || ext == ".img":
		return probeISO(r)
	}
	if b, err := r.read(0, 1024); err == nil {
		if _, _, ok := tsLayout(b); ok {
			return probeTS(r)
		}
	}
	// ISO 的前 32 KB 是系统区，常常全是 0，没有魔数可看：扩展名不是 .iso 时再看一眼卷描述符
	if r.size > 64<<10 {
		if vd, err := r.read(16*2048, 8); err == nil && (bytes.Equal(vd[1:6], []byte("BEA01")) || bytes.Equal(vd[1:6], []byte("CD001"))) {
			return probeISO(r)
		}
	}
	return nil, ErrUnsupported
}

func isMP4Head(h []byte) bool {
	t := string(h[4:8])
	return t == "ftyp" || t == "moov" || t == "mdat" || t == "free" || t == "wide" || t == "skip"
}

// ==================== 分块读取 ====================

// blockReader 把零碎的小读取合并成按块对齐的大读取，块缓存在内存里。
// 解析器只管 read(off, n)，不用关心底层一次请求多大
type blockReader struct {
	src     io.ReaderAt
	size    int64
	lim     Limits
	cache   map[int64][]byte
	fetched int
	bytes   int64
}

func newBlockReader(src io.ReaderAt, size int64, lim Limits) *blockReader {
	return &blockReader{src: src, size: size, lim: lim, cache: map[int64][]byte{}}
}

// read 读 [off, off+n)，越过文件尾时截短；一个字节都没有返回 io.ErrUnexpectedEOF
func (r *blockReader) read(off, n int64) ([]byte, error) {
	if off < 0 || n < 0 {
		return nil, fmt.Errorf("读取位置无效 %d+%d", off, n)
	}
	if r.size > 0 && off+n > r.size {
		n = r.size - off
	}
	if n <= 0 {
		return nil, io.ErrUnexpectedEOF
	}
	bs := r.lim.BlockSize
	first, last := off/bs, (off+n-1)/bs
	if err := r.fill(first, last); err != nil {
		return nil, err
	}
	out := make([]byte, 0, n)
	for idx := first; idx <= last; idx++ {
		blk := r.cache[idx]
		start, end := int64(0), int64(len(blk))
		if idx == first {
			start = off - idx*bs
		}
		if want := off + n - idx*bs; want < end {
			end = want
		}
		if start >= end {
			break // 文件尾
		}
		out = append(out, blk[start:end]...)
		if end < bs {
			break
		}
	}
	if len(out) == 0 {
		return nil, io.ErrUnexpectedEOF
	}
	return out, nil
}

// readFull 同 read，但读不满就报错（解析定长结构用）
func (r *blockReader) readFull(off, n int64) ([]byte, error) {
	b, err := r.read(off, n)
	if err != nil {
		return nil, err
	}
	if int64(len(b)) < n {
		return nil, io.ErrUnexpectedEOF
	}
	return b, nil
}

// fill 把 [first, last] 里还没缓存的块取回来：连续缺的几块一次请求
func (r *blockReader) fill(first, last int64) error {
	bs := r.lim.BlockSize
	for idx := first; idx <= last; idx++ {
		if _, ok := r.cache[idx]; ok {
			continue
		}
		end := idx
		for end+1 <= last {
			if _, ok := r.cache[end+1]; ok {
				break
			}
			end++
		}
		start := idx * bs
		n := (end - idx + 1) * bs
		if r.size > 0 && start+n > r.size {
			n = r.size - start
		}
		if n <= 0 {
			return io.ErrUnexpectedEOF
		}
		if r.fetched >= r.lim.MaxRequests || (r.lim.MaxBytes > 0 && r.bytes+n > r.lim.MaxBytes) {
			return ErrBudget
		}
		buf := make([]byte, n)
		r.fetched++
		got, err := r.src.ReadAt(buf, start)
		if err != nil && !(errors.Is(err, io.EOF) && got > 0) {
			return err
		}
		buf = buf[:got]
		r.bytes += int64(got)
		for k := idx; k <= end; k++ {
			lo := (k - idx) * bs
			if lo > int64(len(buf)) {
				lo = int64(len(buf))
			}
			hi := lo + bs
			if hi > int64(len(buf)) {
				hi = int64(len(buf))
			}
			r.cache[k] = buf[lo:hi]
		}
		idx = end
	}
	return nil
}

// ==================== 小工具 ====================

func be16(b []byte) int { return int(b[0])<<8 | int(b[1]) }
func be32(b []byte) int64 {
	return int64(b[0])<<24 | int64(b[1])<<16 | int64(b[2])<<8 | int64(b[3])
}
func be64(b []byte) int64 { return be32(b)<<32 | be32(b[4:]) }
func le16(b []byte) int   { return int(b[0]) | int(b[1])<<8 }
func le32(b []byte) int64 {
	return int64(b[0]) | int64(b[1])<<8 | int64(b[2])<<16 | int64(b[3])<<24
}
func le64(b []byte) int64 { return le32(b) | le32(b[4:])<<32 }

// MainAudio 挑「最好的一条」音轨：Atmos > TrueHD > DTS-HD MA > DTS:X > DTS-HD > DDP > DD > LPCM > FLAC > DTS > AAC，
// 同档比声道数。光盘里第一条常是 LPCM 或导演评论，不能按顺序取（ffprobemeta 的 _audio_stream_quality_score 同思路）。
// preferDefault 为真时（普通文件）有默认轨就用默认轨：压制组挑的默认轨就是这份资源的主音轨
func (i *Info) MainAudio(preferDefault bool) *Audio {
	if i == nil || len(i.Audios) == 0 {
		return nil
	}
	if preferDefault {
		for k := range i.Audios {
			if i.Audios[k].Default {
				return &i.Audios[k]
			}
		}
		return &i.Audios[0]
	}
	best, bestScore := 0, -1
	for k, a := range i.Audios {
		s := audioRank[a.Codec]*100 + a.Channels
		if a.Atmos {
			s += 10000
		}
		if s > bestScore {
			best, bestScore = k, s
		}
	}
	return &i.Audios[best]
}

var audioRank = map[string]int{
	"truehd": 90, "dtshd_ma": 85, "dtsx": 84, "dtshd": 78, "eac3": 60, "ac3": 50,
	"lpcm": 55, "flac": 45, "dts": 70, "aac": 30, "opus": 28, "mp3": 20,
}
