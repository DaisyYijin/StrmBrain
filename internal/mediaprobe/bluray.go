package mediaprobe

import (
	"errors"
	"sort"
	"strings"
)

// ==================== 光盘镜像（ISO） ====================
//
// 蓝光：BDMV/PLAYLIST 下每个 .mpls 几百字节到几 KB，读全部、按时长挑主播放列表（最长的那个，
// libbluray bd_info 的做法），再读它第一个片段的 STN 表：视频格式、帧率、UHD 的动态范围、
// 杜比视界增强层、各条音轨的编码与语言。STN 里音轨只分「单声道 / 立体声 / 多声道」，
// 所以再读这个片段的 m2ts 开头（按 STN 给的 PID 只收音轨）：帧头给出 5.1 / 7.1、TrueHD 带不带全景声、
// DTS-HD MA 还是 DTS:X。m2ts 读不到（超出读取上限等）就只用 STN 的，不算失败。
//
// DVD（VIDEO_TS）：按 VOB 总大小挑主标题集，读它的 VTS_xx_0.IFO 里的视频 / 音频属性。

const (
	mplsMaxFiles = 1000 // 迪士尼一类的混淆盘有上千个播放列表
	mplsMaxSize  = 64 << 10
	mplsMaxSecs  = 6 * 3600 // 混淆盘里有拼出来十几个小时的假列表
)

func probeISO(r *blockReader) (*Info, error) {
	fs, err := udfOpen(r)
	if err != nil {
		return nil, err
	}
	if ent, ok, err := fs.lookup(fs.root, "BDMV"); err != nil {
		return nil, err
	} else if ok && ent.isDir {
		return probeBDMV(fs, ent.icb)
	}
	if ent, ok, err := fs.lookup(fs.root, "VIDEO_TS"); err == nil && ok && ent.isDir {
		return probeDVD(fs, ent.icb)
	}
	return nil, errors.New("镜像里既没有 BDMV 也没有 VIDEO_TS")
}

type mplsInfo struct {
	name    string
	version string
	secs    float64
	clip    string // 第一个片段的片段名（BDMV/STREAM/<clip>.m2ts）
	first   []byte // 第一个片段的 STN 表
}

func probeBDMV(fs *udfFS, bdmv udfLongAD) (*Info, error) {
	pl, ok, err := fs.lookup(bdmv, "PLAYLIST")
	if err != nil {
		return nil, err
	}
	if !ok {
		return nil, errors.New("BDMV 下没有 PLAYLIST")
	}
	ents, err := fs.list(pl.icb)
	if err != nil {
		return nil, err
	}
	sort.Slice(ents, func(i, j int) bool { return ents[i].name < ents[j].name })
	var best *mplsInfo
	n := 0
	for _, e := range ents {
		if e.isDir || !strings.HasSuffix(strings.ToLower(e.name), ".mpls") {
			continue
		}
		if n++; n > mplsMaxFiles {
			break
		}
		data, err := fs.readFile(e.icb, mplsMaxSize)
		if err != nil {
			if errors.Is(err, ErrBudget) {
				return nil, err
			}
			continue // 个别播放列表坏了不影响挑主列表
		}
		m, err := parseMPLS(data)
		if err != nil || m.secs <= 0 || m.secs > mplsMaxSecs {
			continue
		}
		m.name = e.name
		if best == nil || m.secs > best.secs {
			mm := m
			best = &mm
		}
	}
	if best == nil {
		return nil, errors.New("没有可用的播放列表")
	}
	info := &Info{Container: "bluray", Disc: "bluray"}
	applySTN(info, best.first)
	if best.version == "0300" || (info.Video != nil && info.Video.Height >= 2160) {
		info.Disc = "uhd-bluray"
	}
	refineFromM2TS(fs, bdmv, best.clip, info)
	return info, nil
}

