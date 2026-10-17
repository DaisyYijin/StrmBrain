package mediaprobe

import (
	"bytes"
	"encoding/binary"
	"errors"
	"testing"
)

// 夹具都是在测试里按规范拼出来的最小结构：只有元数据，没有任何音视频数据

type memReader []byte

func (m memReader) ReadAt(p []byte, off int64) (int, error) {
	if off >= int64(len(m)) {
		return 0, errors.New("越界")
	}
	n := copy(p, m[off:])
	return n, nil
}

// ---------- MKV ----------

func ebml(id uint64, payload ...[]byte) []byte {
	var idb []byte
	for v := id; v > 0; v >>= 8 {
		idb = append([]byte{byte(v)}, idb...)
	}
	body := bytes.Join(payload, nil)
	size := make([]byte, 8)
	binary.BigEndian.PutUint64(size, uint64(len(body)))
	size[0] = 0x01 // 8 字节长度
	return append(append(idb, size...), body...)
}

func ebmlU(id uint64, v uint64) []byte {
	b := make([]byte, 8)
	binary.BigEndian.PutUint64(b, v)
	return ebml(id, b)
}

func ebmlS(id uint64, s string) []byte { return ebml(id, []byte(s)) }

func mkvTracks() []byte {
	hvcC := make([]byte, 23)
	hvcC[0], hvcC[17] = 1, 2 // bitDepthLumaMinus8 = 2
	dvvC := []byte{1, 0, 8 << 1, 0, 1 << 4}
	video := ebml(ebmlIDTrackEntry,
		ebmlU(ebmlIDTrackType, 1),
		ebmlS(ebmlIDCodecID, "V_MPEGH/ISO/HEVC"),
		ebml(ebmlIDCodecPrivate, hvcC),
		ebmlU(ebmlIDDefaultDuration, 41708333), // 23.976
		ebml(ebmlIDVideo, ebmlU(ebmlIDPixelWidth, 3840), ebmlU(ebmlIDPixelHeight, 1600),
			ebml(ebmlIDColour, ebmlU(ebmlIDTransfer, 16))),
		ebml(ebmlIDBlockAddMapping, ebmlU(ebmlIDBlockAddIDType, 0x64767643), ebml(ebmlIDBlockAddExtra, dvvC)),
	)
	truehd := ebml(ebmlIDTrackEntry,
		ebmlU(ebmlIDTrackType, 2),
		ebmlS(ebmlIDCodecID, "A_TRUEHD"),
		ebmlS(ebmlIDName, "TrueHD Atmos 7.1"),
		ebml(ebmlIDAudio, ebmlU(ebmlIDChannels, 8)),
	)
	ac3 := ebml(ebmlIDTrackEntry,
		ebmlU(ebmlIDTrackType, 2),
		ebmlS(ebmlIDCodecID, "A_AC3"),
		ebmlU(ebmlIDFlagDefault, 0),
		ebmlS(ebmlIDLanguage, "chi"),
		ebml(ebmlIDAudio, ebmlU(ebmlIDChannels, 6)),
	)
	return ebml(ebmlIDTracks, video, truehd, ac3)
}

func TestProbeMKVTracksAtHead(t *testing.T) {
	file := append(ebml(ebmlIDHeader, ebmlS(0x4282, "matroska")),
		ebml(ebmlIDSegment, ebml(0x1549A966, ebmlU(0x2AD7B1, 1000000)), mkvTracks(), ebml(ebmlIDCluster, make([]byte, 64)))...)
	info, st, err := Probe(memReader(file), int64(len(file)), "x.mkv", DefaultLimits)
	if err != nil {
		t.Fatal(err)
	}
	v := info.Video
	if v == nil || v.Codec != "hevc" || v.Width != 3840 || v.BitDepth != 10 || v.Transfer != "pq" || !v.DV || v.DVProfile != 8 || v.FPS != 23.976 {
		t.Fatalf("视频轨不对: %+v", v)
	}
	if len(info.Audios) != 2 || info.Audios[0].Codec != "truehd" || !info.Audios[0].Atmos || info.Audios[0].Channels != 8 {
		t.Fatalf("音轨不对: %+v", info.Audios)
	}
	if a := info.Audios[1]; a.Default || a.Lang != "chi" {
		t.Fatalf("第二条音轨的默认标记 / 语言不对: %+v", a)
	}
	if main := info.MainAudio(true); main.Codec != "truehd" {
		t.Fatalf("普通文件按默认轨: %+v", main)
	}
	if st.Requests != 1 {
		t.Fatalf("Tracks 在头部只该读一块，实际 %d", st.Requests)
	}
}

