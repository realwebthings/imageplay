#!/usr/bin/env python3
"""Shrink PNGs without changing how they look.

Why this exists
---------------
Most design tools export PNGs at 16 bits per channel -- 8 bytes per pixel,
twice what any logo, icon or social card needs. A 512x514 logo exported this
way can weigh 600KB; at 8-bit it is around 219KB. Same image, same colours,
less than half the bytes. Icons like these are typically referenced as the
apple-touch icon, the PWA icon and the JSON-LD organisation logo all at once,
so every mobile visitor pays for the excess.

Separately, exports pick up bulk metadata -- C2PA content credentials (caBX),
EXIF, embedded thumbnails -- that affects no pixel and can be a third of the
file. An already-8-bit PNG is re-encoded when stripping recovers enough to
justify it.

THE COLOUR TRAP -- read before changing anything here
-----------------------------------------------------
Some exports also carry a ``cICP`` chunk tagging the image Rec.2100 PQ (BT.2020
primaries, PQ transfer) -- i.e. claiming to be HDR. They are not. The embedded
XMP typically says ``photoshop:ICCProfile="sRGB IEC61966-2-1 black scaled"``,
the artwork was authored in sRGB, and the pixels peak around 0.485 luminance
where real PQ content approaches 1.0. The tag is an export artifact.

What matters is that ImageMagick ignores cICP while every viewer that actually
shows the file -- Preview, Finder, Chrome, Safari -- honours it. So the bytes
on disk are NOT what anyone sees, and "preserve the stored values" is the wrong
goal. Dropping the tag while keeping the values measures perfectly (MAE 0.0002,
zero differing pixels) yet collapses rendered gold to a flat olive ``#756A4C``.
Raw-byte comparison cannot detect this.

Baking a transform in is no better, because renderers tone-map the tag
differently:

    macOS Preview / Finder / Quick Look   gold #835616, luminance 118.7
    Chrome / Safari                       gold #7E5215, luminance 115.6

A file baked for Chrome and opened in Preview is tone-mapped a second time and
drops ~3 luminance, which reads as "dull".

So the tag is copied across verbatim instead. The output decodes by exactly the
same rules as the input, nothing can shift in any renderer, and only the bit
depth changes.

Judge any change by opening both files in the SAME viewer, never by
``magick compare`` on the files themselves and never across two viewers.

Portability
-----------
Python 3.10+ (stdlib only) plus ImageMagick. Both version 7 (``magick``) and
version 6 (``convert`` + ``compare``) work; the script detects which is present.

    macOS: brew install imagemagick
    Linux: apt-get install imagemagick   (or dnf/apk equivalent)

Nothing here is platform-specific -- cICP-tagged files are handled by copying
the chunk, not by resolving it through a colour-management tool.

Re-run after exporting new artwork -- most design tools default to 16-bit and
some re-add the cICP tag, silently reintroducing both problems.

Usage
-----
    python3 compress_image.py                          # scan public/
    python3 compress_image.py --write                  # apply
    python3 compress_image.py public/logo.png          # one file
    python3 compress_image.py public app/icons --write # several dirs
    python3 compress_image.py --dir public/icons       # same as above

Files and directories may be mixed freely; with no path given it scans
``public/``. Nothing is written without ``--write``.

Writing somewhere else instead of overwriting:

    python3 compress_image.py logo.png --out build/          # into a directory
    python3 compress_image.py logo.png --out small.png       # to a filename
    python3 compress_image.py public --suffix=-min --write   # logo-min.png

``--out`` is a directory unless it ends in ``.png``, and implies ``--write``.

Given an image and nothing else, the script asks what to make of it:

    python3 compress_image.py logo.png

    Which variant would you like?

      1. all       every variant below, to compare
      2. lossless  8-bit, metadata stripped -- pixel-identical
      3. noalpha   also drop a fully-opaque alpha channel -- pixel-identical
      4. lossy256  quantise to 256 colours -- changes pixels
      5. lossy128  quantise to 128 colours -- changes pixels
      6. lossy64   quantise to 64 colours -- changes pixels

Enter accepts the default (all). The answer is the instruction, so the chosen
variant is written without needing --write as well, and the source is left
alone either way.

Nothing is asked when the command already says what it wants: any of --all,
--lossy, --drop-opaque-alpha, --write or --out answers the question, and a
directory scan never asks at all. Off a terminal -- piped, scripted, in CI --
there is nobody to answer, so every variant is generated.

Generating every variant without being asked:

    python3 compress_image.py logo.png --all --write

writes logo-lossless.png, logo-noalpha.png and logo-lossy256/128/64.png beside
the source and never touches the source itself. Variants that come out larger,
or past the error budget, are refused individually -- one being rejected says
nothing about the rest. --all IS the variant choice, so combining it with
--lossy, --drop-opaque-alpha or --suffix is an error rather than a refinement.

Going further than lossless (both off by default):

    --drop-opaque-alpha   discard an alpha channel that is fully opaque. Still
                          lossless (MAE 0) -- it removes a quarter of the pixel
                          data that stores nothing -- but the file stops being
                          RGBA, so skip it if something downstream needs four
                          channels. A channel that varies is never touched.

    --lossy [COLORS]      quantise to COLORS colours (default 256). This CHANGES
                          PIXELS. The usual guard would reject it by design, so
                          --max-lossy-mae (default 0.02) bounds the damage
                          instead.

On a 1355x768 screenshot: 2624KB lossless-strips to 1786KB, 1612KB with the
alpha dropped, 496KB at --lossy 256, and 306KB at --lossy 64.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

# Largest per-channel deviation accepted between the original and the
# re-encoded file. A pure 16->8 bit truncation lands around 0.0002; anything
# meaningfully above means the re-encode changed real pixels (an unwanted
# colour conversion, a flattened alpha channel) and must not be written. A
# colour-space conversion applied by mistake scores around 0.017 here -- 8x
# over this limit -- so the threshold catches it comfortably.
MAX_MAE = 0.002

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Chunks that carry no pixel or colour meaning and are pure payload: C2PA
# content credentials, EXIF, text records, embedded thumbnails and the like.
# Colour-critical chunks (cICP, sRGB, gAMA, iCCP, PLTE, tRNS) are deliberately
# NOT counted here -- see THE COLOUR TRAP.
CRITICAL_CHUNKS = {"IHDR", "PLTE", "IDAT", "IEND", "tRNS", "cICP", "sRGB", "gAMA", "iCCP"}

# An already-8-bit file is only worth re-encoding if stripping its metadata
# actually recovers something. Below this, the rewrite is not worth the churn.
MIN_METADATA_STRIP = 8 * 1024

# What --all produces, in descending fidelity. The first two are pixel-exact;
# the quantised three trade accuracy for size, so they are named by their
# colour count and the caller picks a rung by looking at the sizes.
# (suffix, drop_alpha, colors)
ALL_VARIANTS = (
    ("-lossless", False, None),
    ("-noalpha", True, None),
    ("-lossy256", False, 256),
    ("-lossy128", False, 128),
    ("-lossy64", False, 64),
)


class Ihdr:
    __slots__ = ("width", "height", "bit_depth", "color_type")

    def __init__(self, width: int, height: int, bit_depth: int, color_type: int):
        self.width = width
        self.height = height
        self.bit_depth = bit_depth
        self.color_type = color_type

    @property
    def has_alpha(self) -> bool:
        return bool(self.color_type & 4)


def iter_chunks(data: bytes):
    """Yield (type, offset, length) for each PNG chunk.

    Every chunk is length(4) + type(4) + data + crc(4), so this is exact for
    any valid PNG and needs no image decoder.
    """
    i = 8
    while i + 8 <= len(data):
        length = struct.unpack(">I", data[i : i + 4])[0]
        ctype = data[i + 4 : i + 8].decode("latin1", "replace")
        yield ctype, i, length
        i += 12 + length


def read_ihdr(path: str) -> Ihdr | None:
    try:
        with open(path, "rb") as fh:
            head = fh.read(26)
    except OSError:
        return None
    if len(head) < 26 or not head.startswith(PNG_SIGNATURE):
        return None
    width, height = struct.unpack(">II", head[16:24])
    return Ihdr(width, height, head[24], head[25])


def has_chunk(path: str, name: str) -> bool:
    with open(path, "rb") as fh:
        data = fh.read()
    return any(ctype == name for ctype, _, _ in iter_chunks(data))


def ancillary_bytes(path: str) -> int:
    """Total size of chunks that hold no pixel or colour information.

    This is what ``-strip`` would reclaim, counted before doing any work so a
    file with nothing to gain is never rewritten.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    return sum(
        length + 12 for ctype, _, length in iter_chunks(data) if ctype not in CRITICAL_CHUNKS
    )


