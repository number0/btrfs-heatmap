#!/usr/bin/python3
#
# Copyright (C) 2016-2017 Hans van Kranenburg <hans@knorrie.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public
# License v2 as published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public
# License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor,
# Boston, MA 02110-1301 USA

import argparse
import btrfs
import os
import struct
import sys
import types
import zlib


class HeatmapError(Exception):
    pass


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--order",
        type=int,
        help="Hilbert curve order (default: automatically chosen)",
    )
    parser.add_argument(
        "--size",
        type=int,
        help="Image size (default: 10). Height/width is 2^size",
    )
    parser.add_argument(
        "--sort",
        choices=['physical', 'virtual'],
        default='physical',
        help="Show disk usage sorted on dev_extent (physical) or chunk/stripe (virtual)"
    )
    parser.add_argument(
        "--blockgroup",
        type=int,
        help="Instead of a filesystem overview, show extents in a block group",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        help="increase debug output verbosity (-v, -vv, -vvv, etc)",
    )
    parser.add_argument(
        "-o",
        "--output",
        dest="output",
        help="Output png file name or directory (default: filename automatically chosen)",
    )
    parser.add_argument(
        "--curve",
        choices=['hilbert', 'linear', 'snake'],
        default='hilbert',
        help="Space filling curve type or alternative. Default is hilbert.",
    )
    parser.add_argument(
        "mountpoint",
        help="Btrfs filesystem mountpoint",
    )
    return parser.parse_args()


struct_color = struct.Struct('!BBB')

black = (0x00, 0x00, 0x00)
white = (0xff, 0xff, 0xff)

p_red = (0xca, 0x53, 0x5c)
fuchsia = (0xde, 0x5d, 0x94)
curry = (0xf9, 0xe1, 0x7e)
clover = (0x6e, 0xa6, 0x34)
moss = (0x81, 0x88, 0x3c)
bluebell = (0xaa, 0xcc, 0xeb)
pool = (0x8f, 0xdd, 0xea)
beet = (0x9d, 0x54, 0x9c)
aubergine = (0x6a, 0x5a, 0x7f)
plum = (0xdb, 0xc9, 0xea)
slate = (0x75, 0x77, 0x7b)
chocolate = (0x6f, 0x5e, 0x55)

red = (0xff, 0x00, 0x33)
blue = (0x00, 0x00, 0xff)
blue_white = (0x99, 0xcc, 0xff)  # for mixed bg

dev_extent_colors = {
    btrfs.BLOCK_GROUP_DATA: white,
    btrfs.BLOCK_GROUP_METADATA: blue,
    btrfs.BLOCK_GROUP_SYSTEM: red,
    btrfs.BLOCK_GROUP_DATA | btrfs.BLOCK_GROUP_METADATA: blue_white,
}

metadata_extent_colors = {
    btrfs.ctree.ROOT_TREE_OBJECTID: p_red,
    btrfs.ctree.EXTENT_TREE_OBJECTID: beet,
    btrfs.ctree.CHUNK_TREE_OBJECTID: moss,
    btrfs.ctree.DEV_TREE_OBJECTID: aubergine,
    btrfs.ctree.FS_TREE_OBJECTID: bluebell,
    btrfs.ctree.CSUM_TREE_OBJECTID: clover,
    btrfs.ctree.QUOTA_TREE_OBJECTID: fuchsia,
    btrfs.ctree.UUID_TREE_OBJECTID: chocolate,
    btrfs.ctree.FREE_SPACE_TREE_OBJECTID: plum,
    btrfs.ctree.DATA_RELOC_TREE_OBJECTID: slate,
}


def hilbert(order):
    U = (-1, 0)
    R = (0, 1)
    D = (1, 0)
    L = (0, -1)

    URDR = (U, R, D, R)
    RULU = (R, U, L, U)
    URDD = (U, R, D, D)
    LDRR = (L, D, R, R)
    RULL = (R, U, L, L)
    DLUU = (D, L, U, U)
    LDRD = (L, D, R, D)
    DLUL = (D, L, U, L)

    inception = {
        URDR: (RULU, URDR, URDD, LDRR),
        RULU: (URDR, RULU, RULL, DLUU),
        URDD: (RULU, URDR, URDD, LDRD),
        LDRR: (DLUL, LDRD, LDRR, URDR),
        RULL: (URDR, RULU, RULL, DLUL),
        DLUU: (LDRD, DLUL, DLUU, RULU),
        LDRD: (DLUL, LDRD, LDRR, URDD),
        DLUL: (LDRD, DLUL, DLUU, RULL)
    }

    pos = [(2 ** order) - 1, 0, 0]  # y, x, linear

    def walk(steps, level):
        if level > 1:
            for substeps in inception[steps]:
                for subpos in walk(substeps, level - 1):
                    yield subpos
        else:
            for step in steps:
                yield pos
                pos[0] += step[0]  # y
                pos[1] += step[1]  # x
                pos[2] += 1  # linear

    return walk(URDR, order)