// Tracks 放在媒体数据之后（有的封装器这么写），靠 SeekHead 跳过去
func TestProbeMKVTracksViaSeekHead(t *testing.T) {
	head := ebml(ebmlIDHeader, ebmlS(0x4282, "matroska"))
	cluster := ebml(ebmlIDCluster, make([]byte, 600<<10)) // 比一块大，Tracks 落在后面的块里
	// SeekHead 的位置相对 Segment 内容起点；先按占位算出长度
	seek := func(pos uint64) []byte {
		return ebml(ebmlIDSeekHead, ebml(ebmlIDSeek, ebmlU(ebmlIDSeekID, ebmlIDTracks), ebmlU(ebmlIDSeekPosition, pos)))
	}
	pos := uint64(len(seek(0)) + len(cluster))
	seg := ebml(ebmlIDSegment, seek(pos), cluster, mkvTracks())
	file := append(head, seg...)
	info, st, err := Probe(memReader(file), int64(len(file)), "x.mkv", DefaultLimits)
	if err != nil {
		t.Fatal(err)
	}
	if info.Video == nil || info.Video.Codec != "hevc" {
		t.Fatalf("没读到视频轨: %+v", info)
	}
	if st.Requests > 3 {
		t.Fatalf("不该把媒体数据整段读下来：读了 %d 块", st.Requests)
	}
}

// ---------- MP4 ----------

func box(typ string, payload ...[]byte) []byte {
	body := bytes.Join(payload, nil)
	b := make([]byte, 8, 8+len(body))
	binary.BigEndian.PutUint32(b, uint32(8+len(body)))
	copy(b[4:], typ)
	return append(b, body...)
}

func u16(v int) []byte { return []byte{byte(v >> 8), byte(v)} }
func u32(v int) []byte { return []byte{byte(v >> 24), byte(v >> 16), byte(v >> 8), byte(v)} }

type bitWriter struct {
	b   []byte
	pos int
}

func (w *bitWriter) put(n int, v int) {
	for i := n - 1; i >= 0; i-- {
		if w.pos%8 == 0 {
			w.b = append(w.b, 0)
		}
		if v>>uint(i)&1 == 1 {
			w.b[len(w.b)-1] |= 1 << uint(7-w.pos%8)
		}
		w.pos++
	}
}

func mp4Moov() []byte {
	hvcC := make([]byte, 23)
	hvcC[0], hvcC[17] = 1, 2
	visual := append(make([]byte, 24), u16(1920)...)
	visual = append(visual, u16(1080)...)
	visual = append(visual, make([]byte, 50)...)
	colr := box("colr", []byte("nclx"), u16(9), u16(16), u16(9), []byte{0})
	hvc1 := box("dvh1", visual, box("hvcC", hvcC), colr, box("dvcC", []byte{1, 0, 5 << 1, 0, 0}))
	stsdV := box("stsd", u32(0), u32(1), hvc1)
	stts := box("stts", u32(0), u32(1), u32(1000), u32(1001))
	mdhdV := box("mdhd", u32(0), u32(0), u32(0), u32(24000), u32(0), u16(0x55C4), u16(0))
	trakV := box("trak", box("tkhd", make([]byte, 84)),
		box("mdia", mdhdV, box("hdlr", u32(0), u32(0), []byte("vide"), make([]byte, 12)),
			box("minf", box("stbl", stsdV, stts))))

	var w bitWriter
	w.put(13, 768)
	w.put(3, 0)                                  // 一个独立子流
	w.put(2, 0)                                  // fscod
	w.put(5, 16)                                 // bsid
	w.put(1, 0)                                  // reserved
	w.put(1, 0)                                  // asvc
	w.put(3, 0)                                  // bsmod
	w.put(3, 7)                                  // acmod 3/2
	w.put(1, 1)                                  // lfe
	w.put(3, 0)                                  // reserved
	w.put(4, 1)                                  // 一个依赖子流
	w.put(9, 1<<7)                               // Lrs/Rrs → 7.1
	w.put(7, 0)                                  // reserved
	w.put(1, 1)                                  // JOC
	w.put(8, 16)                                 // complexity
	audio := append(make([]byte, 16), u16(2)...) // channelcount 写的 2，以 dec3 为准
	audio = append(audio, make([]byte, 10)...)
	ec3 := box("ec-3", audio, box("dec3", w.b))
	stsdA := box("stsd", u32(0), u32(1), ec3)
	mdhdA := box("mdhd", u32(0), u32(0), u32(0), u32(48000), u32(0), u16(0x15C7), u16(0)) // eng
	trakA := box("trak", box("mdia", mdhdA, box("hdlr", u32(0), u32(0), []byte("soun"), make([]byte, 12)),
		box("minf", box("stbl", stsdA))))
	return box("moov", box("mvhd", make([]byte, 100)), trakV, trakA)
}