def png_chunk(ctype: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(ctype + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", crc)


def tag_srgb(path: str) -> None:
    """Declare sRGB explicitly rather than relying on "untagged means sRGB".

    sRGB carries a rendering intent (0 = perceptual); gAMA 45455 is its
    standard companion, so older viewers that ignore sRGB still get the right
    gamma. The spec requires both to precede PLTE as well as IDAT, so a
    palette image is spliced at whichever comes first.
    """
    with open(path, "rb") as fh:
        src = fh.read()

    srgb = png_chunk(b"sRGB", bytes([0]))
    gama = png_chunk(b"gAMA", struct.pack(">I", 45455))

    parts = [src[:8]]
    inserted = False
    for ctype, offset, length in iter_chunks(src):
        if ctype in ("PLTE", "IDAT") and not inserted:
            parts.append(srgb)
            parts.append(gama)
            inserted = True
        parts.append(src[offset : offset + 12 + length])

    with open(path, "wb") as fh:
        fh.write(b"".join(parts))


def alpha_is_fully_opaque(binary: str, path: str) -> bool:
    """True when every alpha sample is maximum, i.e. the channel stores nothing.

    Such a channel is a quarter of the pixel data carrying no information, so
    dropping it is lossless. A channel that varies at all must be kept.
    """
    cmd = [binary] if binary == "magick" else ["convert"]
    cmd += [path, "-alpha", "extract", "-format", "%[min] %[max]", "info:"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return False
    parts = proc.stdout.split()
    if len(parts) != 2:
        return False
    try:
        low, high = float(parts[0]), float(parts[1])
    except ValueError:
        return False
    # ImageMagick reports either 0..1 or 0..QuantumRange depending on build,
    # so "opaque" is judged as min == max at the top of whichever scale.
    return low == high and high in (1.0, 255.0, 65535.0)


def require_imagemagick() -> str:
    """Return the ImageMagick binary name, or exit with install instructions.

    ImageMagick 7 ships `magick`; version 6 (still common on Linux) ships
    `convert` and `compare` as separate binaries.
    """
    if shutil.which("magick"):
        return "magick"
    if shutil.which("convert") and shutil.which("compare"):
        return "convert"
    sys.stderr.write(
        "ImageMagick not found. Install it with:\n"
        "  macOS: brew install imagemagick\n"
        "  Linux: apt-get install imagemagick\n"
    )
    raise SystemExit(1)


def extract_chunk(path: str, name: str) -> bytes | None:
    """Return a full chunk (length+type+data+crc) verbatim, or None."""
    with open(path, "rb") as fh:
        data = fh.read()
    for ctype, offset, length in iter_chunks(data):
        if ctype == name:
            return data[offset : offset + 12 + length]
    return None


def insert_before_idat(path: str, chunk: bytes) -> None:
    """Splice a pre-built chunk in ahead of the first PLTE or IDAT."""
    with open(path, "rb") as fh:
        src = fh.read()
    parts = [src[:8]]
    inserted = False
    for ctype, offset, length in iter_chunks(src):
        if ctype in ("PLTE", "IDAT") and not inserted:
            parts.append(chunk)
            inserted = True
        parts.append(src[offset : offset + 12 + length])
    with open(path, "wb") as fh:
        fh.write(b"".join(parts))


def run_convert(binary: str, argv: list[str]) -> str | None:
    """Run the converter. Return None on success, or the error text.

    A single unreadable or unsupported file should cost that file, not the
    whole run, so the failure is returned for the caller to report rather
    than raised.
    """
    cmd = [binary] + argv if binary == "magick" else ["convert"] + argv
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        return None
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return detail[-1] if detail else f"exit status {proc.returncode}"


def mean_absolute_error(binary: str, a: str, b: str) -> float:
    """Mean absolute error, normalised 0..1.

    `magick compare` reports the metric on STDERR, and exits 0 on a perfect
    match but non-zero as soon as the images differ at all -- so both streams
    are read and a non-zero exit is not treated as failure.
    """
    cmd = (
        [binary, "compare", "-metric", "MAE", a, b, "null:"]
        if binary == "magick"
        else ["compare", "-metric", "MAE", a, b, "null:"]
    )
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = f"{proc.stderr or ''}{proc.stdout or ''}".strip()
    match = re.search(r"\(([\d.eE+-]+)\)", out)
    return float(match.group(1)) if match else float("nan")


def destination_for(path: str, out: str | None, suffix: str | None, many: bool) -> str:
    """Where the result for `path` should land.

    With neither --out nor --suffix this is `path` itself, i.e. in-place.

    --out is a directory unless it ends in .png: guessing from "does this path
    already exist" makes the same command do different things depending on the
    state of the disk, and silently turns `--out build` into a *file* called
    build the first time it is run. The extension is the one signal the caller
    actually controls.
    """
    name = os.path.basename(path)
    if suffix:
        stem, ext = os.path.splitext(name)
        name = f"{stem}{suffix}{ext}"

    if not out:
        return os.path.join(os.path.dirname(path), name)

    if out.lower().endswith(".png") and not out.endswith(os.sep):
        if many:
            # Several inputs cannot share one filename; they would overwrite
            # each other in turn and only the last would survive.
            raise SystemExit(
                "--out names a single .png file but several inputs were given; "
                "pass a directory instead"
            )
        return out
    return os.path.join(out, name)


def kb(num_bytes: int) -> str:
    return f"{num_bytes / 1024:.1f}KB"


def process(
    path: str,
    binary: str,
    tmp_dir: str,
    index: int,
    opts,
    suffix: str | None,
    want_drop_alpha: bool,
    colors: int | None,
    many: bool,
) -> int | None:
    """Produce one variant of one file.

    Returns the bytes saved, or None when the file was left alone -- so the
    caller counts outcomes without this needing to know about totals.
    """
    ihdr = read_ihdr(path)
    if ihdr is None:
        print(f"skip  {path} — not a readable PNG")
        return None

    has_cicp = has_chunk(path, "cICP")
    # An 8-bit file has no depth left to give, but it can still be
    # carrying bulk metadata -- C2PA content credentials (caBX), EXIF,
    # embedded thumbnails -- which is often a sizeable share of the
    # file and none of it affects a single pixel. Re-encoding such a
    # file is still worth it; one with nothing to strip is not.
    metadata_bytes = ancillary_bytes(path)
    # --lossy and --drop-opaque-alpha both have work to do on a file
    # that is already 8-bit and carries no metadata, so they suppress
    # the early exit.
    wants_recode = colors is not None or want_drop_alpha
    if ihdr.bit_depth <= 8 and metadata_bytes < MIN_METADATA_STRIP and not wants_recode:
        print(f"ok    {path} — already {ihdr.bit_depth}-bit")
        return None

    before = os.path.getsize(path)
    # Indexed by source position and variant, not by basename: two directories
    # can each hold a "logo.png", and --all runs five variants over one source.
    # Neither may write through another's temp file.
    out = os.path.join(tmp_dir, f"{index}{suffix or ''}-{os.path.basename(path)}")

    # The colour type is pinned to the source's family because
    # ImageMagick otherwise palettises anything fitting in 256 colours,
    # which both defeats the alpha check below and risks banding.
    # Greyscale is kept greyscale in both its forms: promoting type 4
    # to RGBA stores three identical colour channels and can leave the
    # output ~30% larger than it needs to be.
    #   0 = grey, 2 = RGB, 3 = palette, 4 = grey+alpha, 6 = RGBA
    if ihdr.color_type in (0, 4):
        png_color_type = ihdr.color_type
    elif ihdr.has_alpha:
        png_color_type = 6
    else:
        png_color_type = 2

    # Dropping alpha is only lossless when the channel is uniformly
    # opaque, so the file is inspected rather than trusted. A channel
    # that varies carries real transparency and is left alone.
    drop_alpha = (
        want_drop_alpha and ihdr.has_alpha and alpha_is_fully_opaque(binary, path)
    )
    if drop_alpha:
        # grey+alpha (4) degrades to grey (0), RGBA (6) to RGB (2).
        png_color_type = 0 if ihdr.color_type == 4 else 2

    # Carry a cICP tag across verbatim rather than resolving it. The
    # tag is what every viewer uses to decide how to paint these
    # pixels; baking a transform in and dropping the tag makes the file
    # correct in one renderer and wrong in the rest -- see THE COLOUR
    # TRAP. Copying it means the output decodes by the same rules as
    # the input, so nothing can shift anywhere.
    cicp_chunk = extract_chunk(path, "cICP") if has_cicp else None

    argv = [path, "-depth", "8", "-strip"]
    if drop_alpha:
        argv.append("-alpha")
        argv.append("off")
    if colors is not None:
        # Quantisation picks its own palette, so the colour type is
        # left to ImageMagick here rather than pinned.
        argv += ["-colors", str(colors)]
    else:
        argv += ["-define", f"png:color-type={png_color_type}"]
    argv += ["-define", "png:compression-level=9", out]

    error = run_convert(binary, argv)
    if error is not None:
        print(f"SKIP  {path} — convert failed: {error}")
        return None
    if cicp_chunk is not None:
        insert_before_idat(out, cicp_chunk)
    else:
        tag_srgb(out)

    after = os.path.getsize(out)
    # Straight comparison against the source: only the bit depth may
    # change. Anything beyond rounding means a colour transform crept in.
    mae = mean_absolute_error(binary, path, out)
    out_ihdr = read_ihdr(out)

    dims_match = (
        out_ihdr is not None
        and out_ihdr.width == ihdr.width
        and out_ihdr.height == ihdr.height
    )
    # Palette PNGs (colour-type 3) express transparency through a tRNS
    # chunk rather than an alpha channel, so testing color_type & 4
    # alone would report a false loss for them.
    out_has_alpha = out_ihdr is not None and (
        out_ihdr.has_alpha or (out_ihdr.color_type == 3 and has_chunk(out, "tRNS"))
    )
    # Alpha loss is a failure unless it was the whole point, and it
    # was only requested after proving the channel held nothing.
    # Quantisation is the awkward case: it emits a palette PNG, which
    # drops a uniformly-opaque alpha channel as a side effect. That
    # loses no information, so it is judged by whether the source
    # alpha actually held any, not by the output's colour type.
    alpha_kept = (not ihdr.has_alpha) or out_has_alpha or drop_alpha
    if not alpha_kept and colors is not None:
        alpha_kept = alpha_is_fully_opaque(binary, path)

    # --lossy changes pixels deliberately, so the tight guard would
    # reject every result by design; its own budget applies instead.
    limit = opts.max_lossy_mae if colors is not None else MAX_MAE

    # mae != mae detects NaN: the comparison did not produce a
    # number at all, so the result is unverified and must not be
    # trusted any more than an out-of-range one.
    if not dims_match or not alpha_kept or not (mae == mae) or mae > limit:
        reported = "unreadable" if mae != mae else mae
        which = f" [{suffix.lstrip('-')}]" if suffix else ""
        print(
            f"SKIP  {path}{which} — refusing to write (MAE={reported} > {limit}, "
            f"dims {'ok' if dims_match else 'CHANGED'}, "
            f"alpha {'ok' if alpha_kept else 'LOST'})"
        )
        return None

    dest = destination_for(path, opts.out, suffix, many)
    writing_elsewhere = os.path.normpath(dest) != os.path.normpath(path)

    # A bigger result is never worth keeping, wherever it was going to
    # land -- quantising a small image can easily inflate it, because a
    # palette plus tRNS costs more than the pixels it replaces. The one
    # exception stays a cICP rewrite, which exists to fix the tag
    # rather than to save bytes.
    if after >= before and not has_cicp:
        # Name the variant: --all prints one line per variant, and three
        # bare "result is larger" lines against one source say nothing about
        # which attempts were refused.
        which = f" [{suffix.lstrip('-')}]" if suffix else ""
        print(
            f"ok    {path}{which} — result is larger ({kb(before)} -> {kb(after)}), "
            "leaving as is"
        )
        return None

    # Signed explicitly: a cICP rewrite is allowed to grow, and
    # "-{-190}%" would otherwise print as "--190%".
    pct = (before - after) / before * 100
    pct_text = f"-{pct:.0f}%" if pct >= 0 else f"+{-pct:.0f}%"
    notes = []
    if has_cicp:
        notes.append("cICP colour tag preserved")
    if metadata_bytes >= MIN_METADATA_STRIP:
        notes.append(f"{kb(metadata_bytes)} metadata stripped")
    if drop_alpha:
        notes.append("opaque alpha dropped")
    if colors is not None:
        notes.append(f"quantised to {colors} colours")
    note = (", " + ", ".join(notes)) if notes else ""
    verb = "WROTE" if opts.write else "would"
    # An already-8-bit file is being rewritten for its metadata alone,
    # so describing it as a depth change would be a lie.
    change = (
        f"{ihdr.bit_depth}-bit {kb(before)} -> 8-bit {kb(after)}"
        if ihdr.bit_depth > 8
        else f"8-bit {kb(before)} -> {kb(after)}"
    )
    arrow = f" -> {dest}" if writing_elsewhere else ""
    print(f"{verb} {path}{arrow} — {change} ({pct_text}, MAE {mae}{note})")

    if opts.write:
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.copyfile(out, dest)
    return before - after


VARIANT_MENU = (
    ("all", "every variant below, to compare"),
    ("lossless", "8-bit, metadata stripped -- pixel-identical"),
    ("noalpha", "also drop a fully-opaque alpha channel -- pixel-identical"),
    ("lossy256", "quantise to 256 colours -- changes pixels"),
    ("lossy128", "quantise to 128 colours -- changes pixels"),
    ("lossy64", "quantise to 64 colours -- changes pixels"),
)


def ask_variant() -> str | None:
    """Ask which variant to generate. Returns a VARIANT_MENU key, or None.

    Only ever called on a terminal -- a piped or scripted run must not block
    waiting for an answer nobody is there to give.
    """
    print("Which variant would you like?\n")
    for number, (key, blurb) in enumerate(VARIANT_MENU, 1):
        print(f"  {number}. {key:<9} {blurb}")
    print()
    try:
        reply = input("Choose 1-6 [1]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not reply:
        return "all"
    if reply.isdigit() and 1 <= int(reply) <= len(VARIANT_MENU):
        return VARIANT_MENU[int(reply) - 1][0]
    # Accept the name as readily as the number; it is what the menu shows.
    for key, _ in VARIANT_MENU:
        if reply.lower() == key:
            return key
    print(f"'{reply}' is not one of the choices.")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-encode 16-bit PNGs to 8-bit and strip bogus HDR tags."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="PNG files and/or directories to process (default: the --dir value)",
    )
    parser.add_argument(
        "--write", action="store_true", help="re-encode in place (default: report only)"
    )
    parser.add_argument("--dir", default="public", help="directory to scan (default: public)")
    parser.add_argument(
        "--out",
        metavar="DEST",
        help=(
            "write results to DEST instead of overwriting the originals. "
            "DEST is a directory unless it ends in .png, in which case it "
            "names the output file (one input only). Implies --write."
        ),
    )
    parser.add_argument(
        "--suffix",
        metavar="TEXT",
        help='append TEXT to each output name, e.g. --suffix "-min" writes logo-min.png',
    )
    parser.add_argument(
        "--drop-opaque-alpha",
        action="store_true",
        help=(
            "discard the alpha channel when every pixel in it is fully opaque. "
            "Lossless (MAE 0) and saves ~25%% of the pixel data, but the file "
            "stops being RGBA -- skip it if something downstream requires 4 channels"
        ),
    )
    parser.add_argument(
        "--lossy",
        nargs="?",
        type=int,
        const=256,
        metavar="COLORS",
        help=(
            "quantise to COLORS colours (default 256). This CHANGES PIXELS and "
            "is not covered by the usual MAE guard; --max-lossy-mae bounds it instead"
        ),
    )
    parser.add_argument(
        "--max-lossy-mae",
        type=float,
        default=0.02,
        metavar="MAE",
        help="reject a --lossy result above this error (default: 0.02)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "generate every variant as a separate file -- lossless, alpha-dropped "
            "and quantised at 256/128/64 colours -- so the sizes can be compared "
            "side by side. Never overwrites the original"
        ),
    )
    opts = parser.parse_args()

    if opts.lossy is not None and opts.lossy < 2:
        parser.error("--lossy needs at least 2 colours")

    # --all IS the variant selection, so a flag that also selects one is a
    # contradiction rather than a refinement. Failing beats silently ignoring.
    if opts.all:
        conflicting = [
            name
            for name, value in (
                ("--lossy", opts.lossy is not None),
                ("--drop-opaque-alpha", opts.drop_opaque_alpha),
                ("--suffix", bool(opts.suffix)),
            )
            if value
        ]
        if conflicting:
            parser.error(
                f"--all already generates every variant; drop {', '.join(conflicting)} "
                "or ask for that one variant on its own"
            )

    # Given an image and no word on what to make of it, there is a choice to
    # offer rather than a default to assume. On a terminal, ask. Anywhere else
    # -- a pipe, a script, CI -- nobody is there to answer, so generate
    # everything and let the caller pick from the sizes.
    # --write already says what to do -- re-encode this file in place -- so it
    # is an instruction, not an open question. Prompting there would change
    # what an existing command does: it would write a new file instead of the
    # in-place compression the caller asked for.
    chose_variant = (
        opts.all
        or opts.lossy is not None
        or opts.drop_opaque_alpha
        or opts.write
        or opts.out
    )
    if opts.paths and not chose_variant and not opts.suffix:
        if sys.stdin.isatty() and sys.stdout.isatty():
            choice = ask_variant()
            if choice is None:
                return 1
        else:
            choice = "all"

        if choice == "all":
            opts.all = True
        elif choice == "noalpha":
            opts.drop_opaque_alpha = True
            opts.suffix = opts.suffix or "-noalpha"
        elif choice.startswith("lossy"):
            opts.lossy = int(choice.removeprefix("lossy"))
            opts.suffix = opts.suffix or f"-{choice}"
        else:
            opts.suffix = opts.suffix or "-lossless"

        # The answer is the instruction; making them repeat it as --write
        # would be asking the same question twice.
        opts.write = True

    # Five variants cannot share one filename, so --out must be a directory here.
    if opts.all and opts.out and opts.out.lower().endswith(".png"):
        parser.error("--all writes several files, so --out must be a directory")

    # --out is an explicit destination, so asking for it means asking to write.
    if opts.out:
        opts.write = True

    binary = require_imagemagick()

    # Accept either form: a bare list of files/directories, or --dir. Passing a
    # single file is the obvious thing to reach for, so it must not be an error.
    targets = opts.paths if opts.paths else [opts.dir]

    pngs: list[str] = []
    missing = False
    for target in targets:
        if os.path.isdir(target):
            pngs.extend(
                os.path.join(target, name)
                for name in os.listdir(target)
                if name.lower().endswith(".png")
            )
        elif os.path.isfile(target):
            # Named explicitly, so process it whatever the extension says --
            # read_ihdr() rejects anything that is not really a PNG.
            pngs.append(target)
        else:
            sys.stderr.write(f"No such file or directory: {target}\n")
            missing = True

    if missing:
        return 1

    # De-duplicate while keeping a stable order (a file may be named twice, or
    # named alongside the directory that contains it).
    pngs = sorted(dict.fromkeys(os.path.normpath(p) for p in pngs))

    # A previous --suffix run leaves its output next to the source, so a later
    # scan of the same directory would re-compress those copies into
    # logo-min-min.png. Files named explicitly are still honoured.
    if opts.suffix and not opts.paths:
        stem_suffix = opts.suffix
        pngs = [f for f in pngs if not os.path.splitext(f)[0].endswith(stem_suffix)]

    if not pngs:
        print(f"No PNGs found in {', '.join(targets)}")
        return 0

    tmp_dir = tempfile.mkdtemp(prefix="compress-images-")
    saved_total = 0
    converted = 0
    skipped = 0

    try:
        for index, path in enumerate(pngs):
            if opts.all:
                # One source, several outputs: each variant is an independent
                # attempt, and one being refused says nothing about the rest.
                produced = 0
                best_saved = 0
                for suffix, drop_alpha, colors in ALL_VARIANTS:
                    saved = process(
                        path, binary, tmp_dir, index, opts,
                        suffix, drop_alpha, colors, many=True,
                    )
                    if saved is not None:
                        produced += 1
                        # These variants are alternatives to each other, not
                        # cumulative: adding their savings would claim to have
                        # recovered several times the source's own size.
                        best_saved = max(best_saved, saved)
                if produced:
                    converted += produced
                    saved_total += best_saved
                else:
                    skipped += 1
                continue

            saved = process(
                path, binary, tmp_dir, index, opts,
                opts.suffix, opts.drop_opaque_alpha, opts.lossy, many=len(pngs) > 1,
            )
            if saved is None:
                skipped += 1
            else:
                saved_total += saved
                converted += 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    verb = "written" if opts.write else "convertible"
    if opts.all:
        # "saved" counts the best variant per source, since only one of them
        # would ever ship.
        print(
            f"\n{converted} variant(s) {verb}, {skipped} source(s) left alone, "
            f"up to {kb(saved_total)} saved by the smallest."
        )
    else:
        verb = "re-encoded" if opts.write else "convertible"
        print(
            f"\n{converted} file(s) {verb}, {skipped} left alone, {kb(saved_total)} saved."
        )
    if not opts.write and converted:
        print("Dry run — nothing written. Re-run with --write to apply.")
        if not (opts.out or opts.suffix):
            print("If these files are tracked by git, `git checkout --` reverts a run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
