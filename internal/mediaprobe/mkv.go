package mediaprobe

import (
	"encoding/binary"
	"errors"
	"fmt"
	"math"
	"strings"
)

// ==================== Matroska / WebM ====================
//
// 只读到 Tracks 为止：EBML 头 → Segment → 顶层子元素逐个跳过（只读元素头，不读内容），
// 碰到 Tracks 就解析；碰到 Cluster（媒体数据开始了）还没见到 Tracks，就按 SeekHead 记的位置跳过去。
// 绝大多数文件 Tracks 在头部几 KB 以内，一次 Range 请求就够。

const (
	ebmlIDHeader       = 0x1A45DFA3
	ebmlIDSegment      = 0x18538067
	ebmlIDSeekHead     = 0x114D9B74
	ebmlIDSeek         = 0x4DBB
	ebmlIDSeekID       = 0x53AB
	ebmlIDSeekPosition = 0x53AC
	ebmlIDTracks       = 0x1654AE6B
	ebmlIDCluster      = 0x1F43B675

	ebmlIDTrackEntry      = 0xAE
	ebmlIDTrackNumber     = 0xD7
	ebmlIDTrackType       = 0x83
	ebmlIDFlagDefault     = 0x88
	ebmlIDCodecID         = 0x86
	ebmlIDCodecPrivate    = 0x63A2
	ebmlIDDefaultDuration = 0x23E383
	ebmlIDLanguage        = 0x22B59C
	ebmlIDLanguageBCP47   = 0x22B59D
	ebmlIDName            = 0x536E
	ebmlIDVideo           = 0xE0
	ebmlIDAudio           = 0xE1
	ebmlIDPixelWidth      = 0xB0
	ebmlIDPixelHeight     = 0xBA
	ebmlIDColour          = 0x55B0
	ebmlIDBitsPerChannel  = 0x55B2
	ebmlIDTransfer        = 0x55BA
	ebmlIDChannels        = 0x9F
	ebmlIDBlockAddMapping = 0x41E4
	ebmlIDBlockAddIDType  = 0x41E7
	ebmlIDBlockAddExtra   = 0x41ED

	ebmlUnknownSize = -1
	mkvTracksMax    = 2 << 20 // Tracks 正常几 KB；CodecPrivate 塞了大附件的也不会到这个数
)

// ebmlHead 一个元素的头
type ebmlHead struct {
	id      uint64
	size    int64 // ebmlUnknownSize = 未知长度（直播流 / 边录边写的文件）
	dataOff int64 // 内容从哪开始（绝对偏移）
}

// readVint 读一个 EBML 变长整数。keepMarker 为真时保留长度标记位（元素 ID 的写法）
func readVint(b []byte, keepMarker bool) (uint64, int, error) {
	if len(b) == 0 {
		return 0, 0, errors.New("EBML 数据不完整")
	}
	first := b[0]
	n := 1
	for mask := byte(0x80); n <= 8 && first&mask == 0; mask >>= 1 {
		n++
	}
	if n > 8 || len(b) < n {
		return 0, 0, errors.New("EBML 变长整数无效")
	}
	var v uint64
	if keepMarker {
		v = uint64(first)
	} else {
		v = uint64(first & (0xFF >> n))
	}
	allOnes := v == uint64(0xFF>>n)
	for i := 1; i < n; i++ {
		v = v<<8 | uint64(b[i])
		if b[i] != 0xFF {
			allOnes = false
		}
	}
	if !keepMarker && allOnes {
		return math.MaxUint64, n, nil // 未知长度
	}
	return v, n, nil
}

// parseEBMLHead 从内存里的字节解析元素头，base 是 b[0] 的绝对偏移
func parseEBMLHead(b []byte, base int64) (ebmlHead, int, error) {
	id, n1, err := readVint(b, true)
	if err != nil {
		return ebmlHead{}, 0, err
	}
	sz, n2, err := readVint(b[n1:], false)
	if err != nil {
		return ebmlHead{}, 0, err
	}
	h := ebmlHead{id: id, dataOff: base + int64(n1+n2)}
	if sz == math.MaxUint64 {
		h.size = ebmlUnknownSize
	} else if sz > math.MaxInt64/2 {
		return ebmlHead{}, 0, errors.New("EBML 元素长度异常")
	} else {
		h.size = int64(sz)
	}
	return h, n1 + n2, nil
}