// bdM2TSSample 主片段 m2ts 读多少：蓝光码率高，2 MB 里各条音轨也有几十帧了
const bdM2TSSample = 2 << 20

// refineFromM2TS 读主片段 m2ts 开头，按 PID 补各条音轨的声道、全景声与 DTS 的细分
func refineFromM2TS(fs *udfFS, bdmv udfLongAD, clip string, info *Info) {
	want := map[int]string{}
	for _, a := range info.Audios {
		if a.pid > 0 {
			want[a.pid] = a.Codec
		}
	}
	if clip == "" || len(want) == 0 {
		return
	}
	st, ok, err := fs.lookup(bdmv, "STREAM")
	if err != nil || !ok {
		return
	}
	f, ok, err := fs.lookup(st.icb, clip+".m2ts")
	if err != nil || !ok {
		return
	}
	off, n, err := fs.fileExtent(f.icb)
	if err != nil || n <= 0 {
		return
	}
	if n > bdM2TSSample {
		n = bdM2TSSample
	}
	b, err := fs.r.read(off, n)
	if err != nil {
		return
	}
	pkt, start, ok := tsLayout(b)
	if !ok {
		return
	}
	streams := scanTS(b, pkt, start, want)
	for i := range info.Audios {
		a := &info.Audios[i]
		s := streams[a.pid]
		if s == nil {
			continue
		}
		if a.Codec == "lpcm" && len(s.pes) > 0 {
			if ch := bdLPCMChannels(s.pes[0]); ch > 0 {
				a.Channels = ch
			}
			continue
		}
		if fi, ok := parseAudioFrames(a.Codec, s.es); ok {
			applyFrameInfo(a, fi)
		}
	}
}

// parseMPLS 播放列表：总时长（各片段 OUT-IN，45 kHz）与第一个片段的 STN 表
func parseMPLS(d []byte) (mplsInfo, error) {
	var m mplsInfo
	if len(d) < 20 || string(d[:4]) != "MPLS" {
		return m, errors.New("不是 MPLS")
	}
	m.version = string(d[4:8])
	pl := be32(d[8:])
	if pl+10 > int64(len(d)) {
		return m, errors.New("MPLS 播放列表位置越界")
	}
	numItems := be16(d[pl+6:])
	off := pl + 10
	var total int64
	for i := 0; i < numItems; i++ {
		if off+2 > int64(len(d)) {
			return m, errors.New("MPLS 片段越界")
		}
		l := int64(be16(d[off:]))
		item := d[off+2:]
		if l+2 > int64(len(d))-off || l < 32 {
			return m, errors.New("MPLS 片段越界")
		}
		item = item[:l]
		in, out := be32(item[12:]), be32(item[16:])
		if out > in {
			total += out - in
		}
		if i == 0 {
			m.clip = string(item[:5])
			p := int64(32)
			if (be16(item[9:])>>4)&1 == 1 { // 多角度：跟着角度表
				if p+2 > l {
					return m, errors.New("MPLS 角度表越界")
				}
				angles := int64(item[p])
				p += 2 + (angles-1)*10
			}
			if p+2 <= l {
				stnLen := int64(be16(item[p:]))
				if p+2+stnLen <= l {
					m.first = item[p+2 : p+2+stnLen]
				}
			}
		}
		off += 2 + l
	}
	m.secs = float64(total) / 45000
	return m, nil
}

