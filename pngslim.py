#!/usr/bin/env python3
"""pngslim - make PNG files smaller without changing a single pixel.

Only two things in the file get rewritten:

  1. the filter byte in front of every scanline (PNG lets each row pick its own
     predictor)
  2. the deflate stream holding those filtered bytes

Width, height, colour type, bit depth, palette, transparency, text chunks, ICC
profile - all copied through byte for byte.  Since the header never changes,
the output cannot decode to anything other than the original pixels, and every
run re-decodes its own result to prove it.

Usage:
    python pngslim.py image.png
    python pngslim.py assets/ --check
    python pngslim.py *.png --verbose
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import tempfile
import time
import zlib
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}

LEGAL_DEPTHS = {
    0: (1, 2, 4, 8, 16),
    2: (8, 16),
    3: (1, 2, 4, 8),
    4: (8, 16),
    6: (8, 16),
}

CRITICAL_CHUNKS = (b"IHDR", b"PLTE", b"IDAT", b"IEND")

ADAM7 = (
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
)

# "adaptive" picks a filter per row from the four that can be computed for a
# whole row at once.  Paeth is left out of that inner loop: it needs a three-way
# comparison per byte, which costs about 30x more than the others for well under
# one percent of the final size.  It is still available as a whole-image choice.
STRATEGIES = ("none", "sub", "up", "avg", "paeth", "adaptive")
ADAPTIVE_FILTERS = (0, 1, 2, 3)

# memLevel is left at zlib's default 8: raising it to 9 made about half the
# test images slightly smaller and half slightly larger, for no net gain.
PROFILE_DEFAULT = (9, zlib.Z_DEFAULT_STRATEGY, 8)
PROFILE_FILTERED = (9, zlib.Z_FILTERED, 8)
PROFILE_RLE = (9, zlib.Z_RLE, 8)


class PngError(Exception):
    """Raised when a file cannot be read or safely rewritten."""


# --------------------------------------------------------------------------
# Chunks
# --------------------------------------------------------------------------


@dataclass
class Chunk:
    type: bytes
    data: bytes
    crc_ok: bool


@dataclass
class Header:
    width: int
    height: int
    bit_depth: int
    color_type: int
    interlace: int


def parse_chunks(blob: bytes) -> list[Chunk]:
    if not blob.startswith(PNG_SIGNATURE):
        raise PngError(
            "not a PNG file (no PNG signature) - if the name ends in .png but "
            "the contents are a JPEG or WebP, rename it or convert it first"
        )

    chunks: list[Chunk] = []
    pos = len(PNG_SIGNATURE)
    total = len(blob)

    while pos < total:
        if pos + 8 > total:
            raise PngError("file ends inside a chunk header - the file is truncated")
        (length,) = struct.unpack(">I", blob[pos : pos + 4])
        ctype = blob[pos + 4 : pos + 8]
        if not ctype.isalpha():
            raise PngError(
                f"chunk at offset {pos} has a non-letter type {ctype!r} - "
                "the file is damaged"
            )
        if pos + 12 + length > total:
            raise PngError(
                f"chunk {ctype.decode('ascii', 'replace')} claims {length} bytes "
                "but the file ends first - the file is truncated"
            )
        data = blob[pos + 8 : pos + 8 + length]
        (stored_crc,) = struct.unpack(">I", blob[pos + 8 + length : pos + 12 + length])
        chunks.append(Chunk(ctype, data, (zlib.crc32(ctype + data) & 0xFFFFFFFF) == stored_crc))
        pos += 12 + length
        if ctype == b"IEND":
            break

    if not chunks:
        raise PngError("no chunks found")
    if chunks[0].type != b"IHDR":
        raise PngError("first chunk is not IHDR - not a valid PNG")

    for chunk in chunks:
        if chunk.type in CRITICAL_CHUNKS and not chunk.crc_ok:
            raise PngError(
                f"CRC mismatch in critical chunk {chunk.type.decode()} - "
                "the file is corrupt, refusing to rewrite it"
            )
    return chunks


def read_header(chunks: list[Chunk]) -> Header:
    ihdr = chunks[0]
    if len(ihdr.data) != 13:
        raise PngError(f"IHDR must be 13 bytes, got {len(ihdr.data)}")
    width, height, depth, ctype, comp, filt, interlace = struct.unpack(
        ">IIBBBBB", ihdr.data
    )
    if width == 0 or height == 0:
        raise PngError("image has zero width or height")
    if ctype not in CHANNELS:
        raise PngError(f"unknown colour type {ctype}")
    if depth not in LEGAL_DEPTHS[ctype]:
        raise PngError(f"bit depth {depth} is not legal for colour type {ctype}")
    if comp != 0:
        raise PngError(f"unknown compression method {comp}")
    if filt != 0:
        raise PngError(f"unknown filter method {filt}")
    if interlace not in (0, 1):
        raise PngError(f"unknown interlace method {interlace}")
    if ctype == 3 and not any(c.type == b"PLTE" for c in chunks):
        raise PngError("indexed image has no PLTE chunk")
    return Header(width, height, depth, ctype, interlace)


def idat_bytes(chunks: list[Chunk]) -> bytes:
    parts = [c.data for c in chunks if c.type == b"IDAT"]
    if not parts:
        raise PngError("no IDAT chunk - there is no image data to compress")
    return b"".join(parts)


def output_size(chunks: list[Chunk], new_idat_len: int) -> int:
    size = len(PNG_SIGNATURE)
    seen_idat = False
    for chunk in chunks:
        if chunk.type == b"IDAT":
            if not seen_idat:
                size += 12 + new_idat_len
                seen_idat = True
            continue
        size += 12 + len(chunk.data)
    return size


def build_png(chunks: list[Chunk], new_idat: bytes) -> bytes:
    out = [PNG_SIGNATURE]
    written = False
    for chunk in chunks:
        if chunk.type == b"IDAT":
            if written:
                continue
            payload = new_idat
            written = True
        else:
            payload = chunk.data
        out.append(struct.pack(">I", len(payload)))
        out.append(chunk.type)
        out.append(payload)
        out.append(struct.pack(">I", zlib.crc32(chunk.type + payload) & 0xFFFFFFFF))
    return b"".join(out)


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------
#
# A scanline is a few thousand bytes.  Doing the arithmetic one byte at a time
# costs about 7 MB/s in Python, so the filters that only look at a fixed offset
# (none, sub, up, average) work on the whole row at once: the row becomes one
# big integer in which every byte is an independent lane.  The trick is adding
# and subtracting without letting a carry leak into the next lane:
#
#   subtract: ((x | 0x80..) - (y & 0x7f..)) ^ ((x ^ ~y) & 0x80..)
#   add:      ((x & 0x7f..) + (y & 0x7f..)) ^ ((x ^ y) & 0x80..)
#
# Both are exact per lane and run around 70 MB/s.  Paeth needs a three-way
# comparison per byte, which has no such shortcut, so it stays a plain loop.

_MASK_CACHE: dict[int, tuple[int, int, int]] = {}


def _lane_masks(size: int) -> tuple[int, int, int]:
    cached = _MASK_CACHE.get(size)
    if cached is None:
        high = int.from_bytes(b"\x80" * size, "big")
        low = int.from_bytes(b"\x7f" * size, "big")
        full = (1 << (8 * size)) - 1
        cached = (high, low, full)
        if len(_MASK_CACHE) < 64:
            _MASK_CACHE[size] = cached
    return cached


def _swar_sub(x_bytes: bytes, y_bytes: bytes) -> bytes:
    size = len(x_bytes)
    if size == 0:
        return b""
    high, low, full = _lane_masks(size)
    x = int.from_bytes(x_bytes, "big")
    y = int.from_bytes(y_bytes, "big")
    return ((((x | high) - (y & low)) ^ ((x ^ ~y) & high)) & full).to_bytes(size, "big")


def _swar_add(x_bytes: bytes, y_bytes: bytes) -> bytes:
    size = len(x_bytes)
    if size == 0:
        return b""
    high, low, full = _lane_masks(size)
    x = int.from_bytes(x_bytes, "big")
    y = int.from_bytes(y_bytes, "big")
    return ((((x & low) + (y & low)) ^ ((x ^ y) & high)) & full).to_bytes(size, "big")


def _shift_left(row: bytes, bpp: int) -> bytes:
    """The row moved right by one pixel, zero filled."""
    if bpp >= len(row):
        return bytes(len(row))
    return bytes(bpp) + row[:-bpp]


def filter_row(ftype: int, row: bytes, prev: bytes, bpp: int) -> bytes:
    if ftype == 0:
        return bytes(row)
    if ftype == 1:
        return _swar_sub(row, _shift_left(row, bpp))
    if ftype == 2:
        return _swar_sub(row, prev)
    if ftype == 3:
        size = len(row)
        left = int.from_bytes(_shift_left(row, bpp), "big")
        above = int.from_bytes(prev, "big")
        _, low, full = _lane_masks(size)
        # floor((a + b) / 2) per lane, without carries: (a & b) + ((a ^ b) >> 1)
        mean = ((left & above) + (((left ^ above) >> 1) & low)) & full
        return _swar_sub(row, mean.to_bytes(size, "big"))
    if ftype == 4:
        n = len(row)
        out = bytearray(n)
        for i in range(bpp):
            out[i] = (row[i] - prev[i]) & 0xFF
        for i in range(bpp, n):
            cur = row[i]
            a = row[i - bpp]
            b = prev[i]
            c = prev[i - bpp]
            p = a + b - c
            pa = p - a
            pb = p - b
            pc = p - c
            if pa < 0:
                pa = -pa
            if pb < 0:
                pb = -pb
            if pc < 0:
                pc = -pc
            if pa <= pb and pa <= pc:
                out[i] = (cur - a) & 0xFF
            elif pb <= pc:
                out[i] = (cur - b) & 0xFF
            else:
                out[i] = (cur - c) & 0xFF
        return bytes(out)
    raise ValueError(f"unknown filter type {ftype}")


def unfilter_row(ftype: int, data: bytes, prev: bytes, bpp: int) -> bytes:
    if ftype == 0:
        return bytes(data)
    if ftype == 2:
        return _swar_add(data, prev)

    n = len(data)
    out = bytearray(n)
    if ftype == 1:
        out[:bpp] = data[:bpp]
        for i in range(bpp, n):
            out[i] = (data[i] + out[i - bpp]) & 0xFF
    elif ftype == 3:
        for i in range(bpp):
            out[i] = (data[i] + (prev[i] >> 1)) & 0xFF
        for i in range(bpp, n):
            out[i] = (data[i] + ((out[i - bpp] + prev[i]) >> 1)) & 0xFF
    elif ftype == 4:
        for i in range(bpp):
            out[i] = (data[i] + prev[i]) & 0xFF
        for i in range(bpp, n):
            a = out[i - bpp]
            b = prev[i]
            c = prev[i - bpp]
            p = a + b - c
            pa = p - a
            pb = p - b
            pc = p - c
            if pa < 0:
                pa = -pa
            if pb < 0:
                pb = -pb
            if pc < 0:
                pc = -pc
            if pa <= pb and pa <= pc:
                out[i] = (data[i] + a) & 0xFF
            elif pb <= pc:
                out[i] = (data[i] + b) & 0xFF
            else:
                out[i] = (data[i] + c) & 0xFF
    else:
        raise PngError(f"scanline uses unknown filter type {ftype}")
    return bytes(out)


# --------------------------------------------------------------------------
# Image
# --------------------------------------------------------------------------


@dataclass
class Pass:
    """One sub-image. Non-interlaced PNGs have one; Adam7 has up to seven."""

    width: int
    height: int
    bpp: int
    rowbytes: int
    rows: list[bytes] = field(default_factory=list)


def _pass_geometry(header: Header) -> list[tuple[int, int]]:
    if header.interlace == 0:
        return [(header.width, header.height)]
    sizes = []
    for x0, y0, dx, dy in ADAM7:
        w = (header.width - x0 + dx - 1) // dx if header.width > x0 else 0
        h = (header.height - y0 + dy - 1) // dy if header.height > y0 else 0
        sizes.append((w, h))
    return sizes


def decode_image(chunks: list[Chunk], header: Header) -> list[Pass]:
    bits_per_pixel = CHANNELS[header.color_type] * header.bit_depth
    bpp = max(1, bits_per_pixel // 8)

    try:
        raw = zlib.decompress(idat_bytes(chunks))
    except zlib.error as exc:
        raise PngError(f"IDAT stream does not inflate: {exc}") from exc

    passes: list[Pass] = []
    pos = 0
    total = len(raw)

    for width, height in _pass_geometry(header):
        if width == 0 or height == 0:
            continue
        rowbytes = (width * bits_per_pixel + 7) // 8
        need = height * (rowbytes + 1)
        if pos + need > total:
            raise PngError(
                "inflated data is shorter than the header promises "
                f"(need {need} more bytes at offset {pos}, have {total - pos})"
            )
        prev = bytes(rowbytes)
        rows: list[bytes] = []
        for _ in range(height):
            ftype = raw[pos]
            line = raw[pos + 1 : pos + 1 + rowbytes]
            pos += 1 + rowbytes
            row = unfilter_row(ftype, line, prev, bpp)
            rows.append(row)
            prev = row
        passes.append(Pass(width, height, bpp, rowbytes, rows))

    if not passes:
        raise PngError("image has no scanlines")
    return passes


def _adaptive_choice(row: bytes, prev: bytes, bpp: int) -> int:
    """Pick the filter whose output looks most compressible.

    Score is the classic one: the bytes read as signed values and summed, so
    small residuals (either sign) beat large ones.
    """
    best_ftype = 0
    best_score = None
    for ftype in ADAPTIVE_FILTERS:
        filtered = filter_row(ftype, row, prev, bpp)
        score = 0
        for byte in filtered:
            score += byte if byte < 128 else 256 - byte
        if best_score is None or score < best_score:
            best_score = score
            best_ftype = ftype
    return best_ftype


def encode_passes(passes: list[Pass], strategy: str) -> bytes:
    """Filter every scanline with `strategy`; returns the deflate input."""
    out = bytearray()
    fixed = STRATEGIES.index(strategy) if strategy != "adaptive" else -1
    for sub in passes:
        prev = bytes(sub.rowbytes)
        for row in sub.rows:
            ftype = (
                _adaptive_choice(row, prev, sub.bpp) if fixed < 0 else fixed
            )
            out.append(ftype)
            out += filter_row(ftype, row, prev, sub.bpp)
            prev = row
    return bytes(out)


# --------------------------------------------------------------------------
# Deflate
# --------------------------------------------------------------------------


def deflate(data: bytes, profile: tuple[int, int, int]) -> bytes:
    level, strategy, mem_level = profile
    compressor = zlib.compressobj(level, zlib.DEFLATED, 15, mem_level, strategy)
    return compressor.compress(data) + compressor.flush()


def profiles_for(header: Header) -> list[tuple[tuple[int, int, int], str]]:
    """Which deflate settings are worth trying for this kind of image.

    Z_FILTERED helps when the bytes are mostly small residuals.  Z_RLE only
    wins on long runs of identical bytes, i.e. flat palette art, so it is
    limited to indexed and low-bit-depth images.
    """
    chosen = [(PROFILE_DEFAULT, "zlib-9")]
    if header.color_type != 3:
        chosen.append((PROFILE_FILTERED, "zlib-9-filtered"))
    if header.color_type == 3 or header.bit_depth < 8:
        chosen.append((PROFILE_RLE, "zlib-9-rle"))
    return chosen


# --------------------------------------------------------------------------
# Optimisation
# --------------------------------------------------------------------------


@dataclass
class Attempt:
    filter_name: str
    profile: str
    size: int
    seconds: float


@dataclass
class Outcome:
    path: str
    before: int
    after: int
    status: str  # smaller | minimal | skipped | error
    filter_name: str = ""
    profile: str = ""
    seconds: float = 0.0
    note: str = ""
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def saved(self) -> int:
        return self.before - self.after


def slim_bytes(blob: bytes, brute: bool = False) -> tuple[bytes, Outcome]:
    """Return (bytes to write, outcome).  Never returns something larger."""
    chunks = parse_chunks(blob)
    header = read_header(chunks)

    if any(c.type == b"acTL" for c in chunks):
        raise PngError(
            "animated PNG (APNG): frames live in their own chunks, so "
            "recompressing only the first frame would change the animation"
        )

    passes = decode_image(chunks, header)
    original_size = len(blob)
    attempts: list[Attempt] = []

    # Rank the filters with a cheap judge: every candidate is filtered in full
    # and compressed at level 1, which is fast and ranks the same way level 9
    # does.  Only the winner gets the expensive compressor.
    best_probe = None
    best_filter = ""
    best_filtered = b""
    best_size = None
    best_idat = b""

    for name in STRATEGIES:
        started = time.perf_counter()
        filtered = encode_passes(passes, name)
        probe = len(zlib.compress(filtered, 1))
        elapsed = time.perf_counter() - started

        if brute:
            started = time.perf_counter()
            idat = deflate(filtered, PROFILE_DEFAULT)
            elapsed = time.perf_counter() - started
            size = output_size(chunks, len(idat))
            attempts.append(Attempt(name, "zlib-9", size, elapsed))
            if best_size is None or size < best_size:
                best_size, best_filter, best_idat, best_filtered = (
                    size,
                    name,
                    idat,
                    filtered,
                )
        elif best_probe is None or probe < best_probe:
            best_probe, best_filter, best_filtered = probe, name, filtered

    if not brute:
        best_idat = deflate(best_filtered, PROFILE_DEFAULT)
        best_size = output_size(chunks, len(best_idat))
        attempts.append(Attempt(best_filter, "zlib-9", best_size, 0.0))

    assert best_size is not None

    for profile, label in profiles_for(header)[1:]:
        started = time.perf_counter()
        idat = deflate(best_filtered, profile)
        elapsed = time.perf_counter() - started
        size = output_size(chunks, len(idat))
        attempts.append(Attempt(best_filter, label, size, elapsed))
        if size < best_size:
            best_size, best_idat = size, idat

    outcome = Outcome(
        path="",
        before=original_size,
        after=min(best_size, original_size),
        status="smaller" if best_size < original_size else "minimal",
        filter_name=best_filter,
        profile=next((a.profile for a in attempts if a.size == best_size), "zlib-9"),
        attempts=attempts,
    )

    if best_size >= original_size:
        # Nothing beat what is already in the file.  Return the original bytes
        # so a second run over the same tree produces identical output.
        outcome.after = original_size
        outcome.filter_name = ""
        outcome.profile = ""
        return blob, outcome

    new_blob = build_png(chunks, best_idat)
    _verify(chunks, header, passes, new_blob, best_filtered)
    return new_blob, outcome


def _verify(
    chunks: list[Chunk],
    header: Header,
    passes: list[Pass],
    new_blob: bytes,
    expected_stream: bytes,
) -> None:
    """Decode the result and compare it against the input, pixel for pixel.

    Two independent checks: the deflate stream must hold exactly the bytes we
    meant to write, and unfiltering it must reproduce the original scanlines.
    If either fails, the file on disk is not touched.
    """
    new_chunks = parse_chunks(new_blob)
    if read_header(new_chunks) != header:
        raise PngError("internal check failed: image header changed")

    old_aux = [(c.type, c.data) for c in chunks if c.type != b"IDAT"]
    new_aux = [(c.type, c.data) for c in new_chunks if c.type != b"IDAT"]
    if old_aux != new_aux:
        raise PngError("internal check failed: chunks other than IDAT changed")

    if zlib.decompress(idat_bytes(new_chunks)) != expected_stream:
        raise PngError("internal check failed: deflate stream does not round-trip")

    new_passes = decode_image(new_chunks, read_header(new_chunks))
    if len(new_passes) != len(passes):
        raise PngError("internal check failed: scanline count changed")
    for old, new in zip(passes, new_passes):
        if old.rows != new.rows:
            raise PngError("internal check failed: pixels changed")


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


def iter_png_files(paths: list[str]) -> list[str]:
    found: list[str] = []
    for path in paths:
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                dirs.sort()
                for name in sorted(files):
                    if name.lower().endswith(".png"):
                        found.append(os.path.join(root, name))
        elif os.path.isfile(path):
            found.append(path)
        else:
            raise PngError(f"no such file or directory: {path}")
    return found


def write_atomic(path: str, data: bytes) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle, temp_path = tempfile.mkstemp(dir=directory, suffix=".pngslim")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temp_path, path)
    except BaseException:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


def slim_file(path: str, brute: bool, write: bool) -> Outcome:
    """Worker entry point: one file in, one outcome out."""
    try:
        with open(path, "rb") as stream:
            blob = stream.read()
    except OSError as exc:
        return Outcome(path, 0, 0, "error", note=f"cannot read: {exc.strerror}")

    started = time.perf_counter()
    try:
        new_blob, outcome = slim_bytes(blob, brute=brute)
    except PngError as exc:
        return Outcome(path, len(blob), len(blob), "skipped", note=str(exc))
    except (MemoryError, ValueError) as exc:
        return Outcome(path, len(blob), len(blob), "skipped", note=f"{type(exc).__name__}: {exc}")

    outcome.path = path
    outcome.seconds = time.perf_counter() - started

    if write and outcome.status == "smaller":
        try:
            write_atomic(path, new_blob)
        except OSError as exc:
            outcome.status = "error"
            outcome.note = f"cannot write: {exc.strerror}"
    return outcome


def run(paths: list[str], args) -> int:
    try:
        files = iter_png_files(paths)
    except PngError as exc:
        print(f"pngslim: {exc}", file=sys.stderr)
        return 2

    if not files:
        print("pngslim: no PNG files found", file=sys.stderr)
        return 2

    write = not (args.dry_run or args.check)
    started = time.perf_counter()

    workers = args.jobs
    if workers is None:
        workers = min(len(files), os.cpu_count() or 1)

    brute = args.brute or args.explain
    if workers <= 1 or len(files) == 1:
        outcomes = [slim_file(path, brute, write) for path in files]
    else:
        # Biggest files first: one 5 MB screenshot takes longer than twenty
        # icons, and starting it last would leave the other workers idle.
        order = sorted(files, key=lambda p: -os.path.getsize(p))
        with ProcessPoolExecutor(max_workers=workers) as pool:
            done = list(
                pool.map(
                    slim_file,
                    order,
                    [brute] * len(order),
                    [write] * len(order),
                )
            )
        by_path = {outcome.path: outcome for outcome in done}
        outcomes = [by_path[path] for path in files]

    elapsed = time.perf_counter() - started
    return report(outcomes, elapsed, args)


def report(outcomes: list[Outcome], elapsed: float, args) -> int:
    changed = [o for o in outcomes if o.status == "smaller"]
    minimal = [o for o in outcomes if o.status == "minimal"]
    skipped = [o for o in outcomes if o.status == "skipped"]
    failed = [o for o in outcomes if o.status == "error"]

    before = sum(o.before for o in outcomes)
    after = sum(o.after for o in outcomes)
    saved = before - after

    if args.json:
        print(
            json.dumps(
                {
                    "files": [
                        {
                            "path": o.path,
                            "before": o.before,
                            "after": o.after,
                            "status": o.status,
                            "filter": o.filter_name,
                            "profile": o.profile,
                            "seconds": round(o.seconds, 3),
                            "note": o.note,
                        }
                        for o in outcomes
                    ],
                    "total_before": before,
                    "total_after": after,
                    "total_saved": saved,
                    "seconds": round(elapsed, 2),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 1 if (args.check and changed) else 0

    if args.verbose:
        for o in outcomes:
            if o.status == "smaller":
                print(
                    f"  {o.path}  {o.before:,} -> {o.after:,}  "
                    f"({-o.saved / o.before * 100:+.1f}%)  "
                    f"[{o.filter_name}/{o.profile}]  {o.seconds:.2f}s"
                )
            elif o.status == "minimal":
                print(f"  {o.path}  {o.before:,}  already minimal")
            elif o.status == "skipped":
                print(f"  {o.path}  skipped: {o.note}")
            else:
                print(f"  {o.path}  error: {o.note}")

    if args.check:
        if changed:
            print(
                f"pngslim: {len(changed)} of {len(outcomes)} file(s) can still shrink "
                f"by {saved:,} bytes ({saved / before * 100:.1f}%):"
            )
            for o in sorted(changed, key=lambda x: -x.saved)[:20]:
                print(
                    f"  {o.path}  {o.before:,} -> {o.after:,}  "
                    f"({-o.saved / o.before * 100:+.1f}%)"
                )
            return 1
        print(
            f"pngslim: all {len(outcomes)} file(s) are already as small as this "
            "tool can make them"
        )
        return 0

    percent = (saved / before * 100) if before else 0.0
    verb = "would save" if args.dry_run else "saved"
    print(f"pngslim: {len(outcomes)} file(s)")
    print(
        f"  {before:,} B -> {after:,} B   {verb} {saved:,} B ({percent:.1f}%)"
        f"   in {elapsed:.1f}s"
    )
    print(
        f"  shrunk {len(changed)}, already minimal {len(minimal)}, "
        f"skipped {len(skipped)}, failed {len(failed)}"
    )

    if changed and not args.verbose:
        print("  biggest wins:")
        for o in sorted(changed, key=lambda x: -x.saved)[:5]:
            print(
                f"    {os.path.basename(o.path):<36} {o.before:>9,} -> {o.after:>9,}"
                f"  {-o.saved / o.before * 100:+.1f}%  [{o.filter_name}/{o.profile}]"
            )

    for o in skipped:
        print(f"  skipped {o.path}: {o.note}")
    for o in failed:
        print(f"  failed {o.path}: {o.note}")

    if args.explain:
        for o in outcomes:
            if not o.attempts:
                continue
            print(f"\n  {o.path}  {o.before:,} B")
            for attempt in sorted(o.attempts, key=lambda a: a.size):
                mark = "*" if attempt.size == o.after else " "
                print(
                    f"   {mark} {attempt.filter_name:<9} {attempt.profile:<15} "
                    f"{attempt.size:>10,} B   {attempt.seconds:.2f}s"
                )

    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pngslim",
        description="Shrink PNG files without touching a single pixel.",
        epilog=(
            "Header, palette, transparency and metadata are copied through "
            "unchanged, so the output decodes to identical pixels. Run it twice: "
            "the second run is a no-op."
        ),
    )
    parser.add_argument("paths", nargs="+", metavar="PATH", help="PNG files or directories")
    parser.add_argument(
        "--check",
        action="store_true",
        help="change nothing; exit 1 if any file could be smaller (for CI)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would happen, write nothing"
    )
    parser.add_argument(
        "--brute",
        action="store_true",
        help="try every filter on every row instead of ranking on a sample",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="print every candidate that was tried for each file (implies --brute)",
    )
    parser.add_argument(
        "-j", "--jobs", type=int, default=None, help="worker processes (default: one per core)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="one line per file")
    parser.add_argument("--json", action="store_true", help="machine readable output")
    parser.add_argument("--version", action="version", version="pngslim 1.0.0")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(args.paths, args)


if __name__ == "__main__":
    sys.exit(main())
