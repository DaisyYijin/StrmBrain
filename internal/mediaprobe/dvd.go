package mediaprobe

import (
	"errors"
	"regexp"
	"strings"
)

// ==================== DVD 镜像 ====================
//
// VIDEO_TS 下每个标题集一组 VTS_nn_0.IFO + VTS_nn_1..9.VOB。正片是 VOB 加起来最大的那个标题集，
// 它的 IFO 第 0x200 字节起是视频属性、音轨数与每条音轨的属性（字段排法对照 libdvdread 的 ifo_types.h）。

var reVOB = regexp.MustCompile(`(?i)^VTS_(\d\d)_[1-9]\.VOB$`)

func probeDVD(fs *udfFS, videoTS udfLongAD) (*Info, error) {
	ents, err := fs.list(videoTS)
	if err != nil {
		return nil, err
	}
	sizes := map[string]int64{}
	ifo := map[string]udfLongAD{}
	for _, e := range ents {
		if m := reVOB.FindStringSubmatch(e.name); m != nil {
			n, err := fs.fileSize(e.icb)
			if err != nil {
				return nil, err
			}
			sizes[m[1]] += n
		} else if strings.HasPrefix(strings.ToUpper(e.name), "VTS_") && strings.HasSuffix(strings.ToUpper(e.name), "_0.IFO") {
			ifo[strings.ToUpper(e.name)[4:6]] = e.icb
		}
	}
	best, bestSize := "", int64(-1)
	for nn, n := range sizes {
		if n > bestSize || (n == bestSize && nn < best) {
			best, bestSize = nn, n
		}
	}
	icb, ok := ifo[best]
	if best == "" || !ok {
		return nil, errors.New("DVD 里没有可用的标题集")
	}
	d, err := fs.readFile(icb, 1024)
	if err != nil {
		return nil, err
	}
	info := &Info{Container: "dvd", Disc: "dvd"}
	applyVTSIFO(info, d)
	return info, nil
}

// applyVTSIFO VTS IFO：0x200 视频属性（2 字节）、0x202 音轨数、0x204 起每条音轨 8 字节
func applyVTSIFO(info *Info, d []byte) {
	if len(d) < 0x204 || string(d[:12]) != "DVDVIDEO-VTS" {
		return
	}
	va := d[0x200:]
	v := &Video{BitDepth: 8}
	if va[0]>>6 == 1 {
		v.Codec = "mpeg2"
	} else {
		v.Codec = "mpeg1" // 词表里没有，不写编码
	}
	pal := (va[0]>>4)&3 == 1
	h, fps := 480, 29.97
	if pal {
		h, fps = 576, 25
	}
	switch (va[1] >> 2) & 3 { // picture_size
	case 0:
		v.Width, v.Height = 720, h
	case 1:
		v.Width, v.Height = 704, h
	case 2:
		v.Width, v.Height = 352, h
	case 3:
		v.Width, v.Height = 352, h/2
	}
	v.FPS = fps
	v.Transfer = "sdr"
	info.Video = v
	n := be16(d[0x202:])
	if n > 8 {
		n = 8
	}
	for i := 0; i < n; i++ {
		o := 0x204 + i*8
		if o+8 > len(d) {
			break
		}
		a := d[o : o+8]
		codec := map[byte]string{0: "ac3", 2: "mp3", 3: "mp3", 4: "lpcm", 6: "dts"}[a[0]>>5]
		if codec == "" {
			continue
		}
		au := Audio{Codec: codec, Channels: int(a[1]&7) + 1, Default: i == 0}
		if (a[0]>>2)&3 == 1 {
			au.Lang = langCode(a[2:4])
		}
		info.Audios = append(info.Audios, au)
	}
}
