# FOCtave

**Convert stereo e-stim audio into 4-phase restim funscripts for FOC-Stim.**

FOCtave takes a traditional stereo e-stim audio file (WAV / FLAC / MP3) and
produces the five funscripts that [restim](https://github.com/diglet48/restim)
auto-detects when running in FOC-Stim 4-phase mode:

```
<name>.e1.funscript   -> intensity A  \
<name>.e2.funscript   -> intensity B  /  (pair 1, driven by L envelope)
<name>.e3.funscript   -> intensity C  \
<name>.e4.funscript   -> intensity D  /  (pair 2, driven by R envelope)
<name>.volume.funscript -> volume
```

The electrode pair duplication preserves the original stereo topology on a
4-electrode [FOC-Stim](https://github.com/diglet48/FOC-Stim) box: `(e1, e2)`
act as one bipolar terminal and `(e3, e4)` act as the other, so the track
feels the same way it would on a traditional two-channel stimulator - while
also gaining free cross-coupling between pairs thanks to FOC-Stim's any-to-any
routing.

---

## Quick start

```bash
pip install -r requirements.txt
python foctave.py path/to/your_track.wav
```

That's it - five `.funscript` files appear next to the audio, and restim
will auto-detect them when you point it at the matching media file.

MP3 / M4A / OGG inputs work if `ffmpeg` is on your `PATH`.

---

## How it works

1. Load the stereo audio.
2. Take the amplitude envelope of each channel:
   `|signal|` -> 20 Hz low-pass.
3. Compress the envelope with a gentle gamma curve (default cube root) so
   quiet passages stay feel-able without flattening peaks.
4. Normalize to 0-100 using a high percentile (not the absolute peak) so a
   single loud transient doesn't squash the rest of the track.
5. Downsample to ~30 Hz (one action every 33 ms) and dedupe consecutive
   identical values.
6. Duplicate L -> `e1` + `e2`, R -> `e3` + `e4`, RMS -> `volume`.

Defaults are empirically tuned against real-world 4-phase content to
produce a close match to established stereo-to-4-phase conversion styles.

---

## Presets

Four built-in presets cover the common use cases. Pick with `--preset`;
defaults to `belgium`.

```bash
python foctave.py input.wav                        # belgium (default)
python foctave.py input.wav --preset comfort
python foctave.py input.wav --preset dynamic
python foctave.py input.wav --preset endurance
```

| Preset | gamma | percentile | attack / release | floor | vol ramp | Feel |
|---|---:|---:|---:|---:|---:|---|
| `belgium` | 0.30 | 75 | - | - | - | Faithful FunBelgium-style punch; e-channels pegged at 90-100 for ~60% of the track. |
| `comfort` | 0.40 | 85 | 15 / 120 ms | 0.05 | - | Less saturated, musical transients, quiet sections never hit zero. Gentle. |
| `dynamic` | 0.50 | 95 | 10 / 80 ms | 0.03 | - | Closer to the source audio's actual loudness curve — loud stays loud, quiet stays quiet. |
| `endurance` | 0.35 | 80 | 20 / 150 ms | 0.08 | 0.5 %/min | Moderate baseline + gradual ramp-up over time. Designed for long tracks. |

### Individual tuning knobs

Any of these flags override the active preset:

| Flag | What it does |
|---|---|
| `--rate` | Funscript sample rate in Hz (default 30) |
| `--smooth` | Envelope low-pass cutoff in Hz (default 20) |
| `--gamma` | Compression curve exponent. 1.0 = linear, 0.5 = sqrt, lower = punchier. |
| `--percentile` | Normalization percentile (100 = peak). Lower = more saturation. |
| `--attack-ms` | Asymmetric attack time (ms). Fast catches transients. |
| `--release-ms` | Asymmetric release time (ms). Slow avoids choppy cutoff. |
| `--floor` | Minimum intensity 0-1. Prevents "did it disconnect?" moments. |
| `--volume-ramp` | Additive ramp on the volume channel in %/minute. |

Example - start from `comfort` but crank saturation:

```bash
python foctave.py input.wav --preset comfort --gamma 0.30 --percentile 75
```

---

## Output format

FOCtave writes the minimal funscript JSON used by restim's auto-detect:

```json
{"actions": [{"pos": 72, "at": 0}, {"pos": 74, "at": 33}, ...]}
```

`pos` is `0-100` intensity; `at` is milliseconds from start.

---

## Requirements

- Python 3.11+ (older may work; tested on 3.11)
- `numpy`, `scipy`, `soundfile` (see `requirements.txt`)
- `ffmpeg` on `PATH` for non-WAV/FLAC inputs

---

## Credits

- [restim](https://github.com/diglet48/restim) by diglet48 - the e-stim
  control software this tool targets.
- [FOC-Stim](https://github.com/diglet48/FOC-Stim) by diglet48 - the
  hardware this tool is designed for.
- Inspired by reverse-engineering the stereo-to-4-phase conversion style
  used by FunBelgium's published scripts.

---

## License

MIT - see [LICENSE](LICENSE).