func TestProbeMP4MoovAtTail(t *testing.T) {
	file := append(box("ftyp", []byte("isom"), u32(0)), box("mdat", make([]byte, 2<<20))...)
	file = append(file, mp4Moov()...)
	info, st, err := Probe(memReader(file), int64(len(file)), "x.mp4", DefaultLimits)
	if err != nil {
		t.Fatal(err)
	}
	v := info.Video
	if v == nil || v.Codec != "hevc" || v.Width != 1920 || v.BitDepth != 10 || !v.DV || v.DVProfile != 5 || v.Transfer != "pq" || v.FPS != 23.976 {
		t.Fatalf("视频轨不对: %+v", v)
	}
	if len(info.Audios) != 1 {
		t.Fatalf("音轨数不对: %+v", info.Audios)
	}
	if a := info.Audios[0]; a.Codec != "eac3" || a.Channels != 8 || !a.Atmos || a.Lang != "eng" {
		t.Fatalf("音轨不对: %+v", a)
	}
	if st.Requests > 4 {
		t.Fatalf("mdat 不该读：读了 %d 块", st.Requests)
	}
}

// ---------- 蓝光 ISO ----------

type isoBuilder struct{ sectors map[int][]byte }

func (b *isoBuilder) put(sector int, data []byte) {
	if b.sectors == nil {
		b.sectors = map[int][]byte{}
	}
	s := make([]byte, ((len(data)+udfSector-1)/udfSector)*udfSector)
	copy(s, data)
	for i := 0; i*udfSector < len(s); i++ {
		b.sectors[sector+i] = s[i*udfSector : (i+1)*udfSector]
	}
}

func (b *isoBuilder) bytes() []byte {
	max := 0
	for s := range b.sectors {
		if s > max {
			max = s
		}
	}
	out := make([]byte, (max+1)*udfSector)
	for s, d := range b.sectors {
		copy(out[s*udfSector:], d)
	}
	return out
}

func le16b(v int) []byte { return []byte{byte(v), byte(v >> 8)} }
func le32b(v int) []byte { return []byte{byte(v), byte(v >> 8), byte(v >> 16), byte(v >> 24)} }

func udfTag(id int) []byte {
	t := make([]byte, 16)
	copy(t, le16b(id))
	return t
}

func at(buf []byte, off int, v []byte) { copy(buf[off:], v) }

func longAD(length, lbn, part int) []byte {
	b := make([]byte, 16)
	at(b, 0, le32b(length))
	at(b, 4, le32b(lbn))
	at(b, 8, le16b(part))
	return b
}

func fid(name string, dir bool, icb []byte) []byte {
	n := append([]byte{8}, name...)
	b := make([]byte, 38+len(n))
	at(b, 0, udfTag(udfTagFID))
	if dir {
		b[18] = 0x02
	}
	b[19] = byte(len(n))
	at(b, 20, icb)
	at(b, 38, n)
	for len(b)%4 != 0 {
		b = append(b, 0)
	}
	return b
}

// fe 文件入口：adType 0 = short_ad、1 = long_ad、3 = 内容嵌在入口里
func fe(dir bool, size int, adType int, ads []byte) []byte {
	b := make([]byte, 176+len(ads))
	at(b, 0, udfTag(udfTagFE))
	if dir {
		b[27] = 4
	} else {
		b[27] = 5
	}
	at(b, 34, le16b(adType))
	binary.LittleEndian.PutUint64(b[56:], uint64(size))
	at(b, 172, le32b(len(ads)))
	at(b, 176, ads)
	return b
}