func (r *blockReader) ebmlHeadAt(off int64) (ebmlHead, error) {
	b, err := r.read(off, 12)
	if err != nil {
		return ebmlHead{}, err
	}
	h, _, err := parseEBMLHead(b, off)
	return h, err
}

func probeMKV(r *blockReader) (*Info, error) {
	hdr, err := r.ebmlHeadAt(0)
	if err != nil || hdr.id != ebmlIDHeader || hdr.size < 0 {
		return nil, fmt.Errorf("MKV 头无效: %v", err)
	}
	seg, err := r.ebmlHeadAt(hdr.dataOff + hdr.size)
	if err != nil || seg.id != ebmlIDSegment {
		return nil, fmt.Errorf("MKV 找不到 Segment: %v", err)
	}
	segEnd := r.size
	if seg.size >= 0 && seg.dataOff+seg.size < segEnd {
		segEnd = seg.dataOff + seg.size
	}
	tracksAt := int64(-1) // SeekHead 记的 Tracks 位置（绝对）
	var tracks, cluster *ebmlHead
	off := seg.dataOff
scan:
	for i := 0; i < 64 && off < segEnd; i++ {
		h, err := r.ebmlHeadAt(off)
		if err != nil {
			if tracks != nil {
				break // 轨道已经有了，找 Cluster 只是为了读音频帧，读不到就算
			}
			return nil, err
		}
		switch h.id {
		case ebmlIDTracks:
			hh := h
			tracks = &hh
		case ebmlIDSeekHead:
			if h.size > 0 && h.size < 1<<20 {
				if body, err := r.readFull(h.dataOff, h.size); err == nil {
					if pos := seekHeadTracks(body); pos >= 0 {
						tracksAt = seg.dataOff + pos
					}
				}
			}
		case ebmlIDCluster:
			hh := h
			cluster = &hh
			break scan // 媒体数据开始了
		}
		if h.size < 0 {
			break // 未知长度只可能是 Cluster 这类，后面没法跳
		}
		off = h.dataOff + h.size
	}
	if tracks == nil {
		if tracksAt < 0 {
			return nil, errors.New("MKV 里没找到轨道信息")
		}
		h, err := r.ebmlHeadAt(tracksAt)
		if err != nil || h.id != ebmlIDTracks {
			return nil, fmt.Errorf("MKV SeekHead 指向的不是轨道信息: %v", err)
		}
		tracks = &h
	}
	info, err := parseMKVTracksAt(r, *tracks)
	if err != nil {
		return nil, err
	}
	if cluster != nil {
		refineMKVAudio(r, *cluster, info)
	}
	return info, nil
}

// mkvClusterSample 第一个 Cluster 读多少：每条音轨攒到几帧就够
const mkvClusterSample = 1 << 20

// refineMKVAudio MKV 的 A_DTS 不分核心 / HD MA / DTS:X，TrueHD 也看不出全景声：轨道名没写的，
// 读第一个 Cluster 里这条轨的块，按帧头认。读不到不算失败
func refineMKVAudio(r *blockReader, cluster ebmlHead, info *Info) {
	need := map[uint64]int{} // TrackNumber → Audios 下标
	for i, a := range info.Audios {
		if a.track == 0 {
			continue
		}
		// 轨道名写了 MA 的也读一下：DTS:X 常常只写成「DTS-HD MA 7.1」
		if dtsRank(a.Codec) > 0 && a.Codec != "dtsx" || a.Codec == "truehd" && !a.Atmos {
			need[a.track] = i
		}
	}
	if len(need) == 0 {
		return
	}
	n := int64(mkvClusterSample)
	if cluster.size >= 0 && cluster.size < n {
		n = cluster.size
	}
	body, err := r.read(cluster.dataOff, n)
	if err != nil {
		return
	}
	data := map[uint64][]byte{}
	take := func(block []byte) {
		tn, l, err := readVint(block, false)
		if err != nil || len(block) < l+3 {
			return
		}
		if _, ok := need[tn]; ok && len(data[tn]) < tsAudioESMax {
			data[tn] = append(data[tn], block[l+3:]...)
		}
	}
	eachEBML(body, func(id uint64, d []byte) {
		switch id {
		case 0xA3: // SimpleBlock
			take(d)
		case 0xA0: // BlockGroup
			eachEBML(d, func(id uint64, d []byte) {
				if id == 0xA1 {
					take(d)
				}
			})
		}
	})
	for tn, i := range need {
		if fi, ok := parseAudioFrames(info.Audios[i].Codec, data[tn]); ok {
			applyFrameInfo(&info.Audios[i], fi)
		}
	}
}

