"""
Render a still image + FOCtave funscripts into an MP4 with animated
electrode-glow overlays.

Each frame:
  - base image, brightness modulated by the volume channel
  - bloom (pre-blurred copy, screened on top, intensity modulated by volume)
  - 4 radial glows at the clicked electrode positions, radius and brightness
    driven by e1-e4 channel values
  - arcs between every pair of electrodes, brightness = geometric mean of
    the two endpoint intensities (visualises foc-stim's any-to-any current
    routing)

Usage (after running foctave.py and place.py):

    python render.py path/to/image.jpg

Defaults: looks for <image_stem>.electrodes.json for positions, the 5
funscripts next to the image, and an audio file matching the funscript
stem. Override any of them with flags.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG = "ffmpeg"


CHANNELS = ["e1", "e2", "e3", "e4", "volume"]
ELECTRODE_CHANNELS = ["e1", "e2", "e3", "e4"]
# Electrodes are visualised as a single polyline in order e1 -> e2 -> e3 -> e4,
# matching the "snake head -> necktie -> snake belly -> snake tail" mental model
# for a typical longitudinal 4-electrode placement.


def load_funscript(path: Path) -> tuple[np.ndarray, np.ndarray]:
    d = json.loads(path.read_text(encoding="utf-8"))
    t = np.array([a["at"] for a in d["actions"]], dtype=np.float64)
    p = np.array([a["pos"] for a in d["actions"]], dtype=np.float64)
    return t, p


def find_funscript(stem_dir: Path, stem: str, ch: str) -> Path:
    candidates = [
        stem_dir / f"{stem}.{ch}.funscript",
        stem_dir.parent / f"{stem}.{ch}.funscript",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(f"Cannot find {stem}.{ch}.funscript near {stem_dir}")


def find_audio(stem_dir: Path, stem: str) -> Path | None:
    for ext in (".wav", ".flac", ".mp3", ".m4a", ".ogg"):
        for candidate in (stem_dir / f"{stem}{ext}", stem_dir.parent / f"{stem}{ext}"):
            if candidate.exists():
                return candidate
    return None


def precompute_glow_stamp(max_radius: int) -> np.ndarray:
    """Generate a radial falloff stamp (float32, [0,1]) for an electrode glow.
    Composited by scaling this stamp's size and brightness per frame."""
    size = max_radius * 2 + 1
    yy, xx = np.mgrid[-max_radius:max_radius + 1, -max_radius:max_radius + 1]
    dist = np.sqrt(xx * xx + yy * yy) / max_radius
    # soft falloff: (1 - d^2)^2 for inside, 0 outside
    stamp = np.clip(1.0 - dist * dist, 0, 1) ** 2
    return stamp.astype(np.float32)


def stamp_glow(canvas: np.ndarray, cx: int, cy: int, stamp: np.ndarray,
               radius_scale: float, color: tuple[float, float, float],
               brightness: float) -> None:
    """Additive-blend a scaled electrode glow into canvas (float32 HxWx3)."""
    h, w = canvas.shape[:2]
    sh, sw = stamp.shape
    r_src = sh // 2
    # Effective radius for this frame
    r_eff = max(1, int(r_src * radius_scale))
    if r_eff < 1:
        return
    # Resample stamp to effective size using numpy (fast enough; for MVP fine)
    # Cheap nearest-neighbour via striding:
    idx = (np.linspace(0, sh - 1, r_eff * 2 + 1)).astype(np.int32)
    stamp_r = stamp[idx][:, idx]  # shape (2r+1, 2r+1)

    r = r_eff
    x0, x1 = cx - r, cx + r + 1
    y0, y1 = cy - r, cy + r + 1

    sx0 = max(0, -x0); sx1 = stamp_r.shape[1] - max(0, x1 - w)
    sy0 = max(0, -y0); sy1 = stamp_r.shape[0] - max(0, y1 - h)
    dx0 = max(0, x0); dx1 = min(w, x1)
    dy0 = max(0, y0); dy1 = min(h, y1)
    if dx0 >= dx1 or dy0 >= dy1:
        return

    patch = stamp_r[sy0:sy1, sx0:sx1] * brightness
    for c in range(3):
        canvas[dy0:dy1, dx0:dx1, c] += patch * color[c]


