# compress_image.py

Shrink images without changing how they look.

Handles **PNG, JPEG, WebP and AVIF**, and converts any of them to WebP or AVIF.

Most design tools export PNGs at 16 bits per channel. That is 8 bytes per pixel
— twice what any logo, icon or social card needs. A 512×514 logo exported this
way can weigh 600KB; at 8-bit it is around 219KB. Same image, same colours,
less than half the bytes.

This script finds those files, re-encodes them, and verifies the result before
overwriting anything. It also strips bulk metadata — C2PA content credentials,
EXIF, embedded thumbnails — which can be a third of a PNG's size while
affecting none of its pixels.

It is a single file with no dependencies beyond the Python standard library and
ImageMagick.

## Quick start

Point it at an image and it asks what you want:

```
$ python3 compress_image.py test.png

Which variant would you like?

  1. all       every variant below, to compare
  2. lossless  8-bit, metadata stripped -- pixel-identical
  3. noalpha   also drop a fully-opaque alpha channel -- pixel-identical
  4. lossy256  quantise to 256 colours -- changes pixels
  5. lossy128  quantise to 128 colours -- changes pixels
  6. lossy64   quantise to 64 colours -- changes pixels

Choose 1-6 [1]:
```

Enter takes the default and generates all of them, so you can compare the sizes
and keep whichever you want. The answer is the instruction — the chosen variant
is written without `--write` as well — and **the original is never overwritten**
in this mode.

It only asks when the command has not already said what it wants:

```bash
python3 compress_image.py public/           # scan a directory: reports, asks nothing
python3 compress_image.py public/ --write   # compress in place, as before
python3 compress_image.py logo.png --lossy 64   # one named variant, no question
python3 compress_image.py logo.png --all        # every variant, no question
```

Off a terminal — piped, scripted, in CI — there is nobody to answer, so every
variant is generated rather than blocking on a prompt.

Outside that prompt the old rule still holds: nothing is written without
`--write` (or `--out`, which implies it).

## Install

Python 3.10+ (the script uses `X | None` type syntax) and ImageMagick.
Optionally `jpegtran` (part of libjpeg-turbo) for lossless JPEG stripping.

```bash
# macOS
brew install imagemagick

# Debian / Ubuntu
sudo apt-get install imagemagick

# Fedora
sudo dnf install ImageMagick
```

Both ImageMagick 7 (`magick`) and ImageMagick 6 (`convert` + `compare`) work.
The script detects which one is present and exits with install instructions if
neither is.

## Usage

```bash
python3 compress_image.py                          # scan ./public
python3 compress_image.py --write                  # scan ./public and apply
python3 compress_image.py logo.png                 # one file
python3 compress_image.py logo.png --write
python3 compress_image.py public app/icons --write # several directories
python3 compress_image.py --dir public/icons       # explicit scan directory
```

Files and directories can be mixed freely in one invocation. Duplicates (a file
named alongside the directory containing it) are collapsed. Directory scanning
is one level deep and picks up `*.png`; a file named explicitly is processed
whatever its extension, because the PNG header is checked anyway.

| Flag | Meaning |
| --- | --- |
| `--write` | Re-encode in place. Without it, the run only reports. |
| `--dir DIR` | Directory to scan when no paths are given. Default `public`. |
| `--out DEST` | Write results to `DEST` instead of overwriting. Implies `--write`. |
| `--suffix TEXT` | Append `TEXT` to each output name, e.g. `--suffix=-min`. |
| `--drop-opaque-alpha` | Discard an alpha channel that is entirely opaque. Lossless. |
| `--lossy [COLORS]` | Quantise to `COLORS` colours (default 256). **Changes pixels.** |
| `--max-lossy-mae MAE` | Error budget for `--lossy`. Default `0.02`. |
| `--all` | Generate every variant as a separate file. Never overwrites the source. |
| `--quality Q` | Re-encode a JPEG/WebP/AVIF at quality `Q` (1-100). |
| `--to FORMAT` | Convert to `webp`, `avif`, `png` or `jpg`. Written as a new file. |

## Other formats

The format is read from the file's magic bytes, not its name — a JPEG called
`.png` is treated as the JPEG it is, and an unrecognised file is skipped rather
than guessed at.

JPEG, WebP and AVIF have no bit depth to reduce and no chunks to splice, so they
get the dial they do have — quality — plus a metadata strip. `--all` on a JPEG:

```
would photo.jpg -> photo-stripped.jpg — 480.2KB -> 461.3KB (-4%, MAE 0.0, metadata stripped, pixels untouched)
would photo.jpg -> photo-q90.jpg      — 480.2KB -> 372.4KB (-22%, MAE 0.007, quality 90)
would photo.jpg -> photo-q82.jpg      — 480.2KB -> 253.5KB (-47%, MAE 0.009, quality 82)
would photo.jpg -> photo-q75.jpg      — 480.2KB -> 205.3KB (-57%, MAE 0.011, quality 75)
would photo.jpg -> photo-webp.webp    — 480.2KB -> 148.0KB (-69%, MAE 0.010, converted to webp)
would photo.jpg -> photo-avif.avif    — 480.2KB ->  65.9KB (-86%, MAE 0.014, converted to avif)
```

