package mediaprobe

import (
	"math"
	"sort"
)

// ==================== MPEG-TS / 蓝光 M2TS ====================
//
// 读开头几 MB：PAT → PMT 给每条流的类型、语言、杜比视界描述符；再按 PID 把 PES 负载拼起来，
// 视频交给 videoFromES 读参数集，音频交给 parseAudioFrames 读帧头。
// M2TS 每个包前面多 4 字节时间戳（192 字节一包），蓝光 ISO 里的 .m2ts 也走这里。

const (
	tsSampleSize  = 6 << 20 // 独立的 TS 文件读多少
	tsVideoESMax  = 2 << 20 // 每条视频流最多攒多少负载（4K 首帧就有几百 KB，SPS 在最前面）
	tsAudioESMax  = 128 << 10
	tsVideoPTSMax = 64
)

type tsStream struct {
	pid      int
	typ      byte
	kind     byte // 'v' / 'a'
	codec    string
	lang     string
	dovi     bool
	doviProf int
	es       []byte
	pes      [][]byte // 每个 PES 的开头（蓝光 LPCM 的 4 字节头在这里）
	pts      []int64
	started  bool
}

// tsLayout 包长与第一个同步字的位置：连续三个包都对得上才算
func tsLayout(b []byte) (pkt, off int, ok bool) {
	for _, size := range []int{188, 192} {
		lead := 0
		if size == 192 {
			lead = 4
		}
		for start := 0; start < size && start+2*size+lead < len(b); start++ {
			i := start + lead
			if b[i] == 0x47 && b[i+size] == 0x47 && b[i+2*size] == 0x47 {
				return size, start, true
			}
		}
	}
	return 0, 0, false
}

func probeTS(r *blockReader) (*Info, error) {
	n := int64(tsSampleSize)
	if r.size > 0 && n > r.size {
		n = r.size
	}
	b, err := r.read(0, n)
	if err != nil {
		return nil, err
	}
	pkt, off, ok := tsLayout(b)
	if !ok {
		return nil, ErrUnsupported
	}
	streams := scanTS(b, pkt, off, nil)
	info := &Info{Container: "ts"}
	if pkt == 192 {
		info.Container = "m2ts"
	}
	fillFromTS(info, streams, pkt == 192)
	return info, nil
}

// scanTS 扫一段 TS。want 不为空时只收这些 PID（蓝光 ISO 已经从 STN 知道各条流是什么）
func scanTS(b []byte, pkt, off int, want map[int]string) map[int]*tsStream {
	lead := pkt - 188
	pmtPID := -1
	streams := map[int]*tsStream{}
	if want != nil {
		for pid, codec := range want {
			kind := byte('a')
			if isVideoCodec(codec) {
				kind = 'v'
			}
			streams[pid] = &tsStream{pid: pid, codec: codec, kind: kind}
		}
	}
	for p := off; p+pkt <= len(b); p += pkt {
		pk := b[p+lead : p+lead+188]
		if pk[0] != 0x47 {
			continue
		}
		pusi := pk[1]&0x40 != 0
		pid := int(pk[1]&0x1F)<<8 | int(pk[2])
		afc := (pk[3] >> 4) & 3
		pay := 4
		if afc&2 != 0 {
			pay += 1 + int(pk[4])
		}
		if afc&1 == 0 || pay >= 188 {
			continue
		}
		data := pk[pay:]
		switch {
		case pid == 0 && pusi && want == nil:
			if v := patPMTPID(data); v >= 0 {
				pmtPID = v
			}
		case pid == pmtPID && pusi && want == nil:
			for _, s := range parsePMT(data, pkt == 192) {
				if _, ok := streams[s.pid]; !ok {
					streams[s.pid] = s
				}
			}
		default:
			s := streams[pid]
			if s == nil {
				continue
			}
			s.feed(data, pusi)
		}
	}
	return streams
}

func isVideoCodec(c string) bool {
	switch c {
	case "h264", "hevc", "mpeg2", "vc1":
		return true
	}
	return false
}

// feed 收一个包的负载：新 PES 开头剥掉 PES 头、记 PTS
func (s *tsStream) feed(data []byte, pusi bool) {
	max := tsAudioESMax
	if s.kind == 'v' {
		max = tsVideoESMax
	}
	if pusi {
		if len(data) < 9 || data[0] != 0 || data[1] != 0 || data[2] != 1 {
			s.started = false
			return
		}
		hl := 9 + int(data[8])
		if hl > len(data) {
			s.started = false
			return
		}
		if data[7]&0x80 != 0 && len(data) >= 14 && len(s.pts) < tsVideoPTSMax {
			p := data[9:14]
			pts := int64(p[0]>>1&7)<<30 | int64(p[1])<<22 | int64(p[2]>>1)<<15 | int64(p[3])<<7 | int64(p[4]>>1)
			s.pts = append(s.pts, pts)
		}
		data = data[hl:]
		s.started = true
		if len(s.pes) < 4 {
			s.pes = append(s.pes, append([]byte(nil), data...))
		}
	}
	if !s.started || len(s.es) >= max {
		return
	}
	s.es = append(s.es, data...)
}

