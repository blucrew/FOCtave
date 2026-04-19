"""
FOCtave - convert stereo e-stim audio into 4-phase restim funscripts.

Treats the input as traditional stereo e-stim and duplicates each channel's
amplitude envelope across an electrode pair:

    L envelope -> <name>.e1.funscript AND <name>.e2.funscript   (pair 1)
    R envelope -> <name>.e3.funscript AND <name>.e4.funscript   (pair 2)
    overall    -> <name>.volume.funscript

In foc-stim 4-phase mode this reconstructs the original stereo topology:
(e1,e2) act as one bipolar terminal, (e3,e4) as the other.

Usage:
    python foctave.py input.wav
    python foctave.py input.mp3 --out-dir ./out --rate 30
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfiltfilt


def load_audio(path: Path) -> tuple[np.ndarray, int]:
    try:
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception as e:
        if path.suffix.lower() in {".mp3", ".m4a", ".aac", ".ogg"}:
            data, sr = _ffmpeg_decode(path)
        else:
            raise RuntimeError(f"Failed to read {path}: {e}")
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    elif data.shape[1] > 2:
        data = data[:, :2]
    return data, sr


def _ffmpeg_decode(path: Path) -> tuple[np.ndarray, int]:
    if not shutil.which("ffmpeg"):
        raise RuntimeError(f"Cannot read {path.suffix} without ffmpeg.")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(path), "-ac", "2", "-f", "wav", tmp_path],
            check=True, capture_output=True,
        )
        return sf.read(tmp_path, dtype="float32", always_2d=True)
    finally:
        os.unlink(tmp_path)


def envelope(x: np.ndarray, sr: int, smooth_hz: float) -> np.ndarray:
    sos = butter(4, smooth_hz / (sr / 2), btype="low", output="sos")
    return sosfiltfilt(sos, np.abs(x))


def compress_curve(env: np.ndarray, gamma: float) -> np.ndarray:
    # gamma < 1 boosts quiet passages (cube root ~= 0.33 matches the
    # FunBelgium reference best empirically).
    return np.power(np.maximum(env, 0.0), gamma)


def normalize(env: np.ndarray, percentile: float) -> np.ndarray:
    peak = np.percentile(env, percentile) if percentile < 100 else env.max()
    if peak <= 1e-9:
        return np.zeros_like(env)
    return np.clip(env / peak, 0.0, 1.0)


def asymmetric_smooth(x: np.ndarray, sample_hz: float, attack_ms: float, release_ms: float) -> np.ndarray:
    """One-pole attack/release smoothing (audio compressor style) in the
    downsampled domain. attack_ms or release_ms of 0 means instant in that
    direction."""
    if attack_ms <= 0 and release_ms <= 0:
        return x
    dt_ms = 1000.0 / sample_hz
    a = np.exp(-dt_ms / attack_ms) if attack_ms > 0 else 0.0
    r = np.exp(-dt_ms / release_ms) if release_ms > 0 else 0.0
    y = np.empty_like(x)
    y[0] = x[0]
    for i in range(1, len(x)):
        if x[i] > y[i - 1]:
            y[i] = x[i] + a * (y[i - 1] - x[i])
        else:
            y[i] = x[i] + r * (y[i - 1] - x[i])
    return y


def apply_floor(x: np.ndarray, floor_0_1: float) -> np.ndarray:
    """Map [0,1] to [floor, 1] so quiet moments still register."""
    if floor_0_1 <= 0:
        return x
    return floor_0_1 + (1.0 - floor_0_1) * x


def apply_ramp(x: np.ndarray, sample_hz: float, pct_per_minute: float) -> np.ndarray:
    """Add a gradual linear ramp (restim wiki recommendation ~0.5%/min)."""
    if pct_per_minute == 0:
        return x
    n = len(x)
    minutes = np.arange(n) / (sample_hz * 60.0)
    ramp = minutes * (pct_per_minute / 100.0)
    return np.clip(x + ramp, 0.0, 1.0)


def write_funscript_minimal(path: Path, values_0_1: np.ndarray, out_rate_hz: float) -> None:
    """FunBelgium-style minimal JSON: {"actions": [{"pos":N,"at":ms}, ...]}"""
    dt_ms = 1000.0 / out_rate_hz
    actions = []
    last_pos = -1
    n = len(values_0_1)
    for i, v in enumerate(values_0_1):
        pos = int(round(float(v) * 100))
        if pos == last_pos and 0 < i < n - 1:
            continue
        actions.append({"pos": pos, "at": int(round(i * dt_ms))})
        last_pos = pos
    path.write_text(json.dumps({"actions": actions}, separators=(",", ":")), encoding="utf-8")


def convert(input_path: Path, out_dir: Path, out_rate_hz: float, smooth_hz: float,
            percentile: float, gamma: float, attack_ms: float, release_ms: float,
            floor: float, volume_ramp_pct_per_min: float) -> None:
    print(f"Loading {input_path}...")
    stereo, sr = load_audio(input_path)
    print(f"  {len(stereo)/sr:.1f}s @ {sr} Hz")

    L, R = stereo[:, 0], stereo[:, 1]

    print(f"Extracting envelopes (smoothed at {smooth_hz} Hz)...")
    L_env = envelope(L, sr, smooth_hz)
    R_env = envelope(R, sr, smooth_hz)
    V_env = envelope(np.sqrt(L * L + R * R), sr, smooth_hz)

    step = int(round(sr / out_rate_hz))
    L_ds, R_ds, V_ds = L_env[::step], R_env[::step], V_env[::step]

    print(f"Compressing (gamma={gamma}) and normalizing (percentile={percentile})...")
    L_n = normalize(compress_curve(L_ds, gamma), percentile)
    R_n = normalize(compress_curve(R_ds, gamma), percentile)
    # Volume is a master-intensity channel; keep it linear so restim's own
    # ramp and the user's master gain stay meaningful.
    V_n = normalize(V_ds, 99.5)

    if attack_ms > 0 or release_ms > 0:
        print(f"Attack/release smoothing ({attack_ms}/{release_ms} ms)...")
        L_n = asymmetric_smooth(L_n, out_rate_hz, attack_ms, release_ms)
        R_n = asymmetric_smooth(R_n, out_rate_hz, attack_ms, release_ms)

    if floor > 0:
        print(f"Floor={floor:.2f}")
        L_n = apply_floor(L_n, floor)
        R_n = apply_floor(R_n, floor)

    if volume_ramp_pct_per_min > 0:
        print(f"Volume ramp {volume_ramp_pct_per_min}%/min")
        V_n = apply_ramp(V_n, out_rate_hz, volume_ramp_pct_per_min)

    stem = input_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    # L envelope -> e1 and e2 (identical pair)
    for name in ("e1", "e2"):
        p = out_dir / f"{stem}.{name}.funscript"
        write_funscript_minimal(p, L_n, out_rate_hz)
        print(f"  wrote {p}")
    # R envelope -> e3 and e4 (identical pair)
    for name in ("e3", "e4"):
        p = out_dir / f"{stem}.{name}.funscript"
        write_funscript_minimal(p, R_n, out_rate_hz)
        print(f"  wrote {p}")
    p = out_dir / f"{stem}.volume.funscript"
    write_funscript_minimal(p, V_n, out_rate_hz)
    print(f"  wrote {p}")

    print("Done.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--rate", type=float, default=30.0,
                    help="Funscript sample rate in Hz (default 30, matches FunBelgium style)")
    ap.add_argument("--smooth", type=float, default=20.0,
                    help="Envelope low-pass cutoff in Hz (default 20)")
    ap.add_argument("--percentile", type=float, default=85.0,
                    help="Normalization percentile (default 85; tuned vs FunBelgium. "
                         "Use 100 for peak.)")
    ap.add_argument("--gamma", type=float, default=0.33,
                    help="Compression curve exponent (default 0.33 = cube root, "
                         "tuned vs FunBelgium; 1.0 = linear; 0.5 = sqrt)")
    ap.add_argument("--attack-ms", type=float, default=0.0,
                    help="Attack time for asymmetric smoothing in ms "
                         "(default 0 = symmetric; try 10-30 for musical feel)")
    ap.add_argument("--release-ms", type=float, default=0.0,
                    help="Release time for asymmetric smoothing in ms "
                         "(default 0 = symmetric; try 80-200 for musical feel)")
    ap.add_argument("--floor", type=float, default=0.0,
                    help="Minimum intensity 0-1 (default 0; try 0.05-0.10 for comfort)")
    ap.add_argument("--volume-ramp", type=float, default=0.0,
                    help="Additive volume ramp in %%/minute (default 0; "
                         "restim wiki suggests ~0.5)")
    args = ap.parse_args()

    input_path = args.input.resolve()
    if not input_path.exists():
        print(f"Input not found: {input_path}", file=sys.stderr)
        return 1
    out_dir = (args.out_dir or input_path.parent).resolve()

    convert(input_path, out_dir, args.rate, args.smooth, args.percentile, args.gamma,
            args.attack_ms, args.release_ms, args.floor, args.volume_ramp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
