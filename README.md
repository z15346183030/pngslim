# pngslim

**Make PNG files smaller without changing a single pixel — one file, no dependencies, no install.**

```bash
python pngslim.py assets/
```

```
pngslim: 72 file(s)
  12,496,674 B -> 11,642,134 B   saved 854,540 B (6.8%)   in 63.3s
  shrunk 40, already minimal 29, skipped 3, failed 0
  biggest wins:
    mascot-new.png                704,896 ->   560,485  -20.5%  [paeth/zlib-9-filtered]
    claw.png                      380,141 ->   282,274  -25.7%  [adaptive/zlib-9-filtered]
    qrcode-channel.png            148,025 ->    98,572  -33.4%  [none/zlib-9]
```

Only two things inside the file get rewritten: the filter byte in front of each
scanline, and the deflate stream holding those filtered bytes. The header, the
palette, the transparency chunk, the ICC profile and every text chunk are copied
through byte for byte — so the output cannot decode to anything other than the
original pixels. Every run re-decodes its own result and compares it row by row
before writing.

## Why this exists

`oxipng` and `optipng` are better at this than any pure-Python tool can be: they
link libdeflate or Zopfli, and that alone is worth about 7% more compression than
Python's built-in zlib. If you can install a binary, use them.

This is for the places where you cannot: a locked-down CI runner, an Alpine image
with no cargo, a script someone has to run on a laptop with nothing but Python.
Copy one file, run it, get most of the win.

## Install

Nothing to install. Python 3.9 or newer, standard library only.

```bash
curl -O https://raw.githubusercontent.com/z15346183030/pngslim/main/pngslim.py
python pngslim.py --help
```

## Use

```bash
# one file, in place
python pngslim.py logo.png

# a whole tree, in place
python pngslim.py public/images/

# see what it would do, touch nothing
python pngslim.py public/images/ --dry-run

# fail the build if any PNG in the repo can still shrink
python pngslim.py . --check

# show every filter that was tried, with its size and time
python pngslim.py hero.png --explain
```

| flag | what it does |
|---|---|
| `--dry-run` | report only, write nothing |
| `--check` | write nothing; exit 1 if anything could be smaller (CI gate) |
| `--explain` | print the candidate table for each file (implies `--brute`) |
| `--brute` | run the expensive compressor on every filter, not just the winner |
| `-j N` | worker processes, default is one per core |
| `-v` | one line per file instead of just the summary |
| `--json` | machine readable output |

Exit codes: `0` fine, `1` `--check` found room to shrink (or a file failed),
`2` bad arguments or no PNGs found.

## What it will not do

- **It never changes the image header.** No converting RGBA to palette, no
  dropping a fully opaque alpha channel, no bit depth reduction. Those are real
  wins — `oxipng` gets about 0.4% from them on our test set — but they change the
  file's structure, and downstream tools sometimes care.
- **It never strips metadata.** `tEXt`, `iTXt`, `eXIf`, `iCCP`, `pHYs` all survive.
- **It skips APNG.** Frames live in their own chunks; recompressing one frame
  would change the animation.
- **It refuses corrupt files.** A bad CRC in a critical chunk, a truncated file or
  an IDAT stream that will not inflate is reported, not "fixed".

## How it works

A PNG scanline can be stored five ways. Filter 0 stores the bytes as they are;
filters 1-4 store the difference from the pixel to the left, above, the average
of both, or a three-way guess called Paeth. Smooth images compress far better
after differencing, and sharp flat art (a QR code, a screenshot of text) often
compresses far better without it.

So the job is: try the five filters plus a per-row adaptive mix, pick the one
that compresses smallest, and hand that to deflate at maximum effort.

Ranking the six candidates with the real compressor would be slow, so they are
ranked with a cheap judge — each candidate is filtered in full and compressed at
zlib level 1, which takes a fraction of a second and ranks the same way level 9
does (checked against full level-9 evaluation: same winner on every file tested).
Only the winner is compressed for real, at level 9, with two zlib strategies.

Two details that matter for speed:

- **Row filters run on the whole row at once.** Adding and subtracting bytes one
  at a time in Python costs about 7 MB/s; doing it as a single big integer with
  one byte per lane (carries masked out) costs about 70 MB/s. Paeth needs a
  three-way comparison per byte, so it stays a loop — which is why it is the
  slowest filter by far.
- **Files are processed in parallel**, largest first, because one 5 MB screenshot
  takes longer than twenty icons.

## Measured

72 PNG files taken from a desktop application's asset folder, 12.5 MB in total
(three of them turned out to be JPEGs with a `.png` extension; both tools skip
them, pngslim says so).

| tool | output | saved | wall time |
|---|---|---|---|
| original | 12,496,674 B | — | — |
| **pngslim** | **11,642,134 B** | **854,540 B (6.8%)** | 63 s |
| oxipng `-o 2` | 11,123,331 B | 1,373,343 B (11.0%) | 11 s |
| oxipng `-o 4` | 11,061,030 B | 1,435,644 B (11.5%) | 22 s |
| oxipng `-o 4 --nx` | 11,106,442 B | 1,390,232 B (11.1%) | 18 s |
| oxipng `-o max` | 11,050,122 B | 1,446,552 B (11.6%) | 138 s |

The whole gap is the deflate engine, not the filtering. Take one image, filter it
once with the average filter, then compress that exact byte stream with different
encoders:

| encoder | size of the same filtered stream |
|---|---|
| zlib level 9 (this tool) | 1,332,570 B |
| libdeflate level 9 | 1,302,786 B |
| libdeflate level 12 (oxipng `-o 4`) | 1,235,908 B |

7.3% of pure encoder quality that no amount of Python gets back.

Running pngslim twice on the same tree changes nothing the second time
(854,540 B saved, then 0 B) — the output is stable, so a rebuild does not churn.

## Tests

```bash
python -m unittest discover -s tests
```

39 tests: every filter against a separate per-byte implementation, all colour
types and bit depths including 16-bit and Adam7 interlacing, chunk preservation,
idempotence, corrupt input, and the command line.

## License

MIT. See [LICENSE](LICENSE). No code was taken from the projects this was
inspired by; see [ANALYSIS.md](ANALYSIS.md) for the details.
