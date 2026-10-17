package mediaprobe

import (
	"bytes"
	"encoding/binary"
	"testing"
)

// 二期夹具：帧头、参数集与容器都是按规范在测试里拼的，没有任何真实音视频数据

func (w *bitWriter) ue(v int) {
	x := v + 1
	n := 0
	for t := x; t > 0; t >>= 1 {
		n++
	}
	w.put(n-1, 0)
	w.put(n, x)
}

func (w *bitWriter) bytes() []byte { return w.b }

// escapeRBSP 插入防竞争字节
func escapeRBSP(b []byte) []byte {
	var out []byte
	zeros := 0
	for _, c := range b {
		if zeros >= 2 && c <= 3 {
			out = append(out, 3)
			zeros = 0
		}
		out = append(out, c)
		if c == 0 {
			zeros++
		} else {
			zeros = 0
		}
	}
	return out
}

func nal(header []byte, rbsp []byte) []byte {
	return append(append([]byte{0, 0, 0, 1}, header...), escapeRBSP(rbsp)...)
}

// ---------- 帧头 ----------

func ac3Frame() []byte {
	var w bitWriter
	w.put(16, 0x0B77)
	w.put(16, 0)
	w.put(2, 0)
	w.put(6, 20)
	w.put(5, 8) // bsid
	w.put(3, 0)
	w.put(3, 7) // 3/2
	w.put(2, 0) // cmixlev
	w.put(2, 0) // surmixlev
	w.put(1, 1) // lfe
	return append(w.bytes(), make([]byte, 16)...)
}

func eac3Frame() []byte {
	var w bitWriter
	w.put(16, 0x0B77)
	w.put(2, 0) // 独立子流
	w.put(3, 0) // substreamid
	w.put(11, 100)
	w.put(2, 0)  // fscod
	w.put(2, 3)  // numblkscod
	w.put(3, 2)  // 2/0
	w.put(1, 0)  // lfe
	w.put(5, 16) // bsid
	return append(w.bytes(), make([]byte, 16)...)
}

func trueHDFrame(atmos bool) []byte {
	var w bitWriter
	w.put(16, 0x1234) // 访问单元头
	w.put(16, 0)
	w.put(32, 0xF8726FBA)
	w.put(4, 0)
	w.put(4, 0)
	w.put(2, 0)
	w.put(2, 0)
	w.put(5, 0x0F) // 6 声道布局
	w.put(2, 0)
	w.put(13, 0x4F) // 7.1：L/R C LFE Ls/Rs Lb/Rb
	w.put(16, 0xB752)
	w.put(16, 0)
	w.put(16, 0)
	w.put(16, 0)
	if atmos {
		w.put(4, 4)
		w.put(4, 0)
		w.put(8, 0x80)
	} else {
		w.put(4, 3)
		w.put(4, 0)
		w.put(8, 0)
	}
	return append(w.bytes(), make([]byte, 16)...)
}

// dtsFrame 核心 5.1 + 扩展子流（总声道 8）；ma 带 XLL，x 再带 DTS:X 的同步字
func dtsFrame(ma, x bool) []byte {
	var w bitWriter
	w.put(32, 0x7FFE8001)
	w.put(1, 1)
	w.put(5, 31)
	w.put(1, 0)
	w.put(7, 15)
	w.put(14, 1000)
	w.put(6, 9) // 5 声道
	w.put(4, 13)
	w.put(5, 15)
	w.put(10, 0)
	w.put(2, 1) // LFE
	core := append(w.bytes(), make([]byte, 8)...)

	var e bitWriter
	e.put(32, 0x64582025)
	e.put(8, 0)
	e.put(2, 0) // nExtSSIndex
	e.put(1, 0) // 短头
	e.put(8, 40)
	e.put(16, 2000)
	e.put(1, 1) // 静态字段
	e.put(2, 0)
	e.put(3, 0)
	e.put(1, 0) // 无时间戳
	e.put(3, 0) // 1 个呈现
	e.put(3, 0) // 1 个资源
	e.put(1, 1) // 呈现 0 用子流 0
	e.put(8, 1) // 资源掩码
	e.put(1, 0) // 无混音元数据
	e.put(16, 1500)
	e.put(9, 30)
	e.put(3, 0)
	e.put(1, 0)
	e.put(1, 0)
	e.put(1, 0)
	e.put(5, 23)
	e.put(4, 8)
	e.put(8, 7) // 8 声道
	ext := append(e.bytes(), make([]byte, 8)...)
	if ma {
		ext = append(ext, 0x41, 0xA2, 0x95, 0x47, 0, 0)
	}
	if x {
		ext = append(ext, 0x02, 0x00, 0x08, 0x50, 0, 0)
	}
	return append(core, ext...)
}