Converting is usually where the real saving is. The same 2624KB PNG from above:

| Variant | Size | Change |
| --- | --- | --- |
| `test-lossless.png` | 1786.4KB | -32% |
| `test-lossy64.png` | 306.0KB | -88% |
| `test-webp.webp` | 144.1KB | -95% |
| `test-avif.avif` | 65.5KB | **-98%** |

### Two things worth knowing

**A "strip only" pass must not touch pixels**, which ImageMagick cannot promise
for these formats — it re-encodes (a plain JPEG strip measures MAE 0.0002, not
0). JPEG therefore goes through [`jpegtran`](https://linux.die.net/man/1/jpegtran)
when it is installed, which rearranges the existing coefficients instead of
resampling them: genuinely lossless, and a few percent smaller again. It is
optional — without it, the stripped variant is simply skipped. WebP is written
with `webp:lossless=true`, which is honest but usually *larger* than a lossy
source, so the size guard declines it.

**JPEG cannot store transparency.** Encoding an image with a real alpha channel
to JPEG flattens it against black with no warning at all, so the script refuses:

```
SKIP  logo.png [avif] — jpg cannot store transparency
```

A uniformly opaque alpha channel holds nothing, so it is not in the way.

## Generating every variant at once

To see the whole quality ladder side by side rather than guessing at a setting:

```bash
python3 compress_image.py test.png --all --write
```

That writes five files next to the source and leaves the source itself alone:

```
WROTE test.png -> test-lossless.png — 8-bit 2624.2KB -> 1786.4KB (-32%, MAE 0.0, 417.4KB metadata stripped)
WROTE test.png -> test-noalpha.png  — 8-bit 2624.2KB -> 1611.9KB (-39%, MAE 0.0, opaque alpha dropped)
WROTE test.png -> test-lossy256.png — 8-bit 2624.2KB ->  496.0KB (-81%, MAE 0.011, quantised to 256 colours)
WROTE test.png -> test-lossy128.png — 8-bit 2624.2KB ->  397.5KB (-85%, MAE 0.014, quantised to 128 colours)
WROTE test.png -> test-lossy64.png  — 8-bit 2624.2KB ->  306.0KB (-88%, MAE 0.019, quantised to 64 colours)

5 variant(s) written, 0 source(s) left alone, up to 2318.2KB saved by the smallest.
```

Each variant is judged on its own, so one being refused — because it came out
larger, or past the error budget — says nothing about the others. Refusals name
the variant they belong to:

```
SKIP  photo.png [lossy64] — refusing to write (MAE=0.035 > 0.02, dims ok, alpha ok)
ok    icon.png [lossy256] — result is larger (0.7KB -> 0.8KB), leaving as is
```

The reported saving counts only the *best* variant per source, since these are
alternatives to each other — you ship one, not all five.

`--all` is itself the variant choice, so combining it with `--lossy`,
`--drop-opaque-alpha` or `--suffix` is an error rather than a refinement. With
`--out`, the destination must be a directory.

## Keeping the original

By default the script overwrites in place. To write somewhere else:

```bash
python3 compress_image.py logo.png --out build/         # into a directory
python3 compress_image.py logo.png --out small.png      # to a named file
python3 compress_image.py public --suffix=-min --write  # logo.png -> logo-min.png
```

`--out` is treated as a directory unless it ends in `.png`, and it implies
`--write`. The rule is the file extension rather than "does this path already
exist", so the same command does the same thing on every machine — otherwise
`--out build` would silently create a *file* named `build` on its first run.

A directory scan skips files that already carry the suffix, so re-running
`--suffix=-min` does not produce `logo-min-min.png`.

Note the `=` in `--suffix=-min`: a value starting with `-` needs it, or argparse
reads it as another flag.

## Going smaller than lossless

Both of these are off by default, because both change what the file *is*.

### `--drop-opaque-alpha`

An alpha channel where every pixel is fully opaque stores no information while
costing a quarter of the pixel data. This flag drops it — still MAE 0, still
pixel-identical.

The script checks the actual channel first and refuses to drop one that varies,
so real transparency is never lost. The file does stop being RGBA, so skip this
if something downstream requires four channels.

### `--lossy [COLORS]`

Quantises to a palette (default 256 colours). This **changes pixels**, so the
strict MAE guard would reject every result by design; `--max-lossy-mae`
(default `0.02`) bounds the error instead. A result that comes out *larger* —
easy on small images, where a palette costs more than the pixels it replaces —
is refused.

Measured on a 1355×768 screenshot:

| Mode | Size | Change | MAE |
| --- | --- | --- | --- |
| original | 2624.2KB | — | — |
| default (metadata strip) | 1786.4KB | -32% | 0.0 |
| `--drop-opaque-alpha` | 1611.9KB | -39% | 0.0 |
| `--lossy 256` | 496.0KB | -81% | 0.011 |
| `--lossy 64 --drop-opaque-alpha` | 306.0KB | -88% | 0.019 |

The first three are pixel-identical to the original (verified at zero differing
pixels); the last two are not.

Exit code is `0` on success, `1` if a named path does not exist.

## Sample output

```
would public/logo.png — 16-bit 600.4KB -> 8-bit 219.1KB (-64%, MAE 0.000183, cICP colour tag preserved)
would public/card.png — 8-bit 2624.2KB -> 1786.4KB (-32%, MAE 0.0, 417.4KB metadata stripped)
ok    public/favicon.png — already 8-bit
SKIP  public/photo.png — refusing to write (MAE=0.017, dims ok, alpha ok)

2 file(s) convertible, 2 left alone, 1219.1KB saved.
Dry run — nothing written. Re-run with --write to apply.
```

## What it actually does

For each 16-bit PNG:

1. Re-encode to 8-bit with ImageMagick, stripping metadata, at maximum
   compression.
2. Pin the output colour type to the source's family. Left alone, ImageMagick
   palettises anything that fits in 256 colours, which can band gradients and
   hides alpha-channel loss. Greyscale stays greyscale in both its forms —
   promoting grey+alpha to RGBA would store three identical colour channels and
   leave the file around 30% larger than it needs to be.
3. Carry the colour tag across (see below), or, if there was none, tag the file
   as sRGB explicitly with `sRGB` + `gAMA` chunks.
4. Verify, and only then overwrite.

Files already at 8 bits per channel are normally left untouched — with one
exception. An 8-bit file can still be carrying bulk metadata: C2PA content
credentials (`caBX`), EXIF, embedded thumbnails. None of it affects a single
pixel, and it is often a sizeable share of the file. When stripping would
recover 8KB or more, the file is re-encoded for that reason alone and the
report says so:

```
would test.png — 8-bit 2624.2KB -> 1786.4KB (-32%, MAE 0.0, 417.4KB metadata stripped)
```

Colour-critical chunks (`cICP`, `sRGB`, `gAMA`, `iCCP`, `PLTE`, `tRNS`) are
never counted as strippable — see the colour trap below.

If a re-encode somehow comes out no smaller, it is discarded.

## The safety checks

The output replaces the original only if all of these hold:

- **Mean absolute error ≤ 0.002.** A pure 16→8 bit truncation lands around
  0.0002. Anything meaningfully above that means real pixels changed — an
  unwanted colour conversion, a flattened alpha channel — and the file is left
  alone. A known bad colour transform scores 0.017, eight times the limit.
- **Dimensions unchanged.**
- **Alpha preserved.** Palette PNGs express transparency through a `tRNS` chunk
  rather than an alpha channel, so that case is checked too and does not
  register as a false loss.

A comparison that produces no number at all is reported as `MAE=unreadable`
and treated exactly like an out-of-range one — unverified is not the same as
fine.

Failures print `SKIP` with the reason and move on; they do not abort the run.
That includes a file ImageMagick cannot read: one corrupt PNG costs that file,
not the whole batch.

## The colour trap

Some exports carry a `cICP` chunk tagging the image as Rec.2100 PQ — claiming
to be HDR when it is not. It is an export artifact: the embedded XMP says sRGB,
the artwork was authored in sRGB, and the pixels peak well below where real PQ
content sits.

This matters because **ImageMagick ignores `cICP` while every viewer that shows
the file — Preview, Finder, Chrome, Safari — honours it.** The bytes on disk are
not what anyone sees.

So "preserve the stored pixel values" is the wrong goal. Dropping the tag while
keeping the values measures perfectly (MAE 0.0002, zero differing pixels) and
still collapses rendered gold to a flat olive `#756A4C`. Raw-byte comparison
cannot detect this.

Baking a transform in is no better, because renderers tone-map the tag
differently:

| Renderer | Rendered gold | Luminance |
| --- | --- | --- |
| macOS Preview / Finder / Quick Look | `#835616` | 118.7 |
| Chrome / Safari | `#7E5215` | 115.6 |

A file baked for Chrome and opened in Preview gets tone-mapped a second time
and reads as dull.

**The script therefore copies the `cICP` chunk across verbatim**, splicing it
back in ahead of the first `IDAT` after re-encoding. The output decodes by
exactly the same rules as the input, so the appearance cannot shift in any
renderer. Only the bit depth changes.

> When judging a change, open both files in the **same** viewer. Never compare
> with `magick compare` on the files themselves, and never across two viewers.

## Notes

- Re-run after every artwork export. Most design tools default to 16-bit and
  some re-add the `cICP` tag, silently reintroducing both problems.
- Work happens in a temp directory; the original is overwritten only at the end
  of a successful check. Temp files are indexed by position, so two directories
  can each hold a `logo.png` without clashing.
- If the files are tracked by git, `git checkout --` reverts a run.

## How it works internally

PNG is a flat sequence of chunks — `length(4) + type(4) + data + crc(4)` — so
the script walks them directly with `struct` and `zlib.crc32` rather than
decoding images. That is how it reads the `IHDR` (dimensions, bit depth, colour
type), detects `cICP` and `tRNS`, and splices chunks in at the right place. No
imaging library is needed on the Python side; ImageMagick does the pixel work.

## License

MIT.
