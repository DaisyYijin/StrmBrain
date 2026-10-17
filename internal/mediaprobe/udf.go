package mediaprobe

import (
	"errors"
	"fmt"
	"strings"
	"unicode/utf16"
)

// ==================== UDF（光盘镜像的文件系统） ====================
//
// 只实现读目录、读小文件需要的那部分（ECMA-167 / OSTA UDF 2.50）：
// 锚点（第 256 扇区）→ 卷描述符序列（分区描述符、逻辑卷描述符）→ 分区映射 →
// 文件集描述符 → 根目录。蓝光是 UDF 2.50，目录和文件入口都在「元数据分区」里，
// 元数据分区本身是物理分区里的一个文件，块号要按它的分配描述符换算。
// 思路对照 libudfread（libbluray 用的那个），代码按规范自己写。

const udfSector = 2048

const (
	udfTagAVDP = 2
	udfTagPD   = 5
	udfTagLVD  = 6
	udfTagTD   = 8
	udfTagFSD  = 256
	udfTagFID  = 257
	udfTagFE   = 261
	udfTagEFE  = 266
)

type udfExtent struct {
	off int64 // 绝对字节偏移
	len int64
}

type udfPartition struct {
	start int64       // 物理分区起始扇区
	meta  []udfExtent // 元数据分区：元数据文件在盘上的各段（为空 = 普通物理分区）
}

type udfFS struct {
	r     *blockReader
	bs    int64
	parts []udfPartition // 按分区映射的下标（long_ad 里的 partition reference）
	root  udfLongAD
}

type udfLongAD struct {
	length int64
	lbn    int64
	part   int
}

func parseLongAD(b []byte) udfLongAD {
	return udfLongAD{length: le32(b) & 0x3FFFFFFF, lbn: le32(b[4:]), part: le16(b[8:])}
}

// udfOpen 读卷结构，找到根目录
func udfOpen(r *blockReader) (*udfFS, error) {
	avdp, err := r.readFull(256*udfSector, 32)
	if err != nil {
		return nil, err
	}
	if le16(avdp) != udfTagAVDP {
		return nil, errors.New("不是 UDF 镜像（第 256 扇区没有锚点）")
	}
	vdsLen, vdsLoc := le32(avdp[16:]), le32(avdp[20:])
	if vdsLen > 64*udfSector {
		vdsLen = 64 * udfSector
	}
	fs := &udfFS{r: r, bs: udfSector}
	pdStart := map[int]int64{} // 分区号 → 起始扇区
	var lvd []byte
	for s := int64(0); s*udfSector < vdsLen; s++ {
		d, err := r.readFull((vdsLoc+s)*udfSector, udfSector)
		if err != nil {
			return nil, err
		}
		switch le16(d) {
		case udfTagPD:
			pdStart[le16(d[22:])] = le32(d[188:])
		case udfTagLVD:
			lvd = d
		case udfTagTD:
			s = vdsLen // 结束
		}
	}
	if lvd == nil || len(pdStart) == 0 {
		return nil, errors.New("UDF 卷描述符不完整")
	}
	if bs := le32(lvd[212:]); bs > 0 && bs != udfSector {
		return nil, fmt.Errorf("UDF 逻辑块 %d 字节，不支持", bs)
	}
	fsdAD := parseLongAD(lvd[248:])
	nMaps := le32(lvd[268:])
	maps := lvd[440:]
	type metaMap struct {
		idx      int
		physPart int
		fileLoc  int64
	}
	var metas []metaMap
	for i := int64(0); i < nMaps && len(maps) >= 2; i++ {
		typ, l := maps[0], int(maps[1])
		if l < 6 || l > len(maps) {
			break
		}
		m := maps[:l]
		switch typ {
		case 1:
			fs.parts = append(fs.parts, udfPartition{start: pdStart[le16(m[4:])]})
		case 2:
			ident := string(m[5:28])
			num := le16(m[38:])
			fs.parts = append(fs.parts, udfPartition{start: pdStart[num]})
			if strings.HasPrefix(ident, "*UDF Metadata Partition") && l >= 44 {
				metas = append(metas, metaMap{idx: len(fs.parts) - 1, physPart: num, fileLoc: le32(m[40:])})
			}
		default:
			fs.parts = append(fs.parts, udfPartition{})
		}
		maps = maps[l:]
	}
	// 元数据文件：物理分区里的一个文件入口，它的分配描述符就是元数据分区的块映射
	for _, mm := range metas {
		start := fs.parts[mm.idx].start
		fe, err := r.readFull((start+mm.fileLoc)*udfSector, udfSector)
		if err != nil {
			return nil, err
		}
		ent, err := udfParseFE(fe)
		if err != nil {
			return nil, fmt.Errorf("UDF 元数据文件: %w", err)
		}
		var exts []udfExtent
		for _, ad := range ent.ads {
			exts = append(exts, udfExtent{off: (start + ad.lbn) * udfSector, len: ad.length})
		}
		if len(exts) == 0 {
			return nil, errors.New("UDF 元数据文件为空")
		}
		fs.parts[mm.idx].meta = exts
	}
	fsd, err := fs.readBlock(fsdAD.part, fsdAD.lbn)
	if err != nil {
		return nil, err
	}
	if le16(fsd) != udfTagFSD {
		return nil, errors.New("UDF 文件集描述符无效")
	}
	fs.root = parseLongAD(fsd[400:])
	return fs, nil
}