func TestAudioFrameHeaders(t *testing.T) {
	if fi, ok := parseAC3(append([]byte{1, 2, 3}, ac3Frame()...)); !ok || fi.codec != "ac3" || fi.channels != 6 {
		t.Fatalf("AC-3: %+v %v", fi, ok)
	}
	if fi, ok := parseAC3(eac3Frame()); !ok || fi.codec != "eac3" || fi.channels != 2 {
		t.Fatalf("E-AC-3: %+v %v", fi, ok)
	}
	// 蓝光 TrueHD 流里先是 AC-3 核心，再是 TrueHD 主同步
	if fi, ok := parseAudioFrames("truehd", append(ac3Frame(), trueHDFrame(true)...)); !ok || fi.channels != 8 || !fi.atmos {
		t.Fatalf("TrueHD 全景声: %+v %v", fi, ok)
	}
	if fi, _ := parseTrueHD(trueHDFrame(false)); fi.atmos || fi.channels != 8 {
		t.Fatalf("TrueHD 7.1: %+v", fi)
	}
	cases := []struct {
		ma, x bool
		codec string
	}{{false, false, "dtshd"}, {true, false, "dtshd_ma"}, {true, true, "dtsx"}}
	for _, c := range cases {
		fi, ok := parseDTS(dtsFrame(c.ma, c.x))
		if !ok || fi.codec != c.codec || fi.channels != 8 {
			t.Fatalf("DTS ma=%v x=%v: %+v", c.ma, c.x, fi)
		}
	}
	core := dtsFrame(false, false)[:16]
	if fi, ok := parseDTS(core); !ok || fi.codec != "dts" || fi.channels != 6 {
		t.Fatalf("DTS 核心 5.1: %+v", fi)
	}
	if bdLPCMChannels([]byte{0, 0x80, 0xB1, 0}) != 8 {
		t.Fatal("蓝光 LPCM 7.1")
	}
}

// ---------- 参数集 ----------

func h264SPS(timing bool) []byte {
	var w bitWriter
	w.put(8, 100)
	w.put(8, 0)
	w.put(8, 40)
	w.ue(0)
	w.ue(1) // 4:2:0
	w.ue(2) // 10 bit
	w.ue(2)
	w.put(1, 0)
	w.put(1, 0)
	w.ue(0)
	w.ue(0)
	w.ue(0)
	w.ue(4)
	w.put(1, 0)
	w.ue(119) // 1920
	w.ue(67)  // 1088
	w.put(1, 1)
	w.put(1, 1)
	w.put(1, 1) // 裁边
	w.ue(0)
	w.ue(0)
	w.ue(0)
	w.ue(4) // 下边裁 8 行 → 1080
	w.put(1, 1)
	w.put(1, 0)
	w.put(1, 0)
	w.put(1, 1)
	w.put(3, 5)
	w.put(1, 0)
	w.put(1, 1)
	w.put(8, 9)
	w.put(8, 1) // BT.709
	w.put(8, 9)
	w.put(1, 0)
	if timing {
		w.put(1, 1)
		w.put(32, 1001)
		w.put(32, 48000)
		w.put(1, 1)
	} else {
		w.put(1, 0)
	}
	w.put(1, 1) // rbsp_stop_bit
	return nal([]byte{0x67}, w.bytes())
}