def linear(order):
    edge_len = 2 ** order
    l = 0
    for y in range(0, edge_len):
        for x in range(0, edge_len):
            yield (y, x, l)
            l += 1


def snake(order):
    edge_len = 2 ** order
    l = 0
    for y in range(0, edge_len, 2):
        for x in range(0, edge_len):
            yield (y, x, l)
            l += 1
        y += 1
        for x in range(edge_len - 1, -1, -1):
            yield (y, x, l)
            l += 1


curves = {
    'hilbert': hilbert,
    'linear': linear,
    'snake': snake,
}


class Grid(object):
    def __init__(self, order, size, total_bytes, default_granularity, verbose,
                 min_brightness=None, curve=None):
        self.order, self.size = choose_order_size(order, size, total_bytes, default_granularity)
        self.verbose = verbose
        if curve is None:
            curve = 'hilbert'
        self.curve = curves.get(curve)(self.order)
        self._pixel_mix = []
        self._pixel_dirty = False
        self._next_pixel()
        self.height = 2 ** self.order
        self.width = 2 ** self.order
        self.num_steps = (2 ** self.order) ** 2
        self.total_bytes = total_bytes
        self.bytes_per_pixel = total_bytes / self.num_steps
        self._color_cache = {}
        self._add_color_cache(black)
        self._grid = [[self._color_cache[black]
                       for x in range(self.width)]
                      for y in range(self.height)]
        self._finished = False
        if min_brightness is None:
            self._min_brightness = 0.1
        else:
            if min_brightness < 0 or min_brightness > 1:
                raise ValueError("min_brightness out of range (need >= 0 and <= 1)")
            self._min_brightness = min_brightness
        print("grid curve {} order {} size {} height {} width {} total_bytes {} "
              "bytes_per_pixel {}".format(curve, self.order, self.size,
                                          self.height, self.width, total_bytes,
                                          self.bytes_per_pixel, self.num_steps))

    def _next_pixel(self):
        if self._pixel_dirty is True:
            self._finish_pixel()
        self.y, self.x, self.linear = next(self.curve)

    def _add_to_pixel_mix(self, color, used_pct, pixel_pct):
        self._pixel_mix.append((color, used_pct, pixel_pct))
        self._pixel_dirty = True

    def _pixel_mix_to_rgbytes(self):
        R_composite = sum(color[0] * pixel_pct for color, _, pixel_pct in self._pixel_mix)
        G_composite = sum(color[1] * pixel_pct for color, _, pixel_pct in self._pixel_mix)
        B_composite = sum(color[2] * pixel_pct for color, _, pixel_pct in self._pixel_mix)

        weighted_usage = sum(used_pct * pixel_pct
                             for _, used_pct, pixel_pct in self._pixel_mix)
        weighted_usage_min_bright = self._min_brightness + \
            weighted_usage * (1 - self._min_brightness)

        RGB = (
            int(round(R_composite * weighted_usage_min_bright)),
            int(round(G_composite * weighted_usage_min_bright)),
            int(round(B_composite * weighted_usage_min_bright)),
        )

        if RGB in self._color_cache:
            return self._color_cache[RGB]
        return self._add_color_cache(RGB)

    def _add_color_cache(self, color):
        rgbytes = struct_color.pack(*color)
        self._color_cache[color] = rgbytes
        return rgbytes

    def _set_pixel(self, rgbytes):
        self._grid[self.y][self.x] = rgbytes

    def _finish_pixel(self):
        rgbytes = self._pixel_mix_to_rgbytes()
        self._set_pixel(rgbytes)
        if self.verbose >= 3:
            print("        pixel y {} x{} linear {} rgb #{:02x}{:02x}{:02x}".format(
                self.y, self.x, self.linear, *[byte for byte in rgbytes]))
        self._pixel_mix = []
        self._pixel_dirty = False

    def fill(self, first_byte, length, used_pct, color=white):
        if self._finished is True:
            raise Exception("Cannot change grid any more after retrieving the result once!")
        first_pixel = int(first_byte / self.bytes_per_pixel)
        last_pixel = int((first_byte + length - 1) / self.bytes_per_pixel)

        while self.linear < first_pixel:
            self._next_pixel()

        if first_pixel == last_pixel:
            pct_of_pixel = length / self.bytes_per_pixel
            if self.verbose >= 2:
                print("    in_pixel {0} {1:.2f}%".format(first_pixel, pct_of_pixel * 100))
            self._add_to_pixel_mix(color, used_pct, pct_of_pixel)
        else:
            pct_of_first_pixel = \
                (self.bytes_per_pixel - (first_byte % self.bytes_per_pixel)) / self.bytes_per_pixel
            pct_of_last_pixel = \
                ((first_byte + length) % self.bytes_per_pixel) / self.bytes_per_pixel
            if pct_of_last_pixel == 0:
                pct_of_last_pixel = 1
            if self.verbose >= 2:
                print("    first_pixel {0} {1:.2f}% last_pixel {2} {3:.2f}%".format(
                    first_pixel, pct_of_first_pixel * 100, last_pixel, pct_of_last_pixel * 100))
            # add our part of the first pixel, may be shared with previous fill
            self._add_to_pixel_mix(color, used_pct, pct_of_first_pixel)
            # all intermediate pixels are ours, set brightness directly
            if self.linear < last_pixel - 1:
                self._next_pixel()
                self._add_to_pixel_mix(color, used_pct, pixel_pct=1)
                rgbytes = self._pixel_mix_to_rgbytes()
                self._set_pixel(rgbytes)
                if self.verbose >= 3:
                    print("        pixel range linear {} to {} rgb #{:02x}{:02x}{:02x}".format(
                        self.linear, last_pixel - 1, *[byte for byte in rgbytes]))
                while self.linear < last_pixel - 1:
                    self._next_pixel()
                    self._set_pixel(rgbytes)
            self._next_pixel()
            # add our part of the last pixel, may be shared with next fill
            self._add_to_pixel_mix(color, used_pct, pct_of_last_pixel)

    def write_png(self, pngfile):
        print("pngfile {}".format(pngfile))
        if self._finished is False:
            if self._pixel_dirty is True:
                self._finish_pixel()
            self._finished = True
        if self.size > self.order:
            scale = 2 ** (self.size - self.order)
            rows = ((pix for pix in row for _ in range(scale))
                    for row in self._grid for _ in range(scale))
            _write_png(pngfile, self.width * scale, self.height * scale, rows)
        else:
            _write_png(pngfile, self.width, self.height, self._grid)