def build_path(electrodes_ordered: list[tuple[int, int]],
               spacing_px: float = 2.0) -> tuple[np.ndarray, np.ndarray]:
    """Sample the polyline e1 -> e2 -> e3 -> e4 at ~every `spacing_px` and
    return (xy int array shape (N,2), barycentric weights shape (N,4)).

    Each path point's weight vector has two non-zero entries: its position
    along the current segment contributes to the two adjacent electrodes.
    A point halfway between e2 and e3 gets weights (0, 0.5, 0.5, 0), so its
    local intensity = 0.5 * e2_val + 0.5 * e3_val - the "signal between
    the points" behaviour."""
    pts = np.array(electrodes_ordered, dtype=np.float32)  # (4, 2)
    n_electrodes = len(pts)
    xys = []
    weights = []
    for i in range(n_electrodes - 1):
        a, b = pts[i], pts[i + 1]
        seg_len = float(np.linalg.norm(b - a))
        n_samples = max(2, int(round(seg_len / spacing_px)))
        for j in range(n_samples):
            t = j / n_samples
            pt = a + (b - a) * t
            w = np.zeros(n_electrodes, dtype=np.float32)
            w[i] = 1.0 - t
            w[i + 1] = t
            xys.append(pt)
            weights.append(w)
    # Final endpoint
    xys.append(pts[-1])
    final_w = np.zeros(n_electrodes, dtype=np.float32)
    final_w[-1] = 1.0
    weights.append(final_w)
    return np.array(xys).astype(np.int32), np.array(weights)


def draw_path_ribbon(canvas: np.ndarray, path_xys: np.ndarray,
                     path_intensities: np.ndarray, color: tuple[float, float, float],
                     stamp: np.ndarray, thickness_scale: float) -> None:
    """Stamp a small radial glow at each path point with brightness scaled
    by that point's local intensity. Overlapping stamps add up, yielding a
    continuous ribbon whose per-pixel brightness matches the interpolated
    electrode values."""
    h, w = canvas.shape[:2]
    sh = stamp.shape[0]
    r_src = sh // 2

    r_eff = max(2, int(r_src * thickness_scale))
    idx = np.linspace(0, sh - 1, r_eff * 2 + 1).astype(np.int32)
    stamp_small = stamp[idx][:, idx]  # resampled stamp (2r+1, 2r+1)
    r = r_eff

    for (x, y), intensity in zip(path_xys, path_intensities):
        if intensity <= 0.05:
            continue
        x, y = int(x), int(y)
        x0, x1 = x - r, x + r + 1
        y0, y1 = y - r, y + r + 1
        sx0 = max(0, -x0); sx1 = stamp_small.shape[1] - max(0, x1 - w)
        sy0 = max(0, -y0); sy1 = stamp_small.shape[0] - max(0, y1 - h)
        dx0 = max(0, x0); dx1 = min(w, x1)
        dy0 = max(0, y0); dy1 = min(h, y1)
        if dx0 >= dx1 or dy0 >= dy1:
            continue
        patch = stamp_small[sy0:sy1, sx0:sx1] * (intensity * 140.0)
        for c in range(3):
            canvas[dy0:dy1, dx0:dx1, c] += patch * color[c]