// blockOffset 分区内块号 → 绝对字节偏移
func (fs *udfFS) blockOffset(part int, lbn int64) (int64, error) {
	if part < 0 || part >= len(fs.parts) {
		return 0, fmt.Errorf("UDF 分区引用 %d 无效", part)
	}
	p := fs.parts[part]
	if p.meta == nil {
		return (p.start + lbn) * udfSector, nil
	}
	pos := lbn * fs.bs
	for _, e := range p.meta {
		if pos < e.len {
			return e.off + pos, nil
		}
		pos -= e.len
	}
	return 0, fmt.Errorf("UDF 元数据分区块 %d 越界", lbn)
}

func (fs *udfFS) readBlock(part int, lbn int64) ([]byte, error) {
	off, err := fs.blockOffset(part, lbn)
	if err != nil {
		return nil, err
	}
	return fs.r.readFull(off, udfSector)
}

// udfEntry 文件入口
type udfEntry struct {
	isDir    bool
	size     int64
	ads      []udfAD
	embedded []byte // 内容直接放在入口里（小目录、小文件）
}

type udfAD struct {
	length int64
	lbn    int64
	part   int // -1 = 短描述符，跟入口同一个分区
}

func udfParseFE(d []byte) (udfEntry, error) {
	var e udfEntry
	tag := le16(d)
	var lEA, lAD int64
	var adStart int64
	switch tag {
	case udfTagFE:
		lEA, lAD, adStart = le32(d[168:]), le32(d[172:]), 176
	case udfTagEFE:
		lEA, lAD, adStart = le32(d[208:]), le32(d[212:]), 216
	default:
		return e, fmt.Errorf("不是文件入口（标签 %d）", tag)
	}
	e.isDir = d[27] == 4
	e.size = le64(d[56:])
	adStart += lEA
	if adStart+lAD > int64(len(d)) || lAD < 0 {
		return e, errors.New("文件入口的分配描述符越界")
	}
	ads := d[adStart : adStart+lAD]
	switch le16(d[34:]) & 7 {
	case 0: // short_ad
		for len(ads) >= 8 {
			l := le32(ads)
			if l>>30 == 3 {
				return e, errors.New("文件入口的分配描述符有续段，不支持")
			}
			if l&0x3FFFFFFF > 0 && l>>30 == 0 {
				e.ads = append(e.ads, udfAD{length: l & 0x3FFFFFFF, lbn: le32(ads[4:]), part: -1})
			}
			ads = ads[8:]
		}
	case 1: // long_ad
		for len(ads) >= 16 {
			l := le32(ads)
			if l>>30 == 3 {
				return e, errors.New("文件入口的分配描述符有续段，不支持")
			}
			if l&0x3FFFFFFF > 0 && l>>30 == 0 {
				ad := parseLongAD(ads)
				e.ads = append(e.ads, udfAD{length: ad.length, lbn: ad.lbn, part: ad.part})
			}
			ads = ads[16:]
		}
	case 3: // 内容就在入口里
		e.embedded = append([]byte(nil), ads...)
	default:
		return e, errors.New("不支持的分配描述符类型")
	}
	return e, nil
}

func (fs *udfFS) entry(ad udfLongAD) (udfEntry, int, error) {
	d, err := fs.readBlock(ad.part, ad.lbn)
	if err != nil {
		return udfEntry{}, 0, err
	}
	e, err := udfParseFE(d)
	return e, ad.part, err
}

