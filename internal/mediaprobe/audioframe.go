package mediaprobe

import "bytes"

// ==================== 音频帧头 ====================
//
// 容器里只写了「这是 TrueHD / DTS」的地方（蓝光 STN、MKV 的 A_DTS、TS 的 PMT），
// 声道数、全景声、DTS-HD MA 还是 DTS:X 得看码流本身。这里只读第一帧（或第一个同步头）的头部字段，
// 字段位置对照 ETSI TS 102 366（AC-3 / E-AC-3）、ETSI TS 102 114（DTS），
// TrueHD 的主同步头按 FFmpeg libavcodec/mlp_parse.c 的读法（只看思路，代码自己写）。

// audioFrameInfo 从帧头读到的
type audioFrameInfo struct {
	codec    string // 认得出更细的编码时给（dtshd_ma / dtsx / eac3），否则空
	channels int
	atmos    bool
}

// parseAudioFrames 在一段码流里按编码找第一个能解析的帧头。codec 是容器给的编码
func parseAudioFrames(codec string, b []byte) (audioFrameInfo, bool) {
	switch codec {
	case "truehd":
		// 蓝光的 TrueHD 流里交错着一路 AC-3 兼容核心，主同步头才是 TrueHD 自己的
		if fi, ok := parseTrueHD(b); ok {
			return fi, true
		}
	case "ac3", "eac3":
		return parseAC3(b)
	case "dts", "dtshd", "dtshd_ma", "dtsx":
		return parseDTS(b)
	}
	return audioFrameInfo{}, false
}

// ---------- AC-3 / E-AC-3 ----------

// parseAC3 第一个 0x0B77 同步字后的 BSI：bsid ≤ 10 是 AC-3，11–16 是 E-AC-3
func parseAC3(b []byte) (audioFrameInfo, bool) {
	for i := 0; i+8 <= len(b); i++ {
		if b[i] != 0x0B || b[i+1] != 0x77 {
			continue
		}
		bsid := int(b[i+5] >> 3)
		br := bitReader{b: b[i+2:]}
		var acmod, lfe int
		codec := "ac3"
		switch {
		case bsid <= 10:
			br.skip(16 + 2 + 6) // crc1 fscod frmsizecod
			br.skip(5 + 3)      // bsid bsmod
			acmod = br.read(3)
			if acmod&1 != 0 && acmod != 1 {
				br.skip(2) // cmixlev
			}
			if acmod&4 != 0 {
				br.skip(2) // surmixlev
			}
			if acmod == 2 {
				br.skip(2) // dsurmod
			}
			lfe = br.read(1)
		case bsid <= 16:
			codec = "eac3"
			strmtyp := br.read(2)
			if strmtyp == 1 { // 依赖子流：先找独立子流
				continue
			}
			br.skip(3 + 11) // substreamid frmsiz
			if br.read(2) == 3 {
				br.skip(2) // fscod2
			} else {
				br.skip(2) // numblkscod
			}
			acmod = br.read(3)
			lfe = br.read(1)
		default:
			continue
		}
		if br.err {
			return audioFrameInfo{}, false
		}
		return audioFrameInfo{codec: codec, channels: acmodChannels[acmod] + lfe}, true
	}
	return audioFrameInfo{}, false
}

// ---------- TrueHD ----------

// thdChannelBits 8 声道布局（13 位，低位起）每一位代表几个声道
var thdChannelBits = [13]int{2, 1, 1, 2, 2, 2, 2, 1, 1, 2, 2, 1, 1}

// parseTrueHD 找主同步头 F8 72 6F BA：
// 采样率 4 位、保留 4 位、两个声道修饰各 2 位、6 声道布局 5 位、修饰 2 位、8 声道布局 13 位；
// 签名 B752、flags 16、保留 16、可变码率 1 + 峰值码率 15、子流数 4、保留 2、扩展子流信息 2、子流信息 8。
// 全景声：子流信息最高位置位 = 码流里有 16 声道呈现（MediaInfo 按它认 Atmos；全景声 TrueHD 同时是 4 个子流，
// 普通 7.1 是 3 个）。只看这一位，不同时要求子流数，免得哪种封装把子流数写少了就漏认
func parseTrueHD(b []byte) (audioFrameInfo, bool) {
	sync := []byte{0xF8, 0x72, 0x6F, 0xBA}
	for off := 0; ; {
		i := bytes.Index(b[off:], sync)
		if i < 0 {
			return audioFrameInfo{}, false
		}
		i += off
		off = i + 1
		if i+4+18 > len(b) {
			return audioFrameInfo{}, false
		}
		br := bitReader{b: b[i+4:]}
		br.skip(4 + 4 + 2 + 2)
		ch6 := br.read(5)
		br.skip(2)
		ch8 := br.read(13)
		if br.read(16) != 0xB752 {
			continue
		}
		br.skip(16 + 16 + 16)
		br.skip(4 + 2 + 2) // 子流数 保留 扩展子流信息
		subInfo := br.read(8)
		if br.err {
			return audioFrameInfo{}, false
		}
		ch := 0
		if ch8 != 0 {
			for k := 0; k < 13; k++ {
				if ch8&(1<<k) != 0 {
					ch += thdChannelBits[k]
				}
			}
		} else {
			for k := 0; k < 5; k++ {
				if ch6&(1<<k) != 0 {
					ch += thdChannelBits[k]
				}
			}
		}
		return audioFrameInfo{channels: ch, atmos: subInfo&0x80 != 0}, ch > 0
	}
}