// seekHeadTracks SeekHead 里 Tracks 的位置（相对 Segment 内容起点），没有返回 -1
func seekHeadTracks(body []byte) int64 {
	pos := int64(-1)
	eachEBML(body, func(id uint64, data []byte) {
		if id != ebmlIDSeek {
			return
		}
		var sid uint64
		var spos int64 = -1
		eachEBML(data, func(id uint64, d []byte) {
			switch id {
			case ebmlIDSeekID:
				sid = ebmlUint(d)
			case ebmlIDSeekPosition:
				spos = int64(ebmlUint(d))
			}
		})
		if sid == ebmlIDTracks && spos >= 0 {
			pos = spos
		}
	})
	return pos
}

func parseMKVTracksAt(r *blockReader, h ebmlHead) (*Info, error) {
	if h.size <= 0 || h.size > mkvTracksMax {
		return nil, fmt.Errorf("MKV 轨道信息长度异常: %d", h.size)
	}
	body, err := r.readFull(h.dataOff, h.size)
	if err != nil {
		return nil, err
	}
	return parseMKVTracks(body), nil
}

// eachEBML 遍历内存里一段 EBML 内容的直接子元素
func eachEBML(b []byte, fn func(id uint64, data []byte)) {
	for len(b) > 0 {
		h, n, err := parseEBMLHead(b, 0)
		if err != nil || h.size < 0 || int64(n)+h.size > int64(len(b)) {
			return
		}
		fn(h.id, b[n:int64(n)+h.size])
		b = b[int64(n)+h.size:]
	}
}

func ebmlUint(b []byte) uint64 {
	var v uint64
	for _, c := range b {
		v = v<<8 | uint64(c)
	}
	return v
}

func ebmlFloat(b []byte) float64 {
	switch len(b) {
	case 4:
		return float64(math.Float32frombits(binary.BigEndian.Uint32(b)))
	case 8:
		return math.Float64frombits(binary.BigEndian.Uint64(b))
	}
	return 0
}

