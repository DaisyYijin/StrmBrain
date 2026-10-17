package mediaprobe

import "errors"

// ==================== 视频参数集 ====================
//
// TS / M2TS 没有容器层的轨道描述，分辨率、位深、HDR 都在码流里：H.264 / HEVC 的 SPS（含 VUI），
// HEVC 的 SEI（HDR10+、母版显示信息）与杜比视界 RPU（NAL 62），MPEG-2 / VC-1 的序列头。
// 字段顺序对照 ITU-T H.264 7.3.2.1 / H.265 7.3.2.2、E.2.1。

// ue 无符号指数哥伦布
func (br *bitReader) ue() int {
	zeros := 0
	for br.read(1) == 0 {
		if br.err || zeros > 31 {
			br.err = true
			return 0
		}
		zeros++
	}
	if zeros == 0 {
		return 0
	}
	return (1 << uint(zeros)) - 1 + br.read(zeros)
}

// se 有符号指数哥伦布
func (br *bitReader) se() int {
	v := br.ue()
	if v&1 == 1 {
		return (v + 1) / 2
	}
	return -v / 2
}

// annexBNALs 按起始码切出 NAL 单元（去掉防竞争字节）
func annexBNALs(b []byte) [][]byte {
	var starts []int
	for i := 0; i+3 <= len(b); i++ {
		if b[i] == 0 && b[i+1] == 0 && b[i+2] == 1 {
			starts = append(starts, i+3)
			i += 2
		}
	}
	var out [][]byte
	for k, s := range starts {
		end := len(b)
		if k+1 < len(starts) {
			end = starts[k+1] - 3
			for end > s && b[end-1] == 0 { // 四字节起始码多出来的那个 0
				end--
			}
		}
		if end > s {
			out = append(out, unescapeRBSP(b[s:end]))
		}
	}
	return out
}

func unescapeRBSP(b []byte) []byte {
	out := make([]byte, 0, len(b))
	zeros := 0
	for _, c := range b {
		if zeros >= 2 && c == 3 {
			zeros = 0
			continue
		}
		if c == 0 {
			zeros++
		} else {
			zeros = 0
		}
		out = append(out, c)
	}
	return out
}

// vuiColour VUI 里的 video_signal_type 部分（两种编码同一套写法）
func vuiColour(br *bitReader, v *Video) {
	if br.read(1) == 1 { // aspect_ratio_info_present
		if br.read(8) == 255 {
			br.skip(32)
		}
	}
	if br.read(1) == 1 { // overscan_info_present
		br.skip(1)
	}
	if br.read(1) == 1 { // video_signal_type_present
		br.skip(3 + 1)
		if br.read(1) == 1 { // colour_description_present
			br.skip(8)
			if t := transferName(br.read(8)); t != "" && !br.err {
				v.Transfer = t
			}
			br.skip(8)
		}
	}
	if br.read(1) == 1 { // chroma_loc_info_present
		br.ue()
		br.ue()
	}
}

var errBadSPS = errors.New("SPS 解析失败")

// parseH264SPS nal 含 1 字节 NAL 头
func parseH264SPS(nal []byte) (Video, error) {
	v := Video{Codec: "h264", BitDepth: 8}
	if len(nal) < 4 {
		return v, errBadSPS
	}
	br := bitReader{b: nal[1:]}
	profile := br.read(8)
	br.skip(16) // constraint_set + level_idc
	br.ue()     // seq_parameter_set_id
	chroma := 1
	switch profile {
	case 100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135:
		chroma = br.ue()
		if chroma == 3 {
			br.skip(1)
		}
		v.BitDepth = br.ue() + 8
		br.ue()
		br.skip(1)
		if br.read(1) == 1 { // seq_scaling_matrix_present
			n := 8
			if chroma == 3 {
				n = 12
			}
			for i := 0; i < n; i++ {
				if br.read(1) == 1 {
					size := 16
					if i >= 6 {
						size = 64
					}
					last, next := 8, 8
					for j := 0; j < size; j++ {
						if next != 0 {
							next = (last + br.se() + 256) % 256
						}
						if next != 0 {
							last = next
						}
					}
				}
			}
		}
	}
	br.ue()          // log2_max_frame_num_minus4
	switch br.ue() { // pic_order_cnt_type
	case 0:
		br.ue()
	case 1:
		br.skip(1)
		br.se()
		br.se()
		n := br.ue()
		if n > 255 {
			return v, errBadSPS
		}
		for i := 0; i < n; i++ {
			br.se()
		}
	}
	br.ue()    // max_num_ref_frames
	br.skip(1) // gaps
	wMbs := br.ue() + 1
	hMaps := br.ue() + 1
	frameMbsOnly := br.read(1)
	if frameMbsOnly == 0 {
		br.skip(1)
	}
	br.skip(1) // direct_8x8_inference
	cropX, cropY := 2, 2*(2-frameMbsOnly)
	switch chroma {
	case 0:
		cropX, cropY = 1, 2-frameMbsOnly
	case 2:
		cropY = 2 - frameMbsOnly
	case 3:
		cropX, cropY = 1, 2-frameMbsOnly
	}
	var cl, cr, ct, cb int
	if br.read(1) == 1 {
		cl, cr, ct, cb = br.ue(), br.ue(), br.ue(), br.ue()
	}
	v.Width = wMbs*16 - (cl+cr)*cropX
	v.Height = (2-frameMbsOnly)*hMaps*16 - (ct+cb)*cropY
	if br.read(1) == 1 { // vui_parameters_present
		vuiColour(&br, &v)
		if br.read(1) == 1 { // timing_info_present
			units, scale := br.read(32), br.read(32)
			if units > 0 && !br.err {
				v.FPS = roundFPS(float64(scale) / float64(2*units))
			}
		}
	}
	if br.err && v.Width <= 0 {
		return v, errBadSPS
	}
	return v, nil
}

