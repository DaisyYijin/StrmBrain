package mediaprobe

import (
	"errors"
	"fmt"
	"math"
	"strings"
)

// ==================== MP4 / MOV ====================
//
// 只走盒子头导航到每条轨道的 hdlr / mdhd / stsd / stts，不读 stsz / stco 这些大表：
// 两小时的片子 moov 有好几 MB，整块读超出读取上限。moov 在文件尾（没做 faststart）也一样，
// 顶层只读几个盒子头就能跳过去。

type mp4Box struct {
	typ     string
	off     int64 // 盒子起点
	dataOff int64 // 内容起点（跳过头）
	end     int64
}

func (r *blockReader) mp4BoxAt(off, limit int64) (mp4Box, error) {
	h, err := r.readFull(off, 8)
	if err != nil {
		return mp4Box{}, err
	}
	size := be32(h)
	b := mp4Box{typ: string(h[4:8]), off: off, dataOff: off + 8}
	switch size {
	case 0:
		b.end = limit
	case 1:
		ext, err := r.readFull(off+8, 8)
		if err != nil {
			return mp4Box{}, err
		}
		b.dataOff = off + 16
		b.end = off + be64(ext)
	default:
		b.end = off + size
	}
	if b.end < b.dataOff || b.end > limit {
		return mp4Box{}, fmt.Errorf("MP4 盒子 %q 长度异常", b.typ)
	}
	return b, nil
}

// mp4Children 遍历 [off, end) 里的盒子
func (r *blockReader) mp4Children(off, end int64, fn func(b mp4Box) error) error {
	for i := 0; off+8 <= end && i < 256; i++ {
		b, err := r.mp4BoxAt(off, end)
		if err != nil {
			return err
		}
		if err := fn(b); err != nil {
			return err
		}
		off = b.end
	}
	return nil
}

func (r *blockReader) mp4Find(off, end int64, typ string) (mp4Box, bool, error) {
	var found mp4Box
	ok := false
	err := r.mp4Children(off, end, func(b mp4Box) error {
		if !ok && b.typ == typ {
			found, ok = b, true
		}
		return nil
	})
	return found, ok, err
}

var errStop = errors.New("stop")

func probeMP4(r *blockReader) (*Info, error) {
	limit := r.size
	if limit <= 0 {
		limit = math.MaxInt64 / 2
	}
	var moov mp4Box
	found := false
	err := r.mp4Children(0, limit, func(b mp4Box) error {
		if b.typ == "moov" {
			moov, found = b, true
			return errStop
		}
		return nil
	})
	if err != nil && !errors.Is(err, errStop) {
		return nil, err
	}
	if !found {
		return nil, errors.New("MP4 里没找到 moov")
	}
	info := &Info{Container: "mp4"}
	var videos []Video
	firstAudio := true
	err = r.mp4Children(moov.dataOff, moov.end, func(trak mp4Box) error {
		if trak.typ != "trak" {
			return nil
		}
		mdia, ok, err := r.mp4Find(trak.dataOff, trak.end, "mdia")
		if err != nil || !ok {
			return err
		}
		var handler, lang string
		var timescale int64
		var stbl mp4Box
		hasStbl := false
		err = r.mp4Children(mdia.dataOff, mdia.end, func(b mp4Box) error {
			switch b.typ {
			case "hdlr":
				if d, err := r.readFull(b.dataOff, 12); err == nil {
					handler = string(d[8:12])
				}
			case "mdhd":
				d, err := r.readFull(b.dataOff, 24)
				if err != nil {
					return nil
				}
				if d[0] == 1 { // version 1：创建 / 修改时间与时长是 64 位
					if d, err = r.readFull(b.dataOff, 36); err != nil {
						return nil
					}
					timescale = be32(d[20:])
					lang = mp4Lang(be16(d[32:]))
				} else {
					timescale = be32(d[12:])
					lang = mp4Lang(be16(d[20:]))
				}
			case "minf":
				if s, ok, _ := r.mp4Find(b.dataOff, b.end, "stbl"); ok {
					stbl, hasStbl = s, true
				}
			}
			return nil
		})
		if err != nil || !hasStbl || (handler != "vide" && handler != "soun") {
			return err
		}
		stsd, ok, err := r.mp4Find(stbl.dataOff, stbl.end, "stsd")
		if err != nil || !ok || stsd.end-stsd.dataOff > 256<<10 {
			return err
		}
		body, err := r.readFull(stsd.dataOff, stsd.end-stsd.dataOff)
		if err != nil || len(body) < 16 {
			return err
		}
		entry := body[8:] // version/flags + entry_count
		if handler == "vide" {
			v := parseMP4Visual(entry)
			if v.Codec == "" {
				return nil
			}
			if timescale > 0 {
				if stts, ok, _ := r.mp4Find(stbl.dataOff, stbl.end, "stts"); ok {
					v.FPS = mp4FPS(r, stts, timescale)
				}
			}
			videos = append(videos, v)
			return nil
		}
		a := parseMP4Audio(entry)
		if a.Codec == "" {
			return nil
		}
		if lang != "und" {
			a.Lang = lang
		}
		a.Default, firstAudio = firstAudio, false
		info.Audios = append(info.Audios, a)
		return nil
	})
	if err != nil {
		return nil, err
	}
	info.Video = pickMainVideo(videos)
	return info, nil
}