func parseMKVTracks(body []byte) *Info {
	info := &Info{Container: "mkv"}
	var videos []Video
	dvAny := false
	eachEBML(body, func(id uint64, entry []byte) {
		if id != ebmlIDTrackEntry {
			return
		}
		var (
			num       uint64
			typ       uint64
			codecID   string
			priv      []byte
			defDur    uint64
			isDefault = true // FlagDefault 缺省为 1
			lang      = "eng"
			bcp47     string
			name      string
			v         Video
			a         Audio
			dvRec     []byte
		)
		eachEBML(entry, func(id uint64, d []byte) {
			switch id {
			case ebmlIDTrackNumber:
				num = ebmlUint(d)
			case ebmlIDTrackType:
				typ = ebmlUint(d)
			case ebmlIDCodecID:
				codecID = string(d)
			case ebmlIDCodecPrivate:
				priv = d
			case ebmlIDDefaultDuration:
				defDur = ebmlUint(d)
			case ebmlIDFlagDefault:
				isDefault = ebmlUint(d) != 0
			case ebmlIDLanguage:
				lang = strings.TrimRight(string(d), "\x00")
			case ebmlIDLanguageBCP47:
				bcp47 = strings.TrimRight(string(d), "\x00")
			case ebmlIDName:
				name = strings.TrimRight(string(d), "\x00")
			case ebmlIDVideo:
				eachEBML(d, func(id uint64, d []byte) {
					switch id {
					case ebmlIDPixelWidth:
						v.Width = int(ebmlUint(d))
					case ebmlIDPixelHeight:
						v.Height = int(ebmlUint(d))
					case ebmlIDColour:
						eachEBML(d, func(id uint64, d []byte) {
							switch id {
							case ebmlIDBitsPerChannel:
								v.BitDepth = int(ebmlUint(d))
							case ebmlIDTransfer:
								v.Transfer = transferName(int(ebmlUint(d)))
							}
						})
					}
				})
			case ebmlIDAudio:
				eachEBML(d, func(id uint64, d []byte) {
					if id == ebmlIDChannels {
						a.Channels = int(ebmlUint(d))
					}
				})
			case ebmlIDBlockAddMapping:
				var typ uint64
				var extra []byte
				eachEBML(d, func(id uint64, d []byte) {
					switch id {
					case ebmlIDBlockAddIDType:
						typ = ebmlUint(d)
					case ebmlIDBlockAddExtra:
						extra = d
					}
				})
				// 杜比视界配置记录：'dvcC'（档位 ≤7）/ 'dvvC'（档位 8–10）/ 'dvwC'
				if typ == 0x64766343 || typ == 0x64767643 || typ == 0x64767743 {
					dvRec = extra
				}
			}
		})
		if bcp47 != "" {
			lang = bcp47
		}
		if lang == "und" {
			lang = ""
		}
		switch typ {
		case 1: // 视频
			v.Codec, v.BitDepth = mkvVideoCodec(codecID, priv, v.BitDepth)
			if v.Codec == "" {
				return
			}
			if defDur > 0 {
				v.FPS = roundFPS(1e9 / float64(defDur))
			}
			if dvRec != nil {
				applyDVRecord(&v, dvRec)
				dvAny = true
			}
			videos = append(videos, v)
		case 2: // 音频
			a.Codec = mkvAudioCodec(codecID, name)
			if a.Codec == "" {
				return
			}
			a.Lang, a.Default, a.Name, a.track = lang, isDefault, name, num
			if strings.Contains(strings.ToLower(name), "atmos") && (a.Codec == "truehd" || a.Codec == "eac3") {
				a.Atmos = true
			}
			info.Audios = append(info.Audios, a)
		}
	})
	info.Video = pickMainVideo(videos)
	if info.Video != nil && dvAny && !info.Video.DV {
		// 双轨杜比视界（增强层单独一条视频轨）：主画面那条自己没有 DV 配置
		info.Video.DV = true
	}
	return info
}

func pickMainVideo(vs []Video) *Video {
	if len(vs) == 0 {
		return nil
	}
	best := 0
	for i, v := range vs {
		if v.Width*v.Height > vs[best].Width*vs[best].Height {
			best = i
		}
	}
	v := vs[best]
	return &v
}

// transferName ITU-T H.273 传输特性码
func transferName(tc int) string {
	switch tc {
	case 16:
		return "pq"
	case 18:
		return "hlg"
	case 1, 6, 14, 15:
		return "sdr"
	}
	return ""
}

// applyDVRecord 解析 DOVIDecoderConfigurationRecord：第 3 字节高 7 位是档位，第 5 字节高 4 位是基础层兼容性
// （1 = HDR10、2 = SDR、4 = HLG；档位 7 的基础层就是 HDR10，档位 5 没有兼容层）
func applyDVRecord(v *Video, rec []byte) {
	v.DV = true
	if len(rec) < 5 {
		return
	}
	v.DVProfile = int(rec[2] >> 1)
	compat := rec[4] >> 4
	if v.Transfer != "" {
		return
	}
	switch {
	case v.DVProfile == 7, compat == 1, compat == 6:
		v.Transfer = "pq"
	case compat == 4:
		v.Transfer = "hlg"
	case compat == 2:
		v.Transfer = "sdr"
	}
}