// parseHEVCSPS nal 含 2 字节 NAL 头
func parseHEVCSPS(nal []byte) (Video, error) {
	v := Video{Codec: "hevc"}
	if len(nal) < 6 {
		return v, errBadSPS
	}
	br := bitReader{b: nal[2:]}
	br.skip(4) // sps_video_parameter_set_id
	maxSub := br.read(3)
	br.skip(1)
	// profile_tier_level
	br.skip(2 + 1 + 5 + 32 + 48 + 8)
	profPresent := make([]int, maxSub)
	levelPresent := make([]int, maxSub)
	for i := 0; i < maxSub; i++ {
		profPresent[i], levelPresent[i] = br.read(1), br.read(1)
	}
	if maxSub > 0 {
		for i := maxSub; i < 8; i++ {
			br.skip(2)
		}
	}
	for i := 0; i < maxSub; i++ {
		if profPresent[i] == 1 {
			br.skip(88)
		}
		if levelPresent[i] == 1 {
			br.skip(8)
		}
	}
	br.ue() // sps_seq_parameter_set_id
	chroma := br.ue()
	if chroma == 3 {
		br.skip(1)
	}
	w, h := br.ue(), br.ue()
	subW, subH := 2, 2
	switch chroma {
	case 0, 3:
		subW, subH = 1, 1
	case 2:
		subH = 1
	}
	if br.read(1) == 1 { // conformance_window
		l, r, t, b := br.ue(), br.ue(), br.ue(), br.ue()
		w -= (l + r) * subW
		h -= (t + b) * subH
	}
	v.Width, v.Height = w, h
	v.BitDepth = br.ue() + 8
	br.ue() // bit_depth_chroma
	pocBits := br.ue() + 4
	start := maxSub
	if br.read(1) == 1 { // sub_layer_ordering_info_present
		start = 0
	}
	for i := start; i <= maxSub; i++ {
		br.ue()
		br.ue()
		br.ue()
	}
	br.ue() // log2_min_luma_coding_block_size_minus3
	br.ue()
	br.ue()
	br.ue()
	br.ue() // max_transform_hierarchy_depth_inter
	br.ue()
	if br.read(1) == 1 && br.read(1) == 1 { // scaling_list_enabled → sps_scaling_list_data_present
		for size := 0; size < 4; size++ {
			step := 1
			if size == 3 {
				step = 3
			}
			for m := 0; m < 6; m += step {
				if br.read(1) == 0 {
					br.ue()
					continue
				}
				n := 64
				if c := 1 << uint(4+(size<<1)); c < n {
					n = c
				}
				if size > 1 {
					br.se()
				}
				for k := 0; k < n; k++ {
					br.se()
				}
			}
		}
	}
	br.skip(1 + 1)       // amp sao
	if br.read(1) == 1 { // pcm
		br.skip(4 + 4)
		br.ue()
		br.ue()
		br.skip(1)
	}
	numSets := br.ue()
	if numSets > 64 {
		return v, nil // 分辨率与位深已经有了，VUI 读不到就算
	}
	numDelta := make([]int, numSets)
	for i := 0; i < numSets; i++ {
		if i != 0 && br.read(1) == 1 { // inter_ref_pic_set_prediction
			br.skip(1)
			br.ue()
			n := 0
			for j := 0; j <= numDelta[i-1]; j++ {
				used := br.read(1)
				use := 1
				if used == 0 {
					use = br.read(1)
				}
				if used == 1 || use == 1 {
					n++
				}
			}
			numDelta[i] = n
			continue
		}
		neg, pos := br.ue(), br.ue()
		if neg+pos > 32 {
			return v, nil
		}
		for k := 0; k < neg+pos; k++ {
			br.ue()
			br.skip(1)
		}
		numDelta[i] = neg + pos
	}
	if br.read(1) == 1 { // long_term_ref_pics_present
		n := br.ue()
		if n > 32 {
			return v, nil
		}
		for k := 0; k < n; k++ {
			br.skip(pocBits + 1)
		}
	}
	br.skip(1 + 1) // temporal_mvp strong_intra_smoothing
	if br.err {
		return v, nil
	}
	if br.read(1) == 1 { // vui_parameters_present
		vv := v
		vuiColour(&br, &vv)
		br.skip(1 + 1 + 1) // neutral_chroma field_seq frame_field_info
		if br.read(1) == 1 {
			br.ue()
			br.ue()
			br.ue()
			br.ue()
		}
		if br.read(1) == 1 { // vui_timing_info_present
			units, scale := br.read(32), br.read(32)
			if units > 0 {
				vv.FPS = roundFPS(float64(scale) / float64(units))
			}
		}
		if !br.err {
			v = vv
		} else {
			v.Transfer = vv.Transfer
		}
	}
	return v, nil
}