// patPMTPID PAT 里第一个节目的 PMT PID
func patPMTPID(d []byte) int {
	if len(d) < 1 {
		return -1
	}
	d = d[1+int(d[0]):] // pointer_field
	if len(d) < 12 || d[0] != 0x00 {
		return -1
	}
	secLen := int(d[1]&0x0F)<<8 | int(d[2])
	end := 3 + secLen - 4
	if end > len(d) {
		end = len(d)
	}
	for i := 8; i+4 <= end; i += 4 {
		if prog := be16(d[i:]); prog != 0 {
			return int(d[i+2]&0x1F)<<8 | int(d[i+3])
		}
	}
	return -1
}

// parsePMT 每条基本流的类型与描述符
func parsePMT(d []byte, bluray bool) []*tsStream {
	if len(d) < 1 {
		return nil
	}
	d = d[1+int(d[0]):]
	if len(d) < 12 || d[0] != 0x02 {
		return nil
	}
	secLen := int(d[1]&0x0F)<<8 | int(d[2])
	end := 3 + secLen - 4
	if end > len(d) {
		end = len(d)
	}
	progInfo := int(d[10]&0x0F)<<8 | int(d[11])
	i := 12 + progInfo
	hdmv := bluray
	if 12+progInfo <= len(d) && regFormat(d[12:12+progInfo]) == "HDMV" {
		hdmv = true
	}
	var out []*tsStream
	hevc := 0
	for i+5 <= end {
		typ := d[i]
		pid := int(d[i+1]&0x1F)<<8 | int(d[i+2])
		esLen := int(d[i+3]&0x0F)<<8 | int(d[i+4])
		if i+5+esLen > len(d) {
			break
		}
		desc := d[i+5 : i+5+esLen]
		i += 5 + esLen
		s := &tsStream{pid: pid, typ: typ}
		s.codec, s.kind = tsCodec(typ, desc, hdmv)
		if s.kind == 0 {
			continue
		}
		eachDescriptor(desc, func(tag byte, b []byte) {
			switch tag {
			case 0x0A: // ISO_639_language
				if len(b) >= 3 {
					s.lang = langCode(b[:3])
				}
			case 0xB0: // DOVI_video_stream_descriptor
				s.dovi = true
				if len(b) >= 3 {
					s.doviProf = int(b[2] >> 1)
				}
			}
		})
		if s.codec == "hevc" {
			hevc++
			if hevc > 1 { // 蓝光 UHD 的杜比视界增强层是第二条 HEVC 流
				s.dovi = true
				for _, o := range out {
					if o.codec == "hevc" {
						o.dovi = true
					}
				}
				continue
			}
		}
		out = append(out, s)
	}
	return out
}

func eachDescriptor(b []byte, fn func(tag byte, data []byte)) {
	for len(b) >= 2 {
		l := int(b[1])
		if 2+l > len(b) {
			return
		}
		fn(b[0], b[2:2+l])
		b = b[2+l:]
	}
}

// regFormat registration_descriptor（tag 5）的四字符格式标识
func regFormat(desc []byte) string {
	f := ""
	eachDescriptor(desc, func(tag byte, b []byte) {
		if tag == 0x05 && len(b) >= 4 && f == "" {
			f = string(b[:4])
		}
	})
	return f
}

// tsCodec stream_type → 编码。0x80–0x86 只在蓝光（HDMV）里是 LPCM / AC-3 / DTS / TrueHD…，
// 普通 TS 里同样的值可能是别的私有用途；0x06 私有数据靠描述符认
func tsCodec(typ byte, desc []byte, hdmv bool) (string, byte) {
	switch typ {
	case 0x01, 0x02:
		return "mpeg2", 'v'
	case 0x1B:
		return "h264", 'v'
	case 0x24:
		return "hevc", 'v'
	case 0xEA:
		return "vc1", 'v'
	case 0x03, 0x04:
		return "mp3", 'a'
	case 0x0F, 0x11:
		return "aac", 'a'
	case 0x81:
		return "ac3", 'a'
	case 0x84, 0x87:
		return "eac3", 'a'
	}
	if hdmv {
		switch typ {
		case 0x80:
			return "lpcm", 'a'
		case 0x82:
			return "dts", 'a'
		case 0x83:
			return "truehd", 'a'
		case 0x85:
			return "dtshd", 'a'
		case 0x86:
			return "dtshd_ma", 'a'
		}
	}
	if typ == 0x06 {
		codec := ""
		eachDescriptor(desc, func(tag byte, b []byte) {
			switch tag {
			case 0x6A:
				codec = "ac3"
			case 0x7A:
				codec = "eac3"
			case 0x7B:
				codec = "dts"
			}
		})
		switch regFormat(desc) {
		case "AC-3":
			codec = "ac3"
		case "EAC3":
			codec = "eac3"
		case "DTS1", "DTS2", "DTS3":
			codec = "dts"
		}
		if codec != "" {
			return codec, 'a'
		}
	}
	return "", 0
}