// mp4Lang ISO-639-2/T 打包成 3×5 bit
func mp4Lang(v int) string {
	if v == 0 || v == 0x7FFF {
		return ""
	}
	b := []byte{byte((v>>10)&0x1F) + 0x60, byte((v>>5)&0x1F) + 0x60, byte(v&0x1F) + 0x60}
	for _, c := range b {
		if c < 'a' || c > 'z' {
			return ""
		}
	}
	return string(b)
}

// mp4FPS 取 stts 里样本最多的那一段的时长（可变帧率的片子也按主帧率算）
func mp4FPS(r *blockReader, stts mp4Box, timescale int64) float64 {
	n := stts.end - stts.dataOff
	if n > 64<<10 {
		n = 64 << 10
	}
	d, err := r.read(stts.dataOff, n)
	if err != nil || len(d) < 8 {
		return 0
	}
	count := be32(d[4:])
	var bestN, bestDelta int64
	for i := int64(0); i < count && 8+i*8+8 <= int64(len(d)); i++ {
		c, delta := be32(d[8+i*8:]), be32(d[12+i*8:])
		if c > bestN && delta > 0 {
			bestN, bestDelta = c, delta
		}
	}
	if bestDelta == 0 {
		return 0
	}
	return roundFPS(float64(timescale) / float64(bestDelta))
}

// mp4SubBoxes 遍历内存里一段盒子
func mp4SubBoxes(b []byte, fn func(typ string, data []byte)) {
	for len(b) >= 8 {
		size := be32(b)
		if size < 8 || size > int64(len(b)) {
			return
		}
		fn(string(b[4:8]), b[8:size])
		b = b[size:]
	}
}

func parseMP4Visual(entries []byte) Video {
	var v Video
	if len(entries) < 86 {
		return v
	}
	size := be32(entries)
	if size < 86 || size > int64(len(entries)) {
		return v
	}
	e := entries[:size]
	typ := string(e[4:8])
	v.Width, v.Height = be16(e[32:]), be16(e[34:])
	switch typ {
	case "hvc1", "hev1":
		v.Codec = "hevc"
	case "dvh1", "dvhe":
		v.Codec, v.DV = "hevc", true
	case "avc1", "avc2", "avc3", "avc4":
		v.Codec = "h264"
	case "dva1", "dvav":
		v.Codec, v.DV = "h264", true
	case "av01":
		v.Codec = "av1"
	case "dav1":
		v.Codec, v.DV = "av1", true
	case "vp09":
		v.Codec = "vp9"
	case "mp4v":
		return Video{} // MPEG-4 Part 2（XviD 一类），文件名词表里没有对应写法
	case "vc-1":
		v.Codec = "vc1"
	default:
		return Video{}
	}
	var dvRec []byte
	mp4SubBoxes(e[86:], func(t string, d []byte) {
		switch t {
		case "hvcC":
			v.BitDepth = firstNonZero(v.BitDepth, hvcCBitDepth(d))
		case "avcC":
			v.BitDepth = firstNonZero(v.BitDepth, avcCBitDepth(d))
		case "av1C":
			v.BitDepth = firstNonZero(v.BitDepth, av1CBitDepth(d))
		case "vpcC":
			if len(d) >= 12 {
				v.BitDepth = int(d[6] >> 4)
				if v.Transfer == "" {
					v.Transfer = transferName(int(d[9]))
				}
			}
		case "colr":
			if len(d) >= 10 && (string(d[:4]) == "nclx" || string(d[:4]) == "nclc") {
				if t := transferName(be16(d[6:])); t != "" {
					v.Transfer = t
				}
			}
		case "dvcC", "dvvC", "dvwC":
			dvRec = d
		}
	})
	if dvRec != nil {
		applyDVRecord(&v, dvRec)
	}
	return v
}