func hevcSPS() []byte {
	var w bitWriter
	w.put(4, 0)
	w.put(3, 0)
	w.put(1, 1)
	w.put(2, 0)
	w.put(1, 0)
	w.put(5, 2)
	w.put(32, 0x20000000)
	w.put(48, 0)
	w.put(8, 153)
	w.ue(0)
	w.ue(1)
	w.ue(3840)
	w.ue(2160)
	w.put(1, 0)
	w.ue(2)
	w.ue(2)
	w.ue(4)
	w.put(1, 1)
	w.ue(0)
	w.ue(0)
	w.ue(0)
	w.ue(0)
	w.ue(1)
	w.ue(0)
	w.ue(1)
	w.ue(0)
	w.ue(0)
	w.put(1, 0) // scaling
	w.put(1, 0)
	w.put(1, 1)
	w.put(1, 0) // pcm
	w.ue(2)     // 两组短期参考
	w.ue(1)
	w.ue(0)
	w.ue(0)
	w.put(1, 1)
	w.put(1, 1) // 第二组用帧间预测
	w.put(1, 0)
	w.ue(0)
	w.put(1, 1)
	w.put(1, 0)
	w.put(1, 0)
	w.put(1, 0) // long_term
	w.put(1, 1)
	w.put(1, 1)
	w.put(1, 1) // VUI
	w.put(1, 0)
	w.put(1, 0)
	w.put(1, 1)
	w.put(3, 5)
	w.put(1, 0)
	w.put(1, 1)
	w.put(8, 9)
	w.put(8, 18) // HLG
	w.put(8, 9)
	w.put(1, 0)
	w.put(3, 0)
	w.put(1, 0)
	w.put(1, 1)
	w.put(32, 1001)
	w.put(32, 60000)
	w.put(1, 1)
	return nal([]byte{0x42, 0x01}, w.bytes())
}

func TestVideoParamSets(t *testing.T) {
	v, ok := videoFromES("h264", append([]byte{0, 0, 0, 1, 0x09, 0xF0}, h264SPS(true)...))
	if !ok || v.Width != 1920 || v.Height != 1080 || v.BitDepth != 10 || v.Transfer != "sdr" || v.FPS != 23.976 {
		t.Fatalf("H.264 SPS: %+v %v", v, ok)
	}
	sei := nal([]byte{0x4E, 0x01}, []byte{4, 7, 0xB5, 0x00, 0x3C, 0x00, 0x01, 0x04, 0x00, 0x80})
	rpu := nal([]byte{0x7C, 0x01}, []byte{0x19, 0x08, 0x09})
	v, ok = videoFromES("hevc", bytes.Join([][]byte{hevcSPS(), sei, rpu}, nil))
	if !ok || v.Width != 3840 || v.Height != 2160 || v.BitDepth != 10 || v.Transfer != "hlg" || v.FPS != 59.94 || !v.HDR10Plus || !v.DV {
		t.Fatalf("HEVC SPS / SEI / RPU: %+v %v", v, ok)
	}
	m2 := []byte{0, 0, 1, 0xB3, 0x2D, 0x02, 0x40, 0x33, 0, 0, 0, 0}
	if v, ok := videoFromES("mpeg2", m2); !ok || v.Width != 720 || v.Height != 576 || v.FPS != 25 {
		t.Fatalf("MPEG-2 序列头: %+v", v)
	}
}

// ---------- TS ----------

type tsBuilder struct {
	pkt  int
	out  []byte
	cont map[int]int
}

func (b *tsBuilder) packet(pid int, pusi bool, payload []byte) {
	for first := true; first || len(payload) > 0; first = false {
		n := len(payload)
		if n > 184 {
			n = 184
		}
		p := make([]byte, 4, 188)
		p[0] = 0x47
		p[1] = byte(pid >> 8 & 0x1F)
		if pusi && first {
			p[1] |= 0x40
		}
		p[2] = byte(pid)
		if b.cont == nil {
			b.cont = map[int]int{}
		}
		cc := b.cont[pid]
		b.cont[pid] = (cc + 1) & 15
		if n < 184 { // 用自适应字段补齐
			p[3] = 0x30 | byte(cc)
			af := 183 - n
			p = append(p, byte(af))
			if af > 0 {
				p = append(p, 0)
				for k := 1; k < af; k++ {
					p = append(p, 0xFF)
				}
			}
		} else {
			p[3] = 0x10 | byte(cc)
		}
		p = append(p, payload[:n]...)
		payload = payload[n:]
		if b.pkt == 192 {
			b.out = append(b.out, 0, 0, 0, 0)
		}
		b.out = append(b.out, p...)
	}
}

