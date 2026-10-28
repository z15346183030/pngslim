"""Tests for pngslim.

Run with:  python -m unittest discover -s tests
"""

import os
import random
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import pngslim as P  # noqa: E402

ADAM7 = P.ADAM7
CHANNELS = P.CHANNELS


# --------------------------------------------------------------------------
# Helpers that build PNG files from scratch
# --------------------------------------------------------------------------


def chunk(ctype: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + ctype
        + data
        + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
    )


def random_rows(width, height, color_type, bit_depth, seed=1):
    bits = CHANNELS[color_type] * bit_depth
    rowbytes = (width * bits + 7) // 8
    rng = random.Random(seed)
    return [bytes(rng.randrange(256) for _ in range(rowbytes)) for _ in range(height)]


def smooth_rows(width, height, color_type, bit_depth, seed=2):
    """Rows with a gradient plus a little noise - compressible, like real images."""
    bits = CHANNELS[color_type] * bit_depth
    rowbytes = (width * bits + 7) // 8
    rng = random.Random(seed)
    rows = []
    for y in range(height):
        row = bytearray(rowbytes)
        for x in range(rowbytes):
            row[x] = (x * 3 + y * 5 + rng.randrange(4)) & 0xFF
        rows.append(bytes(row))
    return rows


def assemble(
    width,
    height,
    color_type,
    bit_depth,
    passes,
    filter_type=0,
    interlace=0,
    extra_before=(),
    extra_after=(),
    palette=None,
    level=6,
):
    """passes: list of row lists, one per Adam7 pass (or a single list)."""
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, interlace)
    raw = bytearray()
    for rows in passes:
        prev = bytes(len(rows[0])) if rows else b""
        for row in rows:
            raw.append(filter_type)
            raw += P.filter_row(filter_type, row, prev, max(1, CHANNELS[color_type] * bit_depth // 8))
            prev = row

    parts = [P.PNG_SIGNATURE, chunk(b"IHDR", ihdr)]
    if palette is not None:
        parts.append(chunk(b"PLTE", palette))
    for ctype, data in extra_before:
        parts.append(chunk(ctype, data))
    parts.append(chunk(b"IDAT", zlib.compress(bytes(raw), level)))
    for ctype, data in extra_after:
        parts.append(chunk(ctype, data))
    parts.append(chunk(b"IEND", b""))
    return b"".join(parts)


def adam7_passes(width, height, color_type, bit_depth, seed=3):
    bits = CHANNELS[color_type] * bit_depth
    rng = random.Random(seed)
    out = []
    for x0, y0, dx, dy in ADAM7:
        w = (width - x0 + dx - 1) // dx if width > x0 else 0
        h = (height - y0 + dy - 1) // dy if height > y0 else 0
        if w == 0 or h == 0:
            out.append([])
            continue
        rowbytes = (w * bits + 7) // 8
        out.append([bytes(rng.randrange(256) for _ in range(rowbytes)) for _ in range(h)])
    return out


def decode_rows(blob):
    chunks = P.parse_chunks(blob)
    header = P.read_header(chunks)
    return header, [p.rows for p in P.decode_image(chunks, header)]


def naive_unfilter(ftype, data, prev, bpp):
    """A deliberately separate implementation, used to cross-check decoding."""
    out = bytearray(len(data))
    for i in range(len(data)):
        a = out[i - bpp] if i >= bpp else 0
        b = prev[i]
        c = prev[i - bpp] if i >= bpp else 0
        if ftype == 0:
            pred = 0
        elif ftype == 1:
            pred = a
        elif ftype == 2:
            pred = b
        elif ftype == 3:
            pred = (a + b) // 2
        else:
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
        out[i] = (data[i] + pred) & 0xFF
    return bytes(out)


# --------------------------------------------------------------------------
# Byte level arithmetic
# --------------------------------------------------------------------------


class FilterMathTests(unittest.TestCase):
    def test_swar_subtract_matches_per_byte_loop(self):
        rng = random.Random(11)
        for size in (1, 2, 5, 17, 256, 4096):
            a = bytes(rng.randrange(256) for _ in range(size))
            b = bytes(rng.randrange(256) for _ in range(size))
            expected = bytes((x - y) & 0xFF for x, y in zip(a, b))
            self.assertEqual(P._swar_sub(a, b), expected)

    def test_swar_add_matches_per_byte_loop(self):
        rng = random.Random(12)
        for size in (1, 2, 5, 17, 256, 4096):
            a = bytes(rng.randrange(256) for _ in range(size))
            b = bytes(rng.randrange(256) for _ in range(size))
            expected = bytes((x + y) & 0xFF for x, y in zip(a, b))
            self.assertEqual(P._swar_add(a, b), expected)

    def test_extreme_values_do_not_leak_between_lanes(self):
        a = bytes([255, 0, 255, 0, 128, 127])
        b = bytes([0, 255, 0, 255, 127, 128])
        self.assertEqual(P._swar_sub(a, b), bytes((x - y) & 0xFF for x, y in zip(a, b)))
        self.assertEqual(P._swar_add(a, b), bytes((x + y) & 0xFF for x, y in zip(a, b)))

    def test_filter_unfilter_roundtrip_every_type_and_bpp(self):
        rng = random.Random(13)
        for bpp in (1, 2, 3, 4, 6, 8):
            for ftype in range(5):
                row = bytes(rng.randrange(256) for _ in range(97 * bpp))
                prev = bytes(rng.randrange(256) for _ in range(97 * bpp))
                filtered = P.filter_row(ftype, row, prev, bpp)
                self.assertEqual(
                    P.unfilter_row(ftype, filtered, prev, bpp),
                    row,
                    msg=f"filter {ftype} bpp {bpp}",
                )

    def test_average_filter_uses_floor_of_mean(self):
        # first pixel: left=0, above=1 -> mean 0, so the byte passes through
        # second pixel: left=1, above=2 -> mean 1, so 3 becomes 2
        row = bytes([1, 3])
        prev = bytes([1, 2])
        self.assertEqual(P.filter_row(3, row, prev, 1), bytes([1, 2]))

    def test_paeth_first_pixel_uses_pixel_above(self):
        row = bytes([10, 20])
        prev = bytes([4, 6])
        self.assertEqual(P.filter_row(4, row, prev, 1)[0], 6)


# --------------------------------------------------------------------------
# Round trips through the whole pipeline
# --------------------------------------------------------------------------


class RoundTripTests(unittest.TestCase):
    def assert_pixels_equal(self, before, after):
        header_a, rows_a = decode_rows(before)
        header_b, rows_b = decode_rows(after)
        self.assertEqual(header_a, header_b)
        self.assertEqual(rows_a, rows_b)

    def test_rgb8(self):
        rows = smooth_rows(64, 40, 2, 8)
        blob = assemble(64, 40, 2, 8, [rows])
        out, outcome = P.slim_bytes(blob)
        self.assertLessEqual(len(out), len(blob))
        self.assert_pixels_equal(blob, out)

    def test_rgba8(self):
        rows = smooth_rows(48, 33, 6, 8)
        blob = assemble(48, 33, 6, 8, [rows])
        out, _ = P.slim_bytes(blob)
        self.assert_pixels_equal(blob, out)

    def test_rgba16(self):
        rows = smooth_rows(20, 12, 6, 16)
        blob = assemble(20, 12, 6, 16, [rows])
        out, _ = P.slim_bytes(blob)
        self.assert_pixels_equal(blob, out)

    def test_grayscale_1bit(self):
        rows = random_rows(37, 9, 0, 1)
        blob = assemble(37, 9, 0, 1, [rows])
        out, _ = P.slim_bytes(blob)
        self.assert_pixels_equal(blob, out)

    def test_palette_4bit(self):
        rows = random_rows(31, 11, 3, 4)
        palette = bytes(range(48))
        blob = assemble(31, 11, 3, 4, [rows], palette=palette)
        out, _ = P.slim_bytes(blob)
        self.assert_pixels_equal(blob, out)

    def test_palette_with_transparency(self):
        rows = random_rows(16, 16, 3, 8)
        blob = assemble(
            16,
            16,
            3,
            8,
            [rows],
            palette=bytes(range(30)),
            extra_before=[(b"tRNS", bytes([0, 128, 255]))],
        )
        out, _ = P.slim_bytes(blob)
        self.assert_pixels_equal(blob, out)
        types = [c.type for c in P.parse_chunks(out)]
        self.assertIn(b"tRNS", types)

    def test_interlaced(self):
        passes = adam7_passes(45, 31, 2, 8)
        blob = assemble(45, 31, 2, 8, passes, interlace=1)
        out, _ = P.slim_bytes(blob)
        self.assert_pixels_equal(blob, out)

    def test_one_pixel_wide(self):
        rows = random_rows(1, 50, 6, 8)
        blob = assemble(1, 50, 6, 8, [rows])
        out, _ = P.slim_bytes(blob)
        self.assert_pixels_equal(blob, out)

    def test_single_row_image(self):
        rows = random_rows(200, 1, 2, 8)
        blob = assemble(200, 1, 2, 8, [rows])
        out, _ = P.slim_bytes(blob)
        self.assert_pixels_equal(blob, out)

    def test_decode_matches_independent_unfilter(self):
        """Decode a file whose rows we know, and check them byte for byte."""
        rows = smooth_rows(23, 7, 2, 8)
        blob = assemble(23, 7, 2, 8, [rows], filter_type=4)
        _, decoded = decode_rows(blob)
        self.assertEqual(decoded[0], rows)

    def test_independent_unfilter_agrees_with_ours(self):
        rng = random.Random(21)
        row = bytes(rng.randrange(256) for _ in range(64))
        prev = bytes(rng.randrange(256) for _ in range(64))
        for ftype in range(5):
            filtered = P.filter_row(ftype, row, prev, 2)
            self.assertEqual(naive_unfilter(ftype, filtered, prev, 2), row)


# --------------------------------------------------------------------------
# What survives the rewrite
# --------------------------------------------------------------------------


class PreservationTests(unittest.TestCase):
    def test_ancillary_chunks_kept_verbatim_and_in_order(self):
        rows = smooth_rows(40, 30, 2, 8)
        before = [(b"gAMA", struct.pack(">I", 45455)), (b"pHYs", bytes(9))]
        after = [(b"tEXt", b"Comment\x00hello"), (b"tIME", bytes(7))]
        blob = assemble(40, 30, 2, 8, [rows], extra_before=before, extra_after=after)
        out, _ = P.slim_bytes(blob)
        old_types = [c.type for c in P.parse_chunks(blob)]
        new_types = [c.type for c in P.parse_chunks(out)]
        self.assertEqual(old_types, new_types)
        for ctype, data in before + after:
            self.assertIn((ctype, data), [(c.type, c.data) for c in P.parse_chunks(out)])

    def test_header_is_byte_identical(self):
        rows = smooth_rows(30, 20, 6, 8)
        blob = assemble(30, 20, 6, 8, [rows])
        out, _ = P.slim_bytes(blob)
        self.assertEqual(P.parse_chunks(blob)[0].data, P.parse_chunks(out)[0].data)

    def test_several_idat_chunks_are_merged_into_one(self):
        rows = smooth_rows(50, 20, 2, 8)
        whole = assemble(50, 20, 2, 8, [rows])
        chunks = P.parse_chunks(whole)
        ihdr = chunks[0]
        idat = P.idat_bytes(chunks)
        cut = len(idat) // 2
        blob = b"".join(
            [
                P.PNG_SIGNATURE,
                chunk(b"IHDR", ihdr.data),
                chunk(b"IDAT", idat[:cut]),
                chunk(b"IDAT", idat[cut:]),
                chunk(b"IEND", b""),
            ]
        )
        out, _ = P.slim_bytes(blob)
        self.assertEqual(sum(1 for c in P.parse_chunks(out) if c.type == b"IDAT"), 1)
        _, rows_a = decode_rows(blob)
        _, rows_b = decode_rows(out)
        self.assertEqual(rows_a, rows_b)

    def test_output_size_prediction_matches_real_file(self):
        rows = smooth_rows(33, 17, 2, 8)
        blob = assemble(33, 17, 2, 8, [rows])
        chunks = P.parse_chunks(blob)
        idat = P.idat_bytes(chunks)
        self.assertEqual(P.output_size(chunks, len(idat)), len(blob))


# --------------------------------------------------------------------------
# Behaviour guarantees
# --------------------------------------------------------------------------


class BehaviourTests(unittest.TestCase):
    def test_never_writes_a_bigger_file(self):
        rows = smooth_rows(60, 60, 2, 8)
        blob = assemble(60, 60, 2, 8, [rows], filter_type=2, level=9)
        out, outcome = P.slim_bytes(blob)
        self.assertLessEqual(len(out), len(blob))
        if outcome.status == "minimal":
            self.assertEqual(out, blob)

    def test_second_run_is_a_no_op(self):
        rows = smooth_rows(80, 50, 6, 8)
        blob = assemble(80, 50, 6, 8, [rows])
        once, _ = P.slim_bytes(blob)
        twice, outcome = P.slim_bytes(once)
        self.assertEqual(once, twice)
        self.assertEqual(outcome.status, "minimal")

    def test_optimised_file_reports_minimal(self):
        rows = smooth_rows(40, 40, 2, 8)
        blob = assemble(40, 40, 2, 8, [rows])
        out, _ = P.slim_bytes(blob)
        again, outcome = P.slim_bytes(out)
        self.assertEqual(outcome.status, "minimal")
        self.assertEqual(outcome.saved, 0)

    def test_outcome_records_the_winning_filter(self):
        rows = smooth_rows(70, 40, 2, 8)
        blob = assemble(70, 40, 2, 8, [rows], filter_type=0)
        out, outcome = P.slim_bytes(blob)
        self.assertEqual(outcome.status, "smaller")
        self.assertIn(outcome.filter_name, P.STRATEGIES)
        self.assertGreater(outcome.saved, 0)
        self.assertGreater(len(outcome.attempts), 1)

    def test_verification_catches_a_broken_filter(self):
        rows = smooth_rows(20, 10, 2, 8)
        blob = assemble(20, 10, 2, 8, [rows])
        chunks = P.parse_chunks(blob)
        header = P.read_header(chunks)
        passes = P.decode_image(chunks, header)
        tampered = bytearray(blob)
        # Flip a byte inside the IDAT payload.
        idat_start = blob.index(b"IDAT") + 4
        tampered[idat_start + 5] ^= 0xFF
        with self.assertRaises(P.PngError):
            P._verify(chunks, header, passes, bytes(tampered), b"nope")


# --------------------------------------------------------------------------
# Bad input
# --------------------------------------------------------------------------


class BadInputTests(unittest.TestCase):
    def test_not_a_png(self):
        with self.assertRaises(P.PngError) as ctx:
            P.parse_chunks(b"\xff\xd8\xff\xe0" + b"0" * 40)
        self.assertIn("JPEG", str(ctx.exception))

    def test_truncated_file(self):
        rows = smooth_rows(20, 20, 2, 8)
        blob = assemble(20, 20, 2, 8, [rows])
        with self.assertRaises(P.PngError) as ctx:
            P.slim_bytes(blob[: len(blob) // 2])
        self.assertIn("truncated", str(ctx.exception))

    def test_critical_chunk_with_bad_crc_is_refused(self):
        rows = smooth_rows(10, 10, 2, 8)
        blob = bytearray(assemble(10, 10, 2, 8, [rows]))
        blob[20] ^= 0xFF  # inside IHDR payload
        with self.assertRaises(P.PngError) as ctx:
            P.parse_chunks(bytes(blob))
        self.assertIn("CRC", str(ctx.exception))

    def test_zero_height(self):
        ihdr = struct.pack(">IIBBBBB", 4, 0, 8, 2, 0, 0, 0)
        blob = b"".join(
            [
                P.PNG_SIGNATURE,
                chunk(b"IHDR", ihdr),
                chunk(b"IDAT", zlib.compress(b"\x00" * 13)),
                chunk(b"IEND", b""),
            ]
        )
        with self.assertRaises(P.PngError) as ctx:
            P.slim_bytes(blob)
        self.assertIn("zero", str(ctx.exception))

    def test_apng_is_skipped_with_a_clear_reason(self):
        rows = smooth_rows(8, 8, 2, 8)
        blob = assemble(8, 8, 2, 8, [rows], extra_before=[(b"acTL", bytes(8))])
        with self.assertRaises(P.PngError) as ctx:
            P.slim_bytes(blob)
        self.assertIn("APNG", str(ctx.exception))

    def test_broken_idat(self):
        ihdr = struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)
        blob = b"".join(
            [
                P.PNG_SIGNATURE,
                chunk(b"IHDR", ihdr),
                chunk(b"IDAT", b"not zlib data"),
                chunk(b"IEND", b""),
            ]
        )
        with self.assertRaises(P.PngError) as ctx:
            P.slim_bytes(blob)
        self.assertIn("inflate", str(ctx.exception))


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def run_cli(*args):
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "pngslim.py"), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


class CliTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pngslim-test-")
        self.path = os.path.join(self.dir, "sample.png")
        rows = smooth_rows(90, 60, 2, 8)
        with open(self.path, "wb") as stream:
            stream.write(assemble(90, 60, 2, 8, [rows]))

    def test_dry_run_leaves_the_file_alone(self):
        with open(self.path, "rb") as stream:
            before = stream.read()
        result = run_cli(self.path, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.path, "rb") as stream:
            self.assertEqual(stream.read(), before)
        self.assertIn("would save", result.stdout)

    def test_check_exits_one_when_there_is_a_win(self):
        result = run_cli(self.path, "--check")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("can still shrink", result.stdout)

    def test_check_exits_zero_after_optimising(self):
        run_cli(self.path)
        result = run_cli(self.path, "--check")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("already as small", result.stdout)

    def test_writes_a_smaller_file(self):
        before = os.path.getsize(self.path)
        result = run_cli(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(os.path.getsize(self.path), before)
        self.assertIn("saved", result.stdout)

    def test_directory_is_walked_recursively(self):
        nested = os.path.join(self.dir, "sub", "deeper")
        os.makedirs(nested)
        rows = smooth_rows(30, 30, 2, 8)
        with open(os.path.join(nested, "b.png"), "wb") as stream:
            stream.write(assemble(30, 30, 2, 8, [rows]))
        result = run_cli(self.dir, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("b.png", result.stdout)

    def test_json_output_is_parsable(self):
        import json

        result = run_cli(self.path, "--dry-run", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["files"][0]["path"], self.path)
        self.assertIn("total_before", payload)

    def test_missing_path_is_reported(self):
        result = run_cli(os.path.join(self.dir, "nope.png"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("no such file", result.stderr)


if __name__ == "__main__":
    unittest.main()