def walk_chunks(fs, devices=None, order=None, size=None,
                default_granularity=33554432, verbose=0, min_brightness=None, curve=None):
    if devices is None:
        devices = list(fs.devices())
        devids = None
        print("scope chunks")
    else:
        if isinstance(devices, types.GeneratorType):
            devices = list(devices)
        devids = [device.devid for device in devices]
        print("scope chunk stripes on devices {}".format(' '.join(map(str, devids))))

    total_bytes = sum(device.total_bytes for device in devices)

    grid = Grid(order, size, total_bytes, default_granularity, verbose, min_brightness, curve)
    byte_offset = 0
    for chunk in fs.chunks():
        if devids is None:
            stripes = chunk.stripes
        else:
            stripes = [stripe for stripe in chunk.stripes if stripe.devid in devids]
        if len(stripes) == 0:
            continue
        try:
            block_group = fs.block_group(chunk.vaddr, chunk.length)
        except IndexError:
            continue
        used_pct = block_group.used / block_group.length
        length = chunk.length * len(stripes)
        if verbose >= 1:
            print(block_group)
            print(chunk)
            for stripe in stripes:
                print("    {}".format(stripe))
        grid.fill(byte_offset, length, used_pct,
                  dev_extent_colors[block_group.flags & btrfs.BLOCK_GROUP_TYPE_MASK])
        byte_offset += length
    return grid