func langCode(b []byte) string {
	l := make([]byte, 0, 3)
	for _, c := range b {
		if c >= 'A' && c <= 'Z' {
			c += 'a' - 'A'
		}
		if c < 'a' || c > 'z' {
			return ""
		}
		l = append(l, c)
	}
	if s := string(l); s != "und" {
		return s
	}
	return ""
}

// fillFromTS 把各条流读到的写进 Info
func fillFromTS(info *Info, streams map[int]*tsStream, bluray bool) {
	pids := make([]int, 0, len(streams))
	for pid := range streams {
		pids = append(pids, pid)
	}
	sort.Ints(pids)
	var videos []Video
	dovi, doviProf := false, 0
	first := true
	for _, pid := range pids {
		s := streams[pid]
		if s.dovi {
			dovi = true
			if s.doviProf > 0 {
				doviProf = s.doviProf
			}
		}
		switch s.kind {
		case 'v':
			v, ok := videoFromES(s.codec, s.es)
			if !ok {
				v = Video{Codec: s.codec}
			}
			if v.FPS == 0 {
				v.FPS = ptsFPS(s.pts)
			}
			videos = append(videos, v)
		case 'a':
			a := Audio{Codec: s.codec, Lang: s.lang, Default: first}
			first = false
			if s.codec == "lpcm" && len(s.pes) > 0 {
				a.Channels = bdLPCMChannels(s.pes[0])
			} else if fi, ok := parseAudioFrames(s.codec, s.es); ok {
				applyFrameInfo(&a, fi)
			}
			info.Audios = append(info.Audios, a)
		}
	}
	info.Video = pickMainVideo(videos)
	if info.Video != nil && dovi {
		info.Video.DV = true
		if doviProf > 0 {
			info.Video.DVProfile = doviProf
		}
		if info.Video.Transfer == "" && (bluray || doviProf == 7) {
			info.Video.Transfer = "pq" // 双层杜比视界的基础层是 HDR10
		}
	}
}

// applyFrameInfo 帧头读到的盖过容器层的：容器只说是 DTS-HD，帧头才知道是 MA 还是 DTS:X
func applyFrameInfo(a *Audio, fi audioFrameInfo) {
	if fi.channels > 0 {
		a.Channels = fi.channels
	}
	if fi.atmos {
		a.Atmos = true
	}
	if fi.codec != "" && dtsRank(fi.codec) >= dtsRank(a.Codec) {
		a.Codec = fi.codec
	}
	if fi.codec == "eac3" && a.Codec == "ac3" {
		a.Codec = "eac3"
	}
}

// dtsRank 只在 DTS 家族里往上修正（核心 < HD < MA < X），别的编码不动
func dtsRank(c string) int {
	return map[string]int{"dts": 1, "dtshd": 2, "dtshd_ma": 3, "dtsx": 4}[c]
}

// ptsFPS 视频 PES 的显示时间戳排序后最小的间隔就是一帧（有 B 帧时解码顺序是乱的，排序后照样成立）
func ptsFPS(pts []int64) float64 {
	if len(pts) < 4 {
		return 0
	}
	s := append([]int64(nil), pts...)
	sort.Slice(s, func(i, j int) bool { return s[i] < s[j] })
	var d int64 = math.MaxInt64
	for i := 1; i < len(s); i++ {
		if diff := s[i] - s[i-1]; diff > 0 && diff < d {
			d = diff
		}
	}
	if d == math.MaxInt64 || d == 0 {
		return 0
	}
	return snapFPS(90000 / float64(d))
}

// snapFPS 时间戳算出来的帧率吸附到常见值（差 0.5% 以内）
func snapFPS(f float64) float64 {
	for _, c := range []float64{23.976, 24, 25, 29.97, 30, 48, 50, 59.94, 60, 100, 119.88, 120} {
		if math.Abs(f-c)/c < 0.005 {
			return c
		}
	}
	return roundFPS(f)
}