// mkvVideoCodec CodecID → 编码；位深优先用 Colour 里写的，没写再从编码配置里读
func mkvVideoCodec(id string, priv []byte, depth int) (string, int) {
	switch {
	case strings.HasPrefix(id, "V_MPEGH/ISO/HEVC"):
		return "hevc", firstNonZero(depth, hvcCBitDepth(priv))
	case strings.HasPrefix(id, "V_MPEG4/ISO/AVC"):
		return "h264", firstNonZero(depth, avcCBitDepth(priv))
	case id == "V_AV1":
		return "av1", firstNonZero(depth, av1CBitDepth(priv))
	case id == "V_VP9":
		return "vp9", depth
	case strings.HasPrefix(id, "V_MPEG2"), id == "V_MPEG1":
		return "mpeg2", depth
	case id == "V_MS/VFW/FOURCC":
		// BITMAPINFOHEADER：第 16 字节起是四字符码
		if len(priv) >= 20 {
			switch strings.ToUpper(string(priv[16:20])) {
			case "WVC1", "WMV3":
				return "vc1", depth
			case "H264", "AVC1":
				return "h264", depth
			case "HEVC", "HVC1":
				return "hevc", depth
			}
		}
	}
	return "", 0
}

// mkvAudioCodec CodecID → 编码。DTS 在 MKV 里不分核心 / HD MA / X，只能靠轨道名（压制组几乎都会写），
// 写不出就按普通 DTS（宁可少写，不写错）
func mkvAudioCodec(id, name string) string {
	switch {
	case id == "A_TRUEHD", id == "A_MLP":
		return "truehd"
	case id == "A_EAC3":
		return "eac3"
	case id == "A_AC3", strings.HasPrefix(id, "A_AC3/"):
		return "ac3"
	case id == "A_DTS", strings.HasPrefix(id, "A_DTS/"):
		return dtsByName(name)
	case strings.HasPrefix(id, "A_AAC"):
		return "aac"
	case id == "A_FLAC":
		return "flac"
	case strings.HasPrefix(id, "A_PCM"):
		return "lpcm"
	case id == "A_OPUS":
		return "opus"
	case id == "A_MPEG/L3":
		return "mp3"
	}
	return ""
}

func dtsByName(name string) string {
	n := strings.ToUpper(name)
	switch {
	case strings.Contains(n, "DTS:X"), strings.Contains(n, "DTS-X"), strings.Contains(n, "DTSX"):
		return "dtsx"
	case strings.Contains(n, "MASTER AUDIO"), strings.Contains(n, "HD MA"), strings.Contains(n, "HD-MA"), strings.Contains(n, "HD.MA"), strings.Contains(n, "DTS-HD MA"):
		return "dtshd_ma"
	case strings.Contains(n, "DTS-HD"), strings.Contains(n, "HIGH RES"), strings.Contains(n, "HRA"):
		return "dtshd"
	}
	return "dts"
}

func firstNonZero(a, b int) int {
	if a != 0 {
		return a
	}
	return b
}

// hvcCBitDepth HEVCDecoderConfigurationRecord 第 18 字节低 3 位是 bitDepthLumaMinus8
func hvcCBitDepth(p []byte) int {
	if len(p) >= 23 && p[0] == 1 {
		return int(p[17]&7) + 8
	}
	return 0
}

// avcCBitDepth 只认得出 High 10（profile_idc 110）；其余档次 AVC 都是 8 bit
func avcCBitDepth(p []byte) int {
	if len(p) >= 4 && p[0] == 1 {
		if p[1] == 110 {
			return 10
		}
		return 8
	}
	return 0
}

// av1CBitDepth AV1CodecConfigurationRecord 第 3 字节：high_bitdepth(0x40) / twelve_bit(0x20)
func av1CBitDepth(p []byte) int {
	if len(p) >= 4 && p[0]&0x80 != 0 {
		switch {
		case p[2]&0x40 != 0 && p[2]&0x20 != 0:
			return 12
		case p[2]&0x40 != 0:
			return 10
		}
		return 8
	}
	return 0
}

// roundFPS 帧率保留到常见写法：23.976 / 29.97 / 59.94 这类 NTSC 帧率保留三位小数，整数帧率取整
func roundFPS(f float64) float64 {
	if f <= 0 || f > 1000 {
		return 0
	}
	if r := math.Round(f); math.Abs(f-r) < 0.01 {
		return r
	}
	return math.Round(f*1000) / 1000
}