func psi(tableID byte, body []byte) []byte {
	l := len(body) + 4
	sec := []byte{0, tableID, 0xB0 | byte(l>>8), byte(l)}
	sec = append(sec, body...)
	return append(sec, 0, 0, 0, 0) // CRC 不校验
}

type tsES struct {
	typ  byte
	pid  int
	desc []byte
}

func (b *tsBuilder) tables(streams []tsES) {
	b.packet(0, true, psi(0, []byte{0, 1, 0xC1, 0, 0, 0, 1, 0xE1, 0x00}))
	body := []byte{0, 1, 0xC1, 0, 0, 0xE1, 0x01, 0xF0, 0}
	for _, s := range streams {
		body = append(body, s.typ, 0xE0|byte(s.pid>>8), byte(s.pid), 0xF0|byte(len(s.desc)>>8), byte(len(s.desc)))
		body = append(body, s.desc...)
	}
	b.packet(0x100, true, psi(2, body))
}

func pes(streamID byte, pts int64, data []byte) []byte {
	h := []byte{0, 0, 1, streamID, 0, 0, 0x80, 0x80, 5,
		byte(0x21 | (pts>>29)&0x0E), byte(pts >> 22), byte(1 | (pts>>14)&0xFE), byte(pts >> 7), byte(1 | (pts<<1)&0xFE)}
	return append(h, data...)
}

func TestProbeTS(t *testing.T) {
	b := &tsBuilder{pkt: 188}
	b.tables([]tsES{
		{typ: 0x1B, pid: 0x101},
		{typ: 0x81, pid: 0x102, desc: []byte{0x0A, 4, 'c', 'h', 'i', 0}},
		{typ: 0x06, pid: 0x103, desc: []byte{0x7A, 1, 0, 0x0A, 4, 'e', 'n', 'g', 0}},
	})
	for k := int64(0); k < 8; k++ {
		es := []byte{0, 0, 0, 1, 0x09, 0xF0}
		if k == 0 {
			es = append(es, h264SPS(false)...)
		}
		b.packet(0x101, true, pes(0xE0, 1000+k*3754, append(es, bytes.Repeat([]byte{0x11}, 300)...)))
		b.packet(0x102, true, pes(0xBD, 1000+k*2880, ac3Frame()))
		b.packet(0x103, true, pes(0xBD, 1000+k*2880, eac3Frame()))
	}
	file := b.out
	info, _, err := Probe(memReader(file), int64(len(file)), "x.ts", DefaultLimits)
	if err != nil {
		t.Fatal(err)
	}
	v := info.Video
	if info.Container != "ts" || v == nil || v.Codec != "h264" || v.Width != 1920 || v.BitDepth != 10 || v.FPS != 23.976 {
		t.Fatalf("视频（帧率按时间戳算）: %+v %+v", info, v)
	}
	if len(info.Audios) != 2 || info.Audios[0].Codec != "ac3" || info.Audios[0].Channels != 6 || info.Audios[0].Lang != "chi" || !info.Audios[0].Default {
		t.Fatalf("AC-3 音轨: %+v", info.Audios)
	}
	if a := info.Audios[1]; a.Codec != "eac3" || a.Channels != 2 || a.Lang != "eng" {
		t.Fatalf("E-AC-3（私有流 + 描述符）: %+v", a)
	}
}

