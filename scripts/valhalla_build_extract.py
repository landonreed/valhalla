#!/usr/bin/env python3

import argparse
import ctypes
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import struct
import sys
import tarfile
from tarfile import BLOCKSIZE
from time import time
from typing import Dict, Tuple, List

# "<" prefix means little-endian and no alignment
# order is important! if uint64_t is not first, c++ will use padding bytes to unpack
from typing import List, Tuple

# "<" prefix means little-endian and no alignment
# order is important! if uint64_t is not first, c++ will use padding bytes to unpack
INDEX_BIN_FORMAT = '<QLL'
INDEX_BIN_SIZE = struct.calcsize(INDEX_BIN_FORMAT)
INDEX_FILE = "index.bin"
# skip the first 40 bytes of the tile header
GRAPHTILE_SKIP_BYTES = struct.calcsize('<Q2f16cQ')
TRAFFIC_HEADER_SIZE = struct.calcsize('<2Q4I')
TRAFFIC_SPEED_SIZE = struct.calcsize('<Q')


class TileHeader(ctypes.Structure):
    """
    Resembles the uint64_t bit field bytes 40 - 48 of the
    graphtileheader to get the directededgecount_.
    """
    _fields_ = [
        ("nodecount_", ctypes.c_ulonglong, 21),
        ("directededgecount_", ctypes.c_ulonglong, 21),
        ("predictedspeeds_count_", ctypes.c_ulonglong, 21),
        ("spare1_", ctypes.c_ulonglong, 1)
    ]


description = "Builds a tar extract from the tiles in mjolnir.tile_dir to the path specified in mjolnir.tile_extract."
parser = argparse.ArgumentParser(description=description)
parser.add_argument("-c", "--config", help="Absolute or relative path to the Valhalla config JSON.", type=Path)
parser.add_argument("-t", "--traffic", help="Flag to add a traffic.tar skeleton", action="store_true", default=False)
parser.add_argument("-v", "--verbosity", help="Accumulative verbosity flags; -v: INFO, -vv: DEBUG", action='count', default=0)

# set up the logger basics
LOGGER = logging.getLogger(__name__)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)5s: %(message)s"))
LOGGER.addHandler(handler)


def get_tile_count(in_path: Path) -> int:
    """Recursively fills the passed map with tile path & size info"""
    count = 0
    for _, _, files in os.walk(in_path):
        count += len(list(filter(lambda f: f.endswith('.gph'), files)))

    return count


def get_tile_id(path: str):
    """Turns a tile path into a numeric GraphId"""
    level, idx = path[:-4].split('/', 1)

    return int(level) | (int(idx.replace('/', '')) << 3)


def create_extracts(tiles_fp_: Path, extract_fp_: Path, traffic_fp_: Path, do_traffic: bool):
    """Actually creates the tar ball. Break out of main function for testability."""
    if not tiles_fp_.exists() or not tiles_fp_.is_dir():
        LOGGER.critical(f"Directory 'mjolnir.tile_dir': {tiles_fp_} was not found on the filesystem.")
        sys.exit(1)

    tiles_count = get_tile_count(tiles_fp_)
    if not tiles_count:
        LOGGER.critical(f"Directory {tiles_fp_} does not contain any usable graph tiles.")
        sys.exit(1)

    # write the in-memory index file
    index_size = INDEX_BIN_SIZE * tiles_count
    index_fd = BytesIO(b'0' * index_size)
    index_fd.seek(0)

    # first add the index file, then the tile dir to the tarfile
    with tarfile.open(extract_fp_, 'w') as tar:
        tarinfo = tarfile.TarInfo(INDEX_FILE)
        tarinfo.size = index_size
        tarinfo.mtime = int(time())
        tar.addfile(tarinfo, index_fd)

        tar.add(str(tiles_fp_), recursive=True, arcname='')

    # get the offset and size from the tarred tile members
    index: List[Tuple[int, int, int]] = list()
    with tarfile.open(extract_fp_, 'r|') as tar:
        for member in tar.getmembers():
            if member.name.endswith('.gph'):
                LOGGER.debug(f"Tile {member.name} with offset: {member.offset_data}, size: {member.size}")

                index.append((member.offset_data, get_tile_id(member.name), member.size))

    # write back the actual index info
    with open(extract_fp_, 'r+b') as tar:
        # jump to the data block, index.bin is the first file
        tar.seek(BLOCKSIZE)
        for entry in index:
            tar.write(struct.pack(INDEX_BIN_FORMAT, *entry))

    LOGGER.info(f"Finished tarring {tiles_count} tiles to {extract_fp_}")

    # exit if no traffic extract wanted
    if not do_traffic:
        index_fd.close()
        sys.exit(0)

    LOGGER.info(f"Start creating traffic extract...")

    # we already have the right size of the index file, simply reset it
    index_fd.seek(0)
    with tarfile.open(extract_fp_) as tar_in, tarfile.open(traffic_fp_, 'w') as tar_traffic:
        # get a reference to the
        in_fileobj = tar_in.fileobj

        # add the index file as first data
        tarinfo = tarfile.TarInfo('index.bin')
        tarinfo.size = index_size
        tarinfo.mtime = int(time())
        tar_traffic.addfile(tarinfo, index_fd)
        index_fd.close()

        for tile_in in tar_in.getmembers():
            if not tile_in.name.endswith('.gph'):
                continue
            # jump to the data's offset and skip the uninteresting bytes
            in_fileobj.seek(tile_in.offset_data + GRAPHTILE_SKIP_BYTES)

            # read the appropriate size of bytes from the tar into the TileHeader struct
            tile_header = TileHeader()
            b = BytesIO(in_fileobj.read(ctypes.sizeof(TileHeader)))
            b.readinto(tile_header)
            b.close()

            LOGGER.debug(f"Tile {tile_in.name} has {tile_header.directededgecount_} directed edges")

            # create the traffic tile
            size = TRAFFIC_HEADER_SIZE + TRAFFIC_SPEED_SIZE * tile_header.directededgecount_
            tarinfo = tarfile.TarInfo(tile_in.name)
            tarinfo.size = TRAFFIC_HEADER_SIZE + TRAFFIC_SPEED_SIZE * tile_header.directededgecount_
            tarinfo.mtime = int(time())
            tar_traffic.addfile(tarinfo, BytesIO(b'\0' * size))

    index_fd.close()

    LOGGER.info(f"Finished creating the traffic extract at {traffic_fp}")


if __name__ == '__main__':
    args = parser.parse_args()
    with open(args.config) as f:
        config = json.load(f)
    extract_fp: Path = Path(config["mjolnir"]["tile_extract"])
    tiles_fp: Path = Path(config["mjolnir"]["tile_dir"])
    traffic_fp: Path = Path(config["mjolnir"]["traffic_extract"])

    # set the right logger level
    if args.verbosity == 0:
        LOGGER.setLevel(logging.CRITICAL)
    elif args.verbosity == 1:
        LOGGER.setLevel(logging.INFO)
    elif args.verbosity >= 2:
        LOGGER.setLevel(logging.DEBUG)

    create_extracts(tiles_fp, extract_fp, traffic_fp, args.traffic)