// applySTN 读 STN 表：两字节保留之后是 8 个计数（主视频、主音频、PG、IG、副音频、副视频、画中画字幕、杜比视界）+ 4 字节保留
func applySTN(info *Info, stn []byte) {
	if len(stn) < 14 {
		return
	}
	nVideo, nAudio, nPG := int(stn[2]), int(stn[3]), int(stn[4])
	nDV := int(stn[9])
	p := 14
	// next 一条流：stream_entry（类型 1 = 主片段里的流，紧跟着 PID）+ stream_attributes
	next := func() (attr []byte, pid int, ok bool) {
		if p >= len(stn) {
			return nil, 0, false
		}
		el := int(stn[p])
		if p+1+el > len(stn) {
			return nil, 0, false
		}
		if e := stn[p+1 : p+1+el]; el >= 3 && e[0] == 1 {
			pid = be16(e[1:]) & 0x1FFF
		}
		p += 1 + el
		if p >= len(stn) {
			return nil, 0, false
		}
		l := int(stn[p])
		if p+1+l > len(stn) {
			return nil, 0, false
		}
		attr = stn[p+1 : p+1+l]
		p += 1 + l
		return attr, pid, true
	}
	var videos []Video
	for i := 0; i < nVideo; i++ {
		a, _, ok := next()
		if !ok {
			return
		}
		if v, ok := bdVideo(a); ok {
			videos = append(videos, v)
		}
	}
	for i := 0; i < nAudio; i++ {
		a, pid, ok := next()
		if !ok {
			break
		}
		if au, ok := bdAudio(a); ok {
			au.Default, au.pid = i == 0, pid
			info.Audios = append(info.Audios, au)
		}
	}
	_ = nPG
	info.Video = pickMainVideo(videos)
	if info.Video != nil && nDV > 0 {
		info.Video.DV = true
		if info.Video.Transfer == "" || info.Video.Transfer == "sdr" {
			info.Video.Transfer = "pq" // UHD 盘的杜比视界基础层是 HDR10
		}
	}
}

func bdVideo(a []byte) (Video, bool) {
	if len(a) < 2 {
		return Video{}, false
	}
	var v Video
	switch a[0] {
	case 0x1B:
		v.Codec = "h264"
	case 0x24:
		v.Codec = "hevc"
		v.BitDepth = 10 // UHD 蓝光的 HEVC 一律 10 bit
	case 0xEA:
		v.Codec = "vc1"
	case 0x01, 0x02:
		v.Codec = "mpeg2"
	default:
		return Video{}, false // 0x20 MVC（3D 的另一只眼）不算主画面
	}
	switch a[1] >> 4 {
	case 1, 3:
		v.Width, v.Height = 720, 480
	case 2, 7:
		v.Width, v.Height = 720, 576
	case 4, 6:
		v.Width, v.Height = 1920, 1080
	case 5:
		v.Width, v.Height = 1280, 720
	case 8:
		v.Width, v.Height = 3840, 2160
	}
	v.FPS = map[byte]float64{1: 23.976, 2: 24, 3: 25, 4: 29.97, 6: 50, 7: 59.94}[a[1]&0x0F]
	if a[0] == 0x24 && len(a) >= 4 {
		switch a[2] >> 4 {
		case 0:
			v.Transfer = "sdr"
		case 1:
			v.Transfer = "pq"
		case 2:
			v.Transfer, v.DV = "pq", true
		}
		v.HDR10Plus = a[3]&0x40 != 0
	}
	return v, true
}

func bdAudio(a []byte) (Audio, bool) {
	if len(a) < 5 {
		return Audio{}, false
	}
	var au Audio
	switch a[0] {
	case 0x80:
		au.Codec = "lpcm"
	case 0x81:
		au.Codec = "ac3"
	case 0x82:
		au.Codec = "dts"
	case 0x83:
		au.Codec = "truehd"
	case 0x84, 0xA1:
		au.Codec = "eac3"
	case 0x85:
		au.Codec = "dtshd"
	case 0x86:
		au.Codec = "dtshd_ma"
	case 0xA2: // 副音频的 DTS-HD（DTS Express）
		au.Codec = "dtshd"
	default:
		return Audio{}, false
	}
	switch a[1] >> 4 {
	case 1:
		au.Channels = 1
	case 3:
		au.Channels = 2
	}
	lang := strings.ToLower(strings.TrimRight(string(a[2:5]), "\x00 "))
	if lang != "und" {
		au.Lang = lang
	}
	return au, true
}