func TestProbeM2TS(t *testing.T) {
	b := &tsBuilder{pkt: 192}
	b.tables([]tsES{
		{typ: 0x24, pid: 0x1011},
		{typ: 0x24, pid: 0x1015}, // 杜比视界增强层
		{typ: 0x83, pid: 0x1100},
		{typ: 0x86, pid: 0x1101},
	})
	for k := int64(0); k < 6; k++ {
		es := []byte{}
		if k == 0 {
			es = append(es, hevcSPS()...)
		}
		b.packet(0x1011, true, pes(0xE0, k*1501, append(es, bytes.Repeat([]byte{0x22}, 200)...)))
		b.packet(0x1100, true, pes(0xBD, k*1501, append(ac3Frame(), trueHDFrame(true)...)))
		b.packet(0x1101, true, pes(0xBD, k*1501, dtsFrame(true, true)))
	}
	file := b.out
	info, _, err := Probe(memReader(file), int64(len(file)), "00001.m2ts", DefaultLimits)
	if err != nil {
		t.Fatal(err)
	}
	v := info.Video
	if info.Container != "m2ts" || v == nil || v.Codec != "hevc" || v.Height != 2160 || !v.DV {
		t.Fatalf("视频 + 双层杜比视界: %+v %+v", info, v)
	}
	main := info.MainAudio(false)
	if main.Codec != "truehd" || main.Channels != 8 || !main.Atmos {
		t.Fatalf("主音轨应是 TrueHD 全景声 7.1: %+v", main)
	}
	if a := info.Audios[1]; a.Codec != "dtsx" || a.Channels != 8 {
		t.Fatalf("PMT 写的 DTS-HD MA，帧头认出 DTS:X: %+v", a)
	}
}

// ---------- 蓝光 ISO 里的 m2ts ----------

func TestProbeBlurayISOReadsM2TS(t *testing.T) {
	for _, meta := range []bool{false, true} {
		img := buildBluray(meta)
		info, st, err := Probe(memReader(img), int64(len(img)), "Movie.iso", DefaultLimits)
		if err != nil {
			t.Fatal(err)
		}
		main := info.MainAudio(false)
		if main.Codec != "truehd" || main.Channels != 8 || !main.Atmos {
			t.Fatalf("meta=%v: 主音轨应从 m2ts 补上 7.1 与全景声: %+v", meta, main)
		}
		if lp := info.Audios[0]; lp.Codec != "lpcm" || lp.Channels != 2 {
			t.Fatalf("meta=%v: LPCM 声道: %+v", meta, lp)
		}
		if st.Requests > 6 {
			t.Fatalf("meta=%v: 请求太多 %d", meta, st.Requests)
		}
	}
}

// ---------- DVD ISO ----------

func TestProbeDVDISO(t *testing.T) {
	var b isoBuilder
	const physStart = 300
	avdp := append(udfTag(udfTagAVDP), le32b(4*udfSector)...)
	b.put(256, append(avdp, le32b(32)...))
	pd := make([]byte, 256)
	at(pd, 0, udfTag(udfTagPD))
	at(pd, 188, le32b(physStart))
	b.put(32, pd)
	lvd := make([]byte, 600)
	at(lvd, 0, udfTag(udfTagLVD))
	at(lvd, 212, le32b(udfSector))
	at(lvd, 248, longAD(udfSector, 0, 0))
	at(lvd, 268, le32b(1))
	at(lvd, 440, []byte{1, 6, 1, 0, 0, 0})
	b.put(33, lvd)
	b.put(34, udfTag(udfTagTD))
	fsd := make([]byte, 512)
	at(fsd, 0, udfTag(udfTagFSD))
	at(fsd, 400, longAD(udfSector, 1, 0))
	b.put(physStart, fsd)
	root := fid("VIDEO_TS", true, longAD(udfSector, 2, 0))
	b.put(physStart+1, fe(true, len(root), 3, root))
	list := bytes.Join([][]byte{
		fid("VTS_01_0.IFO", false, longAD(udfSector, 3, 0)),
		fid("VTS_01_1.VOB", false, longAD(udfSector, 4, 0)),
		fid("VTS_02_0.IFO", false, longAD(udfSector, 5, 0)),
		fid("VTS_02_1.VOB", false, longAD(udfSector, 6, 0)),
	}, nil)
	b.put(physStart+2, fe(true, len(list), 3, list))
	ifo := func(pal bool, audio ...[]byte) []byte {
		d := make([]byte, 0x300)
		copy(d, "DVDVIDEO-VTS")
		if pal {
			d[0x200] = 0x5C
		} else {
			d[0x200] = 0x4C
		}
		binary.BigEndian.PutUint16(d[0x202:], uint16(len(audio)))
		for i, a := range audio {
			copy(d[0x204+i*8:], a)
		}
		return d
	}
	main := ifo(true, []byte{0x04, 0x05, 'z', 'h'}, []byte{0xC4, 0x05, 'e', 'n'})
	extra := ifo(false, []byte{0x04, 0x01, 'e', 'n'})
	b.put(physStart+20, main)
	b.put(physStart+30, extra)
	b.put(physStart+3, fe(false, len(main), 1, longAD(len(main), 20, 0)))
	b.put(physStart+4, fe(false, 4<<30, 1, nil)) // 只看大小：主标题集 4 GB
	b.put(physStart+5, fe(false, len(extra), 1, longAD(len(extra), 30, 0)))
	b.put(physStart+6, fe(false, 100<<20, 1, nil))
	img := b.bytes()
	info, _, err := Probe(memReader(img), int64(len(img)), "Movie.iso", DefaultLimits)
	if err != nil {
		t.Fatal(err)
	}
	v := info.Video
	if info.Disc != "dvd" || v == nil || v.Codec != "mpeg2" || v.Width != 720 || v.Height != 576 || v.FPS != 25 {
		t.Fatalf("应读主标题集（VOB 最大的那个）: %+v %+v", info, v)
	}
	if len(info.Audios) != 2 || info.Audios[0].Codec != "ac3" || info.Audios[0].Channels != 6 || info.Audios[0].Lang != "zh" {
		t.Fatalf("音轨: %+v", info.Audios)
	}
	if m := info.MainAudio(false); m.Codec != "dts" {
		t.Fatalf("光盘主音轨挑最好的: %+v", m)
	}
}