def walk_dev_extents(fs, devices=None, order=None, size=None,
                     default_granularity=33554432, verbose=0, min_brightness=None, curve=None):
    if devices is None:
        devices = list(fs.devices())
        dev_extents = fs.dev_extents()
    else:
        if isinstance(devices, types.GeneratorType):
            devices = list(devices)
        dev_extents = (dev_extent
                       for device in devices
                       for dev_extent in fs.dev_extents(device.devid, device.devid))

    print("scope device {}".format(' '.join([str(device.devid) for device in devices])))
    total_bytes = 0
    device_grid_offset = {}
    for device in devices:
        device_grid_offset[device.devid] = total_bytes
        total_bytes += device.total_bytes

    grid = Grid(order, size, total_bytes, default_granularity, verbose, min_brightness, curve)
    block_group_cache = {}
    for dev_extent in dev_extents:
        if dev_extent.vaddr in block_group_cache:
            block_group = block_group_cache[dev_extent.vaddr]
        else:
            try:
                block_group = fs.block_group(dev_extent.vaddr)
            except IndexError:
                continue
            if block_group.flags & btrfs.BLOCK_GROUP_PROFILE_MASK != 0:
                block_group_cache[dev_extent.vaddr] = block_group
        used_pct = block_group.used / block_group.length
        if verbose >= 1:
            print("dev_extent devid {0} paddr {1} length {2} pend {3} type {4} "
                  "used_pct {5:.2f}".format(dev_extent.devid, dev_extent.paddr, dev_extent.length,
                                            dev_extent.paddr + dev_extent.length - 1,
                                            btrfs.utils.block_group_flags_str(block_group.flags),
                                            used_pct * 100))
        first_byte = device_grid_offset[dev_extent.devid] + dev_extent.paddr
        grid.fill(first_byte, dev_extent.length, used_pct,
                  dev_extent_colors[block_group.flags & btrfs.BLOCK_GROUP_TYPE_MASK])
    return grid


def _get_metadata_root(extent):
    if extent.refs > 1:
        return btrfs.ctree.FS_TREE_OBJECTID
    if len(extent.shared_block_refs) > 0:
        return btrfs.ctree.FS_TREE_OBJECTID
    root = extent.tree_block_refs[0].root
    if root >= btrfs.ctree.FIRST_FREE_OBJECTID and root <= btrfs.ctree.LAST_FREE_OBJECTID:
        return btrfs.ctree.FS_TREE_OBJECTID
    return root


def walk_extents(fs, block_groups, order=None, size=None, default_granularity=None, verbose=0,
                 curve=None):
    if isinstance(block_groups, types.GeneratorType):
        block_groups = list(block_groups)
    fs_info = fs.fs_info()
    nodesize = fs_info.nodesize

    if default_granularity is None:
        default_granularity = fs_info.sectorsize

    print("scope block_group {}".format(' '.join([str(b.vaddr) for b in block_groups])))
    total_bytes = 0
    block_group_grid_offset = {}
    for block_group in block_groups:
        block_group_grid_offset[block_group] = total_bytes - block_group.vaddr
        total_bytes += block_group.length

    grid = Grid(order, size, total_bytes, default_granularity, verbose, curve=curve)

    tree = btrfs.ctree.EXTENT_TREE_OBJECTID
    for block_group in block_groups:
        if verbose > 0:
            print(block_group)
        if block_group.flags & btrfs.BLOCK_GROUP_TYPE_MASK == btrfs.BLOCK_GROUP_DATA:
            # Only DATA, so also not DATA|METADATA (mixed).  In this case we
            # take a shortcut. Since we know that all extents are data extents,
            # which get their usual white color, we don't need to load the
            # actual extent objects.
            min_key = btrfs.ctree.Key(block_group.vaddr, 0, 0)
            max_key = btrfs.ctree.Key(block_group.vaddr + block_group.length, 0, 0) - 1
            for header, _ in btrfs.ioctl.search_v2(fs.fd, tree, min_key, max_key, buf_size=65536):
                if header.type == btrfs.ctree.EXTENT_ITEM_KEY:
                    length = header.offset
                    first_byte = block_group_grid_offset[block_group] + header.objectid
                    if verbose >= 1:
                        print("extent vaddr {0} first_byte {1} type {2} length {3}".format(
                            header.objectid, first_byte,
                            btrfs.ctree.key_type_str(header.type), length))
                    grid.fill(first_byte, length, 1, white)

        else:
            # The block group is METADATA or DATA|METADATA or SYSTEM (chunk
            # tree metadata).  We load all extent info to figure out which
            # btree root metadata extents belong to.
            min_vaddr = block_group.vaddr
            max_vaddr = block_group.vaddr + block_group.length - 1
            for extent in fs.extents(min_vaddr, max_vaddr,
                                     load_data_refs=True, load_metadata_refs=True):
                if isinstance(extent, btrfs.ctree.ExtentItem):
                    length = extent.length
                    if extent.flags & btrfs.ctree.EXTENT_FLAG_DATA:
                        color = white
                    elif extent.flags & btrfs.ctree.EXTENT_FLAG_TREE_BLOCK:
                        color = metadata_extent_colors[_get_metadata_root(extent)]
                    else:
                        raise Exception("BUG: expected either DATA or TREE_BLOCK flag, but got "
                                        "{}".format(btrfs.utils.extent_flags_str(extent.flags)))
                elif isinstance(extent, btrfs.ctree.MetaDataItem):
                    length = nodesize
                    color = metadata_extent_colors[_get_metadata_root(extent)]
                first_byte = block_group_grid_offset[block_group] + extent.vaddr
                if verbose >= 1:
                    print("extent vaddr {0} first_byte {1} type {2} length {3}".format(
                          extent.vaddr, first_byte,
                          btrfs.ctree.key_type_str(extent.key.type), length))
                grid.fill(first_byte, length, 1, color)
    return grid