func mpls(version string, secs int, uhd bool) []byte {
	var stn []byte
	stn = append(stn, 0, 0) // reserved
	if uhd {
		stn = append(stn, 1, 2, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0) // 视频 1、音频 2、PG 1、杜比视界 1
	} else {
		stn = append(stn, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
	}
	entry := func(pid int) []byte { return []byte{9, 1, byte(pid >> 8), byte(pid), 0, 0, 0, 0, 0, 0} }
	if uhd {
		stn = append(stn, entry(0x1011)...)
		stn = append(stn, 5, 0x24, 8<<4|1, 1<<4, 0, 0) // HEVC 2160p 23.976 HDR10
		stn = append(stn, entry(0x1100)...)
		stn = append(stn, 5, 0x80, 3<<4|1, 'e', 'n', 'g') // 第一条是立体声 LPCM（评论音轨一类）
		stn = append(stn, entry(0x1101)...)
		stn = append(stn, 5, 0x83, 6<<4|1, 'e', 'n', 'g') // TrueHD 多声道
		stn = append(stn, entry(0x1200)...)
		stn = append(stn, 4, 0x90, 'c', 'h', 'i')
	} else {
		stn = append(stn, entry(0x1011)...)
		stn = append(stn, 5, 0x1B, 6<<4|1, 0, 0, 0) // AVC 1080p
		stn = append(stn, entry(0x1100)...)
		stn = append(stn, 5, 0x81, 3<<4|1, 'c', 'h', 'i')
	}
	item := []byte("00001M2TS")
	item = append(item, 0, 0, 0)
	item = append(item, u32(0)...)
	item = append(item, u32(secs*45000)...)
	item = append(item, make([]byte, 12)...)
	item = append(item, u16(len(stn))...)
	item = append(item, stn...)
	pl := append(u32(0), 0, 0)
	pl = append(pl, u16(1)...)
	pl = append(pl, u16(0)...)
	pl = append(pl, u16(len(item))...)
	pl = append(pl, item...)
	head := append([]byte("MPLS"+version), u32(40)...)
	head = append(head, make([]byte, 40-len(head))...)
	return append(head, pl...)
}

// buildBluray 拼一个 UDF 镜像。meta 为真时按 UDF 2.50 的蓝光写法：目录与文件入口放在元数据分区里，
// 一个播放列表的内容用 long_ad 指物理分区，另一个用 short_ad（越过元数据分区，应退回物理分区）
func buildBluray(meta bool) []byte {
	var b isoBuilder
	const physStart = 300
	avdp := udfTag(udfTagAVDP)
	avdp = append(avdp, le32b(4*udfSector)...)
	avdp = append(avdp, le32b(32)...)
	b.put(256, avdp)

	pd := make([]byte, 256)
	at(pd, 0, udfTag(udfTagPD))
	at(pd, 22, le16b(0))
	at(pd, 188, le32b(physStart))
	b.put(32, pd)

	lvd := make([]byte, 600)
	at(lvd, 0, udfTag(udfTagLVD))
	at(lvd, 212, le32b(udfSector))
	dirPart := 0
	maps := []byte{1, 6, 1, 0, 0, 0}
	if meta {
		dirPart = 1
		m := make([]byte, 64)
		m[0], m[1] = 2, 64
		copy(m[5:], "*UDF Metadata Partition")
		at(m, 38, le16b(0))
		at(m, 40, le32b(50))
		maps = append(maps, m...)
		at(lvd, 268, le32b(2))
	} else {
		at(lvd, 268, le32b(1))
	}
	at(lvd, 248, longAD(udfSector, 0, dirPart))
	at(lvd, 440, maps)
	b.put(33, lvd)
	b.put(34, udfTag(udfTagTD))

	// 目录与文件入口所在的块：metaBase 是元数据分区第 0 块在物理分区里的位置
	metaBase := 0
	if meta {
		metaBase = 100
		ad := append(le32b(10*udfSector), le32b(metaBase)...)
		b.put(physStart+50, fe(false, 10*udfSector, 0, ad))
	}
	dirBlock := func(lbn int, data []byte) { b.put(physStart+metaBase+lbn, data) }

	fsd := make([]byte, 512)
	at(fsd, 0, udfTag(udfTagFSD))
	at(fsd, 400, longAD(udfSector, 1, dirPart))
	dirBlock(0, fsd)

	root := fid("BDMV", true, longAD(udfSector, 2, dirPart))
	dirBlock(1, fe(true, len(root), 3, root))
	bdmv := append(fid("PLAYLIST", true, longAD(udfSector, 3, dirPart)), fid("STREAM", true, longAD(udfSector, 6, dirPart))...)
	dirBlock(2, fe(true, len(bdmv), 3, bdmv))
	stream := fid("00001.m2ts", false, longAD(udfSector, 7, dirPart))
	dirBlock(6, fe(true, len(stream), 3, stream))
	// 主片段 m2ts：LPCM 立体声（PES 开头 4 字节头）+ TrueHD 全景声 7.1
	ts := &tsBuilder{pkt: 192}
	for k := int64(0); k < 4; k++ {
		ts.packet(0x1100, true, pes(0xBD, k*1800, append([]byte{0x03, 0xC0, 0x31, 0x40}, make([]byte, 64)...)))
		ts.packet(0x1101, true, pes(0xBD, k*1800, append(ac3Frame(), trueHDFrame(true)...)))
	}
	b.put(physStart+220, ts.out)
	dirBlock(7, fe(false, len(ts.out), 1, longAD(len(ts.out), 220, 0)))
	list := append(fid("00000.mpls", false, longAD(udfSector, 4, dirPart)), fid("00800.mpls", false, longAD(udfSector, 5, dirPart))...)
	dirBlock(3, fe(true, len(list), 3, list))

	short := mpls("0300", 60, false)
	long := mpls("0300", 7200, true)
	// 播放列表内容放在物理分区第 200 / 210 块
	b.put(physStart+200, short)
	b.put(physStart+210, long)
	if meta {
		dirBlock(4, fe(false, len(short), 1, longAD(len(short), 200, 0)))
		dirBlock(5, fe(false, len(long), 0, append(le32b(len(long)), le32b(210)...)))
	} else {
		dirBlock(4, fe(false, len(short), 0, append(le32b(len(short)), le32b(200)...)))
		dirBlock(5, fe(false, len(long), 1, longAD(len(long), 210, 0)))
	}
	return b.bytes()
}

func TestProbeBlurayISO(t *testing.T) {
	for _, meta := range []bool{false, true} {
		img := buildBluray(meta)
		info, _, err := Probe(memReader(img), int64(len(img)), "Movie.iso", DefaultLimits)
		if err != nil {
			t.Fatalf("meta=%v: %v", meta, err)
		}
		if info.Disc != "uhd-bluray" {
			t.Fatalf("meta=%v: 应认成 UHD 蓝光: %+v", meta, info)
		}
		v := info.Video
		if v == nil || v.Codec != "hevc" || v.Height != 2160 || v.BitDepth != 10 || v.Transfer != "pq" || !v.DV || v.FPS != 23.976 {
			t.Fatalf("meta=%v: 应挑最长的播放列表: %+v", meta, v)
		}
		main := info.MainAudio(false)
		if main == nil || main.Codec != "truehd" || main.Lang != "eng" {
			t.Fatalf("meta=%v: 主音轨应是 TrueHD: %+v", meta, main)
		}
	}
}

func TestBlockReaderBudget(t *testing.T) {
	data := make([]byte, 10<<20)
	r := newBlockReader(memReader(data), int64(len(data)), Limits{BlockSize: 1 << 20, MaxRequests: 2})
	if _, err := r.read(0, 10); err != nil {
		t.Fatal(err)
	}
	if _, err := r.read(5<<20, 10); err != nil {
		t.Fatal(err)
	}
	if _, err := r.read(0, 10); err != nil {
		t.Fatal("读过的块应命中缓存")
	}
	if _, err := r.read(8<<20, 10); !errors.Is(err, ErrBudget) {
		t.Fatalf("超过上限应报 ErrBudget，实际 %v", err)
	}
	// 跨块读
	r2 := newBlockReader(memReader(data), int64(len(data)), Limits{BlockSize: 1 << 20, MaxRequests: 4})
	b, err := r2.read(1<<20-4, 8)
	if err != nil || len(b) != 8 {
		t.Fatalf("跨块读: %v %d", err, len(b))
	}
}

func TestProbeUnsupported(t *testing.T) {
	data := []byte("RIFF....AVI LIST")
	data = append(data, make([]byte, 100)...)
	if _, _, err := Probe(memReader(data), int64(len(data)), "x.avi", DefaultLimits); !errors.Is(err, ErrUnsupported) {
		t.Fatalf("AVI 应报不支持: %v", err)
	}
}