// hevcSEI 前缀 SEI 里认 HDR10+（T.35：B5 003C 0001 04）与母版显示信息（137）
func hevcSEI(nal []byte) (hdr10plus, mastering bool) {
	b := nal[2:]
	for len(b) > 2 {
		typ := 0
		for len(b) > 0 && b[0] == 0xFF {
			typ += 255
			b = b[1:]
		}
		if len(b) == 0 {
			return
		}
		typ += int(b[0])
		b = b[1:]
		size := 0
		for len(b) > 0 && b[0] == 0xFF {
			size += 255
			b = b[1:]
		}
		if len(b) == 0 {
			return
		}
		size += int(b[0])
		b = b[1:]
		if size > len(b) {
			return
		}
		p := b[:size]
		switch typ {
		case 4:
			if len(p) >= 6 && p[0] == 0xB5 && p[1] == 0x00 && p[2] == 0x3C && p[3] == 0x00 && p[4] == 0x01 && p[5] == 0x04 {
				hdr10plus = true
			}
		case 137:
			mastering = true
		}
		b = b[size:]
		if len(b) == 1 && b[0] == 0x80 { // rbsp_trailing_bits
			return
		}
	}
	return
}

// videoFromES 从一段视频基本流里读：SPS 给尺寸位深与 VUI，SEI / RPU 补 HDR10+ 与杜比视界
func videoFromES(codec string, es []byte) (Video, bool) {
	switch codec {
	case "mpeg2":
		return mpeg2SeqHeader(es)
	case "vc1":
		return vc1SeqHeader(es)
	}
	var v Video
	got, plus, mastering, rpu := false, false, false, false
	for _, nal := range annexBNALs(es) {
		if len(nal) < 2 {
			continue
		}
		switch codec {
		case "h264":
			if nal[0]&0x1F == 7 && !got {
				if sv, err := parseH264SPS(nal); err == nil {
					v, got = sv, true
				}
			}
		case "hevc":
			switch (nal[0] >> 1) & 0x3F {
			case 33:
				if !got {
					if sv, err := parseHEVCSPS(nal); err == nil {
						v, got = sv, true
					}
				}
			case 39:
				p, m := hevcSEI(nal)
				plus, mastering = plus || p, mastering || m
			case 62:
				rpu = true
			}
		}
	}
	if !got {
		return v, false
	}
	v.HDR10Plus = v.HDR10Plus || plus
	if v.Transfer == "" && mastering {
		v.Transfer = "pq"
	}
	if rpu {
		v.DV = true
	}
	return v, true
}

// mpeg2SeqHeader 序列头 000001B3：宽 12 位、高 12 位、宽高比 4 位、帧率码 4 位
func mpeg2SeqHeader(es []byte) (Video, bool) {
	for i := 0; i+8 <= len(es); i++ {
		if es[i] == 0 && es[i+1] == 0 && es[i+2] == 1 && es[i+3] == 0xB3 {
			b := es[i+4:]
			v := Video{Codec: "mpeg2", BitDepth: 8}
			v.Width = int(b[0])<<4 | int(b[1]>>4)
			v.Height = int(b[1]&0x0F)<<8 | int(b[2])
			v.FPS = map[byte]float64{1: 23.976, 2: 24, 3: 25, 4: 29.97, 5: 30, 6: 50, 7: 59.94, 8: 60}[b[3]&0x0F]
			return v, v.Width > 0
		}
	}
	return Video{}, false
}

// vc1SeqHeader 高级档次序列头 0000010F：profile 2、level 3、色度 2、帧率 / 码率量化 3+5、后处理 1，之后是最大编码宽高（×2+2）
func vc1SeqHeader(es []byte) (Video, bool) {
	for i := 0; i+10 <= len(es); i++ {
		if es[i] == 0 && es[i+1] == 0 && es[i+2] == 1 && es[i+3] == 0x0F {
			br := bitReader{b: es[i+4:]}
			if br.read(2) != 3 { // 只有高级档次带序列头
				return Video{Codec: "vc1"}, true
			}
			br.skip(3 + 2 + 3 + 5 + 1)
			v := Video{Codec: "vc1", BitDepth: 8}
			v.Width = br.read(12)*2 + 2
			v.Height = br.read(12)*2 + 2
			return v, !br.err
		}
	}
	return Video{}, false
}