def choose_order_size(order=None, size=None, total_bytes=None, default_granularity=None):
    order_was_none = order is None
    if order_was_none:
        import math
        order = min(10, int(math.ceil(math.log(math.sqrt(total_bytes/default_granularity), 2))))
    if size is None:
        if order > 10:
            size = order
        else:
            size = 10
    if size < order:
        if order_was_none:
            order = size
        else:
            raise HeatmapError("size ({}) cannot be smaller than order ({})".format(size, order))
    return order, size


def generate_png_file_name(output=None, parts=None):
    if output is not None and os.path.isdir(output):
        output_dir = output
        output_file = None
    else:
        output_dir = None
        output_file = output
    if output_file is None:
        if parts is None:
            parts = []
        else:
            parts.append('at')
        import time
        parts.append(str(int(time.time())))
        output_file = '_'.join([str(part) for part in parts]) + '.png'
    if output_dir is None:
        return output_file
    return os.path.join(output_dir, output_file)


def _write_png(pngfile, width, height, rows, color_type=2):
    struct_len = struct_crc = struct.Struct('!I')
    out = open(pngfile, 'wb')
    out.write(b'\x89PNG\r\n\x1a\n')
    # IHDR
    out.write(struct_len.pack(13))
    ihdr = struct.Struct('!4s2I5B').pack(b'IHDR', width, height, 8, color_type, 0, 0, 0)
    out.write(ihdr)
    out.write(struct_crc.pack(zlib.crc32(ihdr) & 0xffffffff))
    # IDAT
    length_pos = out.tell()
    out.write(b'\x00\x00\x00\x00IDAT')
    crc = zlib.crc32(b'IDAT')
    datalen = 0
    compress = zlib.compressobj()
    for row in rows:
        for uncompressed in (b'\x00', b''.join(row)):
            compressed = compress.compress(uncompressed)
            if len(compressed) > 0:
                crc = zlib.crc32(compressed, crc)
                datalen += len(compressed)
                out.write(compressed)
    compressed = compress.flush()
    if len(compressed) > 0:
        crc = zlib.crc32(compressed, crc)
        datalen += len(compressed)
        out.write(compressed)
    out.write(struct_crc.pack(crc & 0xffffffff))
    # IEND
    out.write(b'\x00\x00\x00\x00IEND\xae\x42\x60\x82')
    # Go back and write length of the IDAT
    out.seek(length_pos)
    out.write(struct_len.pack(datalen))
    out.close()


def main():
    args = parse_args()
    path = args.mountpoint
    verbose = args.verbose if args.verbose is not None else 0

    fs = btrfs.FileSystem(path)
    fs_info = fs.fs_info()
    print(fs_info)

    filename_parts = ['fsid', fs.fsid]
    if args.curve != 'hilbert':
        filename_parts.append(args.curve)
    bg_vaddr = args.blockgroup
    if bg_vaddr is None:
        if args.sort == 'physical':
            grid = walk_dev_extents(fs, order=args.order, size=args.size, verbose=verbose,
                                    curve=args.curve)
        elif args.sort == 'virtual':
            filename_parts.append('chunks')
            grid = walk_chunks(fs, order=args.order, size=args.size, verbose=verbose,
                               curve=args.curve)
        else:
            raise HeatmapError("Invalid sort option {}".format(args.sort))
    else:
        try:
            block_group = fs.block_group(bg_vaddr)
        except IndexError:
            raise HeatmapError("Error: no block group at vaddr {}!".format(bg_vaddr))
        grid = walk_extents(fs, [block_group], order=args.order, size=args.size, verbose=verbose,
                            curve=args.curve)
        filename_parts.extend(['blockgroup', block_group.vaddr])

    grid.write_png(generate_png_file_name(args.output, filename_parts))


if __name__ == '__main__':
    try:
        main()
    except HeatmapError as e:
        print("Error: {0}".format(e), file=sys.stderr)
        sys.exit(1)