func parseMP4Audio(entries []byte) Audio {
	var a Audio
	if len(entries) < 36 {
		return a
	}
	size := be32(entries)
	if size < 36 || size > int64(len(entries)) {
		return a
	}
	e := entries[:size]
	typ := string(e[4:8])
	switch typ {
	case "ac-3":
		a.Codec = "ac3"
	case "ec-3":
		a.Codec = "eac3"
	case "mlpa":
		a.Codec = "truehd"
	case "dtsc":
		a.Codec = "dts"
	case "dtsh", "dtse":
		a.Codec = "dtshd"
	case "dtsl":
		a.Codec = "dtshd_ma"
	case "dtsx", "dtsy":
		a.Codec = "dtsx"
	case "mp4a":
		a.Codec = "aac"
	case "fLaC":
		a.Codec = "flac"
	case "Opus":
		a.Codec = "opus"
	case ".mp3":
		a.Codec = "mp3"
	case "lpcm", "ipcm", "sowt", "twos", "in24", "in32", "fpcm":
		a.Codec = "lpcm"
	default:
		return Audio{}
	}
	a.Channels = be16(e[24:])
	child := 36
	switch be16(e[16:]) { // QuickTime 的 SoundDescription 版本
	case 1:
		child += 16
	case 2:
		child += 36
	}
	if child > len(e) {
		return a
	}
	mp4SubBoxes(e[child:], func(t string, d []byte) {
		switch t {
		case "dac3":
			if len(d) >= 3 {
				acmod := int(d[1]>>3) & 7
				lfe := int(d[1]>>2) & 1
				a.Channels = acmodChannels[acmod] + lfe
			}
		case "dec3":
			if ch, atmos := parseDec3(d); ch > 0 {
				a.Channels, a.Atmos = ch, atmos
			}
		case "esds":
			// 第一个对象类型 0x69 / 0x6B 是 MP3 装在 mp4a 里
			if i := strings.Index(string(d), "\x04"); i >= 0 && i+2 < len(d) {
				if ot := d[i+2]; ot == 0x69 || ot == 0x6B {
					a.Codec = "mp3"
				}
			}
		}
	})
	return a
}

// acmodChannels AC-3 音频编码模式 → 主声道数（不含 LFE）
var acmodChannels = [8]int{2, 1, 2, 3, 3, 4, 4, 5}

// parseDec3 EC3SpecificBox（ETSI TS 102 366 附录 F）：第一个独立子流的声道 + 依赖子流扩展的声道；
// 结尾的 flag_ec3_extension_type_a 就是 JOC（杜比全景声）
func parseDec3(d []byte) (int, bool) {
	br := bitReader{b: d}
	br.skip(13)
	numInd := br.read(3) + 1
	ch := 0
	for i := 0; i < numInd; i++ {
		br.skip(2 + 5 + 1 + 1 + 3)
		acmod := br.read(3)
		lfe := br.read(1)
		br.skip(3)
		numDep := br.read(4)
		if i == 0 {
			ch = acmodChannels[acmod&7] + lfe
		}
		if numDep > 0 {
			loc := br.read(9)
			if i == 0 {
				ch += chanLocCount(loc)
			}
		} else {
			br.skip(1)
		}
	}
	if br.err || ch == 0 {
		return 0, false
	}
	atmos := false
	if br.left() >= 16 {
		br.skip(7)
		atmos = br.read(1) == 1
	}
	return ch, atmos
}

// chanLocCount 依赖子流 chan_loc 每一位代表的声道数（高位到低位）
func chanLocCount(loc int) int {
	per := [9]int{2, 2, 1, 1, 2, 2, 2, 1, 1} // Lc/Rc Lrs/Rrs Cs Ts Lsd/Rsd Lw/Rw Lvh/Rvh Cvh LFE2
	n := 0
	for i := 0; i < 9; i++ {
		if loc&(1<<(8-i)) != 0 {
			n += per[i]
		}
	}
	return n
}

// bitReader 大端按位读
type bitReader struct {
	b   []byte
	pos int // 位
	err bool
}

func (br *bitReader) read(n int) int {
	v := 0
	for i := 0; i < n; i++ {
		byteIdx := br.pos >> 3
		if byteIdx >= len(br.b) {
			br.err = true
			return 0
		}
		bit := (br.b[byteIdx] >> (7 - uint(br.pos&7))) & 1
		v = v<<1 | int(bit)
		br.pos++
	}
	return v
}

func (br *bitReader) skip(n int) { br.read(n) }
func (br *bitReader) left() int  { return len(br.b)*8 - br.pos }