def render(
    image_path: Path,
    electrodes: dict,
    funscripts: dict,
    audio: Path | None,
    output: Path,
    fps: int,
    max_dim: int,
    duration_s: float | None,
    bloom_strength: float,
    base_dim_range: tuple[float, float],
) -> None:
    base = Image.open(image_path).convert("RGB")
    iw, ih = base.size

    # Downscale for render if needed
    if max(iw, ih) > max_dim:
        sf = max_dim / max(iw, ih)
        nw, nh = int(iw * sf), int(ih * sf)
        # Pad to even dimensions for libx264
        nw -= nw % 2
        nh -= nh % 2
        base = base.resize((nw, nh), Image.LANCZOS)
        # Scale electrode positions
        electrodes = {k: (int(v[0] * sf), int(v[1] * sf)) for k, v in electrodes.items()}
    else:
        sf = 1.0
        nw = iw - (iw % 2)
        nh = ih - (ih % 2)
        if (nw, nh) != (iw, ih):
            base = base.crop((0, 0, nw, nh))

    w, h = nw, nh
    print(f"Render size: {w}x{h} @ {fps}fps")

    # Pre-compute: base as float, pre-blurred bloom copy
    base_arr = np.array(base, dtype=np.float32)  # HxWx3 in [0,255]
    blur_radius = max(6, min(w, h) * 0.025)
    print(f"Pre-blurring bloom base (radius={blur_radius:.1f}px)...")
    bloom_arr = np.array(base.filter(ImageFilter.GaussianBlur(radius=blur_radius)),
                         dtype=np.float32)

    # Electrode glow stamp (pre-computed once) - bigger for anchor points
    electrode_max_radius = int(min(w, h) * 0.18)
    stamp = precompute_glow_stamp(electrode_max_radius)

    # Smaller stamp for the ribbon
    ribbon_stamp = precompute_glow_stamp(int(min(w, h) * 0.035))

    # Precompute the polyline through e1 -> e2 -> e3 -> e4
    electrodes_ordered = [electrodes[ch] for ch in ELECTRODE_CHANNELS]
    path_xys, path_weights = build_path(electrodes_ordered, spacing_px=2.0)
    # Parameter along path, 0..1, for traveling-wave modulation
    path_t = np.linspace(0.0, 1.0, len(path_xys), dtype=np.float32)

    # Duration
    total_ms = max(fs[0][-1] for fs in funscripts.values())
    if duration_s is not None:
        total_ms = min(total_ms, duration_s * 1000)
    n_frames = int(total_ms / 1000 * fps)
    print(f"Rendering {n_frames} frames ({total_ms/1000:.1f}s)")

    # ffmpeg pipe
    cmd = [FFMPEG, "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{w}x{h}", "-r", str(fps), "-i", "-"]
    if audio and audio.exists():
        cmd += ["-i", str(audio)]
    cmd += ["-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p", "-crf", "20"]
    if audio and audio.exists():
        cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
    if duration_s is not None:
        cmd += ["-t", str(duration_s)]
    cmd += [str(output)]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    electrode_colors = {
        "e1": (1.00, 0.35, 0.25),  # red-orange
        "e2": (1.00, 0.55, 0.20),  # orange
        "e3": (0.35, 1.00, 0.55),  # green-cyan
        "e4": (0.30, 0.70, 1.00),  # blue-cyan
    }
    ribbon_color = (1.0, 0.70, 0.30)  # warm amber for the flowing path

    import time
    t_start = time.perf_counter()

    try:
        for i in range(n_frames):
            t_ms = i * 1000 / fps
            vals = {}
            for ch in CHANNELS:
                times, positions = funscripts[ch]
                vals[ch] = float(np.interp(t_ms, times, positions)) / 100.0

            vol = vals["volume"]

            # Base dimming + bloom
            dim_lo, dim_hi = base_dim_range
            dim = dim_lo + (dim_hi - dim_lo) * vol
            canvas = base_arr * dim + bloom_arr * (bloom_strength * vol)

            # Flowing ribbon along the polyline e1->e2->e3->e4.
            # Interpolated intensity at every path point, modulated by a
            # traveling wave so the signal visibly flows.
            e_values = np.array([vals["e1"], vals["e2"], vals["e3"], vals["e4"]],
                                dtype=np.float32)
            path_intensity = path_weights @ e_values  # (N,)
            # Traveling wave: phase advances with time, 2 full wavelengths
            # along the path; modulates intensity by 60-100%.
            t_s = t_ms / 1000.0
            wave = 0.60 + 0.40 * np.sin(2 * np.pi * (path_t * 2.0 - t_s * 1.2))
            path_intensity = path_intensity * wave
            # Ribbon thickness subtly breathes with overall volume
            ribbon_thickness = 0.55 + 0.45 * vol
            draw_path_ribbon(canvas, path_xys, path_intensity, ribbon_color,
                             ribbon_stamp, ribbon_thickness)

            # Electrode radial glows
            for ch in ELECTRODE_CHANNELS:
                intensity = vals[ch]
                if intensity <= 0.02:
                    continue
                cx, cy = electrodes[ch]
                # Radius scales up with intensity; brightness too
                r_scale = 0.35 + 0.65 * intensity
                brightness = 180 * intensity
                stamp_glow(canvas, cx, cy, stamp, r_scale,
                           electrode_colors[ch], brightness)

            # Clip and write
            frame = np.clip(canvas, 0, 255).astype(np.uint8)
            proc.stdin.write(frame.tobytes())

            if i % fps == 0 and i > 0:
                elapsed = time.perf_counter() - t_start
                fps_now = i / elapsed
                eta = (n_frames - i) / max(fps_now, 0.01)
                print(f"\r  frame {i}/{n_frames}  "
                      f"({100*i/n_frames:5.1f}%)  "
                      f"{fps_now:.1f}fps  ETA {eta:.0f}s", end="", flush=True)
        print()
    finally:
        proc.stdin.close()
        proc.wait()

    elapsed = time.perf_counter() - t_start
    print(f"Wrote {output}  ({elapsed:.1f}s render)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", type=Path, help="Background still image")
    ap.add_argument("--electrodes", type=Path, default=None,
                    help="Path to electrodes JSON (default: <image>.electrodes.json)")
    ap.add_argument("--funscripts-stem", type=Path, default=None,
                    help="Stem path for funscripts (default: same as image stem)")
    ap.add_argument("--audio", type=Path, default=None,
                    help="Audio file to embed (default: <stem>.wav/.mp3/...)")
    ap.add_argument("--output", type=Path, default=None,
                    help="Output MP4 path (default: <image_stem>.mp4)")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--max-dim", type=int, default=1280,
                    help="Cap output resolution (default 1280 — good speed/quality balance)")
    ap.add_argument("--duration", type=float, default=None,
                    help="Cap render duration in seconds (useful for previews)")
    ap.add_argument("--bloom", type=float, default=0.45,
                    help="Bloom strength 0-1 (default 0.45)")
    ap.add_argument("--min-dim", type=float, default=0.55,
                    help="Base brightness at silence 0-1 (default 0.55)")
    ap.add_argument("--max-base", type=float, default=1.0,
                    help="Base brightness at peak volume 0-1 (default 1.0)")
    args = ap.parse_args()

    image_path = args.image.resolve()
    if not image_path.exists():
        print(f"Not found: {image_path}", file=sys.stderr)
        return 1

    electrodes_path = (args.electrodes or image_path.with_suffix(".electrodes.json")).resolve()
    if not electrodes_path.exists():
        print(f"No electrodes file at {electrodes_path}.\nRun: python place.py \"{image_path}\"",
              file=sys.stderr)
        return 1
    ed = json.loads(electrodes_path.read_text(encoding="utf-8"))
    electrodes = {k: (int(v["x"]), int(v["y"])) for k, v in ed["electrodes"].items()}

    if args.funscripts_stem:
        stem_dir = args.funscripts_stem.parent
        stem = args.funscripts_stem.name
    else:
        stem_dir = image_path.parent
        stem = image_path.stem

    funscripts = {ch: load_funscript(find_funscript(stem_dir, stem, ch)) for ch in CHANNELS}

    audio = args.audio.resolve() if args.audio else find_audio(stem_dir, stem)
    if audio:
        print(f"Audio: {audio}")
    else:
        print("No audio found — rendering silent video.")

    output = (args.output or image_path.with_suffix(".mp4")).resolve()

    render(
        image_path=image_path,
        electrodes=electrodes,
        funscripts=funscripts,
        audio=audio,
        output=output,
        fps=args.fps,
        max_dim=args.max_dim,
        duration_s=args.duration,
        bloom_strength=args.bloom,
        base_dim_range=(args.min_dim, args.max_base),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
