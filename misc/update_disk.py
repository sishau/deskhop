#!/usr/bin/env python3
"""Replace CONFIG.HTM inside deskhop's 64KB FAT12 disk image, in place.

Avoids mtools and loop mounts entirely: parses the FAT12 structures already
present in disk.img (created by disk/create.sh as a 2MB floppy whose first
64KB is kept), re-lays out the file's cluster chain starting at the same
first cluster, extending into free clusters that are fully contained in the
truncated image, updates the directory-entry size, and verifies by reading
the chain back. A .bak copy of the original image is left behind.

Usage: update_disk.py <disk.img> <config.htm>
"""
import shutil
import struct
import sys

FAT12_EOC_MIN = 0xFF8
NAME83 = b'CONFIG  HTM'


def fat12_get(img, fat_off, n):
    off = fat_off + n * 3 // 2
    v = struct.unpack_from('<H', img, off)[0]
    return (v >> 4) if (n & 1) else (v & 0x0FFF)


def fat12_set(img, fat_off, n, val):
    off = fat_off + n * 3 // 2
    v = struct.unpack_from('<H', img, off)[0]
    if n & 1:
        v = (v & 0x000F) | (val << 4)
    else:
        v = (v & 0xF000) | (val & 0x0FFF)
    struct.pack_into('<H', img, off, v)


class Fat12:
    def __init__(self, img):
        self.img = img
        self.bps = struct.unpack_from('<H', img, 11)[0]        # bytes/sector
        self.spc = img[13]                                     # sectors/cluster
        self.reserved = struct.unpack_from('<H', img, 14)[0]
        self.nfats = img[16]
        self.root_entries = struct.unpack_from('<H', img, 17)[0]
        self.spf = struct.unpack_from('<H', img, 22)[0]        # sectors/FAT
        self.fat_off = self.reserved * self.bps
        self.fat_len = self.nfats * self.spf * self.bps
        self.root_off = self.fat_off + self.fat_len
        self.root_len = self.root_entries * 32
        self.data_off = self.root_off + self.root_len
        self.cluster_bytes = self.spc * self.bps
        # clusters fully contained in the (truncated) image
        self.max_clusters = (len(img) - self.data_off) // self.cluster_bytes

    def cluster_off(self, n):
        return self.data_off + (n - 2) * self.cluster_bytes

    def in_image(self, n):
        return self.cluster_off(n) + self.cluster_bytes <= len(self.img)

    def find_entry(self):
        for i in range(self.root_entries):
            off = self.root_off + i * 32
            if self.img[off] == 0x00:          # end of directory
                break
            if self.img[off + 11] & 0x08:      # volume label
                continue
            if bytes(self.img[off:off + 11]) == NAME83:
                first = struct.unpack_from('<H', self.img, off + 26)[0]
                size = struct.unpack_from('<I', self.img, off + 28)[0]
                return off, first, size
        raise SystemExit(f'{NAME83!r} not found in root directory')

    def chain(self, first):
        out, c = [], first
        while True:
            if not self.in_image(c):
                raise SystemExit(f'cluster {c} lies outside the truncated image')
            out.append(c)
            c = fat12_get(self.img, self.fat_off, c)
            if c >= FAT12_EOC_MIN:
                break
            if len(out) > self.max_clusters:
                raise SystemExit('FAT chain loop detected')
        return out

    def read_file(self, chain, size):
        return b''.join(
            bytes(self.img[self.cluster_off(c):self.cluster_off(c) + self.cluster_bytes])
            for c in chain)[:size]


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    img_path, htm_path = sys.argv[1], sys.argv[2]

    with open(img_path, 'rb') as f:
        fs = Fat12(bytearray(f.read()))
    print(f'image {len(fs.img)} B | cluster {fs.cluster_bytes} B | '
          f'fat@0x{fs.fat_off:04X} root@0x{fs.root_off:04X} data@0x{fs.data_off:04X} '
          f'| clusters in image: {fs.max_clusters}')

    entry_off, first, old_size = fs.find_entry()
    old_chain = fs.chain(first)
    old_data = fs.read_file(old_chain, old_size)
    print(f'CONFIG.HTM: first_cluster={first} size={old_size} '
          f'chain={len(old_chain)} clusters {old_chain[:6]}{"..." if len(old_chain) > 6 else ""}')

    data = open(htm_path, 'rb').read()
    need = (len(data) + fs.cluster_bytes - 1) // fs.cluster_bytes
    print(f'new config.htm: {len(data)} B -> {need} clusters')
    if need > fs.max_clusters:
        raise SystemExit(f'new file needs {need} clusters but only {fs.max_clusters} fit in image')

    used = set(old_chain)
    free = [c for c in range(2, 2 + fs.max_clusters)
            if c not in used and fat12_get(fs.img, fs.fat_off, c) == 0 and fs.in_image(c)]
    print(f'free clusters inside image: {len(free)}')
    if need > len(old_chain) + len(free):
        raise SystemExit(f'not enough clusters: need {need}, max {len(old_chain) + len(free)}')

    # keep old chain head, extend with free clusters, then rebuild all links
    chain = old_chain[:need] + free[:max(0, need - len(old_chain))]
    assert len(chain) == need == len(set(chain))

    for c in old_chain:
        fat12_set(fs.img, fs.fat_off, c, 0)
    for i, c in enumerate(chain):
        fat12_set(fs.img, fs.fat_off, c, chain[i + 1] if i + 1 < len(chain) else FAT12_EOC_MIN)
    # mirror FAT copy #2
    fat1 = fs.img[fs.fat_off:fs.fat_off + fs.spf * fs.bps]
    fs.img[fs.fat_off + fs.spf * fs.bps:fs.fat_off + fs.spf * fs.bps * 2] = fat1

    for i, c in enumerate(chain):
        chunk = data[i * fs.cluster_bytes:(i + 1) * fs.cluster_bytes]
        off = fs.cluster_off(c)
        fs.img[off:off + len(chunk)] = chunk
    struct.pack_into('<I', fs.img, entry_off + 28, len(data))

    # verify: independently re-parse the updated image and read the file back
    check = Fat12(bytes(fs.img))
    _, first2, size2 = check.find_entry()
    chain2 = check.chain(first2)
    back = check.read_file(chain2, size2)
    if back != data or size2 != len(data) or chain2 != chain:
        raise SystemExit('VERIFY FAILED: read-back mismatch')
    print(f'verify OK: {len(chain2)} clusters {chain2[:6]}{"..." if len(chain2) > 6 else ""} '
          f'size={size2}, content read-back matches')

    shutil.copy2(img_path, img_path + '.bak')
    with open(img_path, 'wb') as f:
        f.write(fs.img)
    print(f'written {img_path} (backup: {img_path}.bak)')


if __name__ == '__main__':
    main()