// ---------- MKV 读音频块 ----------

func TestProbeMKVReadsDTSBlock(t *testing.T) {
	tracks := ebml(ebmlIDTracks,
		ebml(ebmlIDTrackEntry, ebmlU(ebmlIDTrackNumber, 1), ebmlU(ebmlIDTrackType, 1), ebmlS(ebmlIDCodecID, "V_MPEG4/ISO/AVC"),
			ebml(ebmlIDVideo, ebmlU(ebmlIDPixelWidth, 1920), ebmlU(ebmlIDPixelHeight, 1080))),
		ebml(ebmlIDTrackEntry, ebmlU(ebmlIDTrackNumber, 2), ebmlU(ebmlIDTrackType, 2), ebmlS(ebmlIDCodecID, "A_DTS"),
			ebml(ebmlIDAudio, ebmlU(ebmlIDChannels, 6))),
		ebml(ebmlIDTrackEntry, ebmlU(ebmlIDTrackNumber, 3), ebmlU(ebmlIDTrackType, 2), ebmlS(ebmlIDCodecID, "A_TRUEHD"),
			ebml(ebmlIDAudio, ebmlU(ebmlIDChannels, 8))),
	)
	block := func(track byte, data []byte) []byte {
		return ebml(0xA3, append([]byte{0x80 | track, 0, 0, 0x80}, data...))
	}
	cluster := ebml(ebmlIDCluster, ebmlU(0xE7, 0),
		block(1, make([]byte, 100)),
		block(2, dtsFrame(true, false)),
		ebml(0xA0, ebml(0xA1, append([]byte{0x83, 0, 0, 0}, trueHDFrame(true)...))),
	)
	file := append(ebml(ebmlIDHeader, ebmlS(0x4282, "matroska")), ebml(ebmlIDSegment, tracks, cluster)...)
	info, _, err := Probe(memReader(file), int64(len(file)), "x.mkv", DefaultLimits)
	if err != nil {
		t.Fatal(err)
	}
	if a := info.Audios[0]; a.Codec != "dtshd_ma" || a.Channels != 8 {
		t.Fatalf("没写轨道名的 A_DTS 按帧头认: %+v", a)
	}
	if a := info.Audios[1]; !a.Atmos {
		t.Fatalf("TrueHD 全景声按主同步头认: %+v", a)
	}
}