// ---------- DTS ----------

// dtsAmodeChannels 核心帧 AMODE → 主声道数
var dtsAmodeChannels = [16]int{1, 2, 2, 2, 2, 3, 3, 4, 4, 5, 6, 6, 6, 7, 8, 8}

// parseDTS 核心帧（7FFE8001）给基础声道；后面的扩展子流（64582025）说明是 DTS-HD，
// 资源描述符里的总声道数才是 7.1 这种真实声道（核心常常只有 5.1）。
// 扩展里有无损（XLL，41A29547）是 HD MA，XLL 里再带 DTS:X 的对象数据（02000850）是 DTS:X（MediaInfo 同样按这两个同步字认）
func parseDTS(b []byte) (audioFrameInfo, bool) {
	var fi audioFrameInfo
	found := false
	if i := bytes.Index(b, []byte{0x7F, 0xFE, 0x80, 0x01}); i >= 0 && i+15 <= len(b) {
		br := bitReader{b: b[i+4:]}
		br.skip(1 + 5 + 1 + 7 + 14) // FTYPE SHORT CPF NBLKS FSIZE
		amode := br.read(6)
		br.skip(4 + 5 + 1 + 1 + 1 + 1 + 1 + 3 + 1 + 1) // SFREQ RATE … ASPF
		lff := br.read(2)
		if !br.err && amode < 16 {
			fi.codec, fi.channels, found = "dts", dtsAmodeChannels[amode], true
			if lff == 1 || lff == 2 {
				fi.channels++
			}
		}
	}
	if i := bytes.Index(b, []byte{0x64, 0x58, 0x20, 0x25}); i >= 0 {
		found = true
		fi.codec = "dtshd"
		if n := dtsExSSChannels(b[i:]); n > 0 {
			fi.channels = n
		}
		rest := b[i:]
		if bytes.Contains(rest, []byte{0x41, 0xA2, 0x95, 0x47}) {
			fi.codec = "dtshd_ma"
		}
		if bytes.Contains(rest, []byte{0x02, 0x00, 0x08, 0x50}) {
			fi.codec = "dtsx"
		}
	}
	return fi, found
}

// dtsExSSChannels 扩展子流头 + 第一个资源描述符里的 nuTotalNumChs（ETSI TS 102 114 7.4 / 7.5）。读不到返回 0
func dtsExSSChannels(b []byte) int {
	br := bitReader{b: b[4:]}
	br.skip(8) // UserDefinedBits
	exssIndex := br.read(2)
	headerSizeType := br.read(1)
	hdrBits, fsizeBits := 8, 16
	if headerSizeType == 1 {
		hdrBits, fsizeBits = 12, 20
	}
	br.skip(hdrBits + fsizeBits)
	numAssets := 1
	if br.read(1) == 1 { // bStaticFieldsPresent
		br.skip(2 + 3) // nuRefClockCode nuExSSFrameDurationCode
		if br.read(1) == 1 {
			br.skip(32 + 4) // 时间戳
		}
		numPres := br.read(3) + 1
		numAssets = br.read(3) + 1
		activeSS := make([]int, numPres)
		for p := 0; p < numPres; p++ {
			activeSS[p] = br.read(exssIndex + 1)
		}
		for p := 0; p < numPres; p++ {
			for ss := 0; ss <= exssIndex; ss++ {
				if activeSS[p]&(1<<ss) != 0 {
					br.skip(8) // nuActiveAssetMask
				}
			}
		}
		if br.read(1) == 1 { // bMixMetadataEnbl
			br.skip(2) // nuMixMetadataAdjLevel
			bits := (br.read(2) + 1) * 4
			cfgs := br.read(2) + 1
			for k := 0; k < cfgs; k++ {
				br.skip(bits)
			}
		}
	} else {
		return 0 // 没有静态字段就没有总声道数
	}
	for k := 0; k < numAssets; k++ {
		br.skip(fsizeBits) // nuAssetFsize
	}
	// 第一个资源描述符
	br.skip(9 + 3) // nuAssetDescriptFsize nuAssetIndex
	if br.read(1) == 1 {
		br.skip(4) // nuAssetTypeDescriptor
	}
	if br.read(1) == 1 {
		br.skip(24) // LanguageDescriptor
	}
	if br.read(1) == 1 {
		n := br.read(10) + 1
		br.skip(n * 8)
	}
	br.skip(5 + 4) // nuBitResolution nuMaxSampleRate
	total := br.read(8) + 1
	if br.err || total > 32 {
		return 0
	}
	return total
}

// ---------- 蓝光 LPCM ----------

// bdLPCMChannels 蓝光 LPCM 每个 PES 开头 4 字节头：第 3 字节高 4 位是声道配置
func bdLPCMChannels(b []byte) int {
	if len(b) < 4 {
		return 0
	}
	return map[byte]int{1: 1, 3: 2, 4: 3, 5: 3, 6: 4, 7: 4, 8: 5, 9: 6, 10: 7, 11: 8}[b[2]>>4]
}