// readContent 读文件内容（最多 max 字节）。feParts 是入口所在的分区：短描述符指它
func (fs *udfFS) readContent(e udfEntry, fePart int, max int64) ([]byte, error) {
	if e.embedded != nil {
		return e.embedded, nil
	}
	want := e.size
	if want > max {
		want = max
	}
	out := make([]byte, 0, want)
	for _, ad := range e.ads {
		if int64(len(out)) >= want {
			break
		}
		n := ad.length
		if rest := want - int64(len(out)); n > rest {
			n = rest
		}
		off, err := fs.adOffset(ad, fePart)
		if err != nil {
			return nil, err
		}
		b, err := fs.r.readFull(off, n)
		if err != nil {
			return nil, err
		}
		out = append(out, b...)
	}
	return out, nil
}

// adOffset 分配描述符 → 绝对偏移。短描述符按规范指入口所在的分区；入口在元数据分区、
// 内容却在物理分区的写法（有的制作工具对文件内容也用短描述符）在元数据分区里越界，
// 这时退回同一张卡上的物理分区
func (fs *udfFS) adOffset(ad udfAD, fePart int) (int64, error) {
	part := ad.part
	if part < 0 {
		part = fePart
	}
	off, err := fs.blockOffset(part, ad.lbn)
	if err == nil || ad.part >= 0 {
		return off, err
	}
	if p := fs.parts[part]; p.meta != nil {
		return (p.start + ad.lbn) * udfSector, nil
	}
	return 0, err
}

type udfDirEntry struct {
	name  string
	isDir bool
	icb   udfLongAD
}

// list 列目录
func (fs *udfFS) list(dir udfLongAD) ([]udfDirEntry, error) {
	e, part, err := fs.entry(dir)
	if err != nil {
		return nil, err
	}
	if !e.isDir {
		return nil, errors.New("不是目录")
	}
	body, err := fs.readContent(e, part, 1<<20)
	if err != nil {
		return nil, err
	}
	var out []udfDirEntry
	for len(body) >= 38 {
		if le16(body) != udfTagFID {
			break
		}
		chars := body[18]
		lFI := int(body[19])
		lIU := le16(body[36:])
		total := (38 + lIU + lFI + 3) &^ 3
		if 38+lIU+lFI > len(body) {
			break
		}
		if chars&0x08 == 0 && lFI > 0 { // 跳过父目录项
			out = append(out, udfDirEntry{
				name:  udfName(body[38+lIU : 38+lIU+lFI]),
				isDir: chars&0x02 != 0,
				icb:   parseLongAD(body[20:]),
			})
		}
		if total > len(body) {
			break
		}
		body = body[total:]
	}
	return out, nil
}

// udfName 压缩过的 Unicode 名字：首字节 8 = 每字符一字节，16 = UTF-16 大端
func udfName(b []byte) string {
	if len(b) == 0 {
		return ""
	}
	switch b[0] {
	case 8:
		return string(b[1:])
	case 16:
		u := make([]uint16, 0, (len(b)-1)/2)
		for i := 1; i+1 < len(b); i += 2 {
			u = append(u, uint16(b[i])<<8|uint16(b[i+1]))
		}
		return string(utf16.Decode(u))
	}
	return string(b[1:])
}

// lookup 按名字（不分大小写）在目录里找一项
func (fs *udfFS) lookup(dir udfLongAD, name string) (udfDirEntry, bool, error) {
	ents, err := fs.list(dir)
	if err != nil {
		return udfDirEntry{}, false, err
	}
	for _, e := range ents {
		if strings.EqualFold(e.name, name) {
			return e, true, nil
		}
	}
	return udfDirEntry{}, false, nil
}

// readFile 读一个文件的内容
func (fs *udfFS) readFile(icb udfLongAD, max int64) ([]byte, error) {
	e, part, err := fs.entry(icb)
	if err != nil {
		return nil, err
	}
	return fs.readContent(e, part, max)
}

// fileExtent 文件第一段内容在镜像里的位置与长度（大文件读开头一段用）
func (fs *udfFS) fileExtent(icb udfLongAD) (int64, int64, error) {
	e, part, err := fs.entry(icb)
	if err != nil {
		return 0, 0, err
	}
	if e.embedded != nil || len(e.ads) == 0 {
		return 0, 0, errors.New("文件没有可读的内容段")
	}
	off, err := fs.adOffset(e.ads[0], part)
	if err != nil {
		return 0, 0, err
	}
	n := e.ads[0].length
	if n > e.size {
		n = e.size
	}
	return off, n, nil
}

// fileSize 文件大小（只读文件入口）
func (fs *udfFS) fileSize(icb udfLongAD) (int64, error) {
	e, _, err := fs.entry(icb)
	return e.size, err
}
