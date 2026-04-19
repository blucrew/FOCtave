"""
FOCtave Studio — one-window GUI for the full pipeline.

Pick an audio file, pick an image, place electrodes, choose a preset, tune,
hit Render. Everything for the project lands in a named subfolder of your
chosen output directory:

    <output>/<project_name>/
        <project>.e1.funscript
        <project>.e2.funscript
        <project>.e3.funscript
        <project>.e4.funscript
        <project>.volume.funscript
        <project>.electrodes.json
        <project>.mp4

Originals (audio, image) stay where they are - nothing gets copied.

Usage:
    python studio.py
"""

import json
import queue
import re
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import foctave
import render as render_mod


LABELS = ["e1", "e2", "e3", "e4"]
COLORS = ["#ff4040", "#ffaa30", "#40ff60", "#40aaff"]
MARKER_RADIUS = 12
HIT_RADIUS = 18

PRESETS = foctave.PRESETS


def slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    return s.strip("_") or "project"


class ElectrodeCanvas(tk.Frame):
    """Reusable canvas widget for placing/editing e1..e4 on an image."""

    def __init__(self, master, on_change=None, **kw):
        super().__init__(master, bg="#111", **kw)
        self.on_change = on_change

        self.canvas = tk.Canvas(self, bg="#111", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)

        self.image: Image.Image | None = None
        self.photo: ImageTk.PhotoImage | None = None
        self.scale: float = 1.0
        self.img_offset_x: int = 0
        self.img_offset_y: int = 0
        self.electrodes: dict[str, tuple[int, int]] = {}
        self.drag_target: str | None = None

        self.canvas.bind("<Button-1>", self._on_left_click)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind("<Configure>", self._on_resize)

    # --- Public API ---

    def set_image(self, image_path: Path, existing_json: Path | None = None):
        self.image = Image.open(image_path).convert("RGB")
        self.electrodes = {}
        if existing_json and existing_json.exists():
            try:
                data = json.loads(existing_json.read_text(encoding="utf-8"))
                for label, pos in data.get("electrodes", {}).items():
                    if label in LABELS:
                        self.electrodes[label] = (int(pos["x"]), int(pos["y"]))
            except Exception:
                pass
        self._fit()
        self._redraw()

    def clear_image(self):
        self.image = None
        self.electrodes = {}
        self.canvas.delete("all")

    def reset_placements(self):
        self.electrodes = {}
        self._redraw()

    def get_electrodes(self) -> dict:
        return dict(self.electrodes)

    def all_placed(self) -> bool:
        return len(self.electrodes) == 4

    # --- Internals ---

    def _fit(self):
        if self.image is None:
            return
        iw, ih = self.image.size
        self.canvas.update_idletasks()
        cw = max(200, self.canvas.winfo_width())
        ch = max(200, self.canvas.winfo_height())
        self.scale = min(cw / iw, ch / ih, 1.0)
        dw, dh = int(iw * self.scale), int(ih * self.scale)
        self.img_offset_x = (cw - dw) // 2
        self.img_offset_y = (ch - dh) // 2
        disp = self.image.resize((dw, dh), Image.LANCZOS) if self.scale < 1.0 else self.image
        self.photo = ImageTk.PhotoImage(disp)
        self.canvas.delete("image")
        self.canvas.create_image(self.img_offset_x, self.img_offset_y,
                                 anchor="nw", image=self.photo, tags=("image",))
        self.canvas.tag_lower("image")

    def _redraw(self):
        self.canvas.delete("marker")
        if self.image is None:
            return
        for i, label in enumerate(LABELS):
            if label not in self.electrodes:
                continue
            ix, iy = self.electrodes[label]
            cx, cy = self._img_to_canvas(ix, iy)
            color = COLORS[i]
            self.canvas.create_oval(cx - MARKER_RADIUS, cy - MARKER_RADIUS,
                                    cx + MARKER_RADIUS, cy + MARKER_RADIUS,
                                    outline=color, width=3, tags=("marker", label))
            self.canvas.create_oval(cx - 3, cy - 3, cx + 3, cy + 3,
                                    fill=color, outline="", tags=("marker", label))
            self.canvas.create_text(cx + MARKER_RADIUS + 4, cy, text=label,
                                    fill=color, font=("Arial", 13, "bold"),
                                    anchor="w", tags=("marker", label))
        # Preview path. With all 4 placed we draw the curved spline the
        # render will actually trace; with 2 or 3 we fall back to straight
        # segments through placed points.
        placed = [lab for lab in LABELS if lab in self.electrodes]
        if len(placed) == 4:
            ordered = [self._img_to_canvas(*self.electrodes[lab]) for lab in LABELS]
            curve = render_mod.catmull_rom_polyline(ordered, samples_per_segment=40)
            flat = []
            for x, y in curve:
                flat.extend([int(x), int(y)])
            if len(flat) >= 4:
                self.canvas.create_line(*flat, fill="#aaa", width=1, dash=(4, 3),
                                        tags=("marker",), smooth=False)
        elif len(placed) >= 2:
            flat = []
            for lab in placed:
                p = self._img_to_canvas(*self.electrodes[lab])
                flat.extend(p)
            self.canvas.create_line(*flat, fill="#888", width=1, dash=(3, 3),
                                    tags=("marker",))
        if self.on_change:
            self.on_change()

    def _img_to_canvas(self, ix, iy):
        return (int(ix * self.scale) + self.img_offset_x,
                int(iy * self.scale) + self.img_offset_y)

    def _canvas_to_img(self, cx, cy):
        if self.image is None or self.scale <= 0:
            return (0, 0)
        ix = int(round((cx - self.img_offset_x) / self.scale))
        iy = int(round((cy - self.img_offset_y) / self.scale))
        iw, ih = self.image.size
        return (max(0, min(iw - 1, ix)), max(0, min(ih - 1, iy)))

    def _inside_image(self, cx, cy):
        if self.image is None:
            return False
        iw, ih = self.image.size
        dw, dh = int(iw * self.scale), int(ih * self.scale)
        return (self.img_offset_x <= cx < self.img_offset_x + dw
                and self.img_offset_y <= cy < self.img_offset_y + dh)

    def _find_at(self, cx, cy):
        for label, (ix, iy) in self.electrodes.items():
            ex, ey = self._img_to_canvas(ix, iy)
            if (cx - ex) ** 2 + (cy - ey) ** 2 <= HIT_RADIUS ** 2:
                return label
        return None

    def _next_unplaced(self):
        for label in LABELS:
            if label not in self.electrodes:
                return label
        return None

    def _on_left_click(self, event):
        if self.image is None:
            return
        hit = self._find_at(event.x, event.y)
        if hit:
            self.drag_target = hit
            self.canvas.config(cursor="fleur")
            return
        if not self._inside_image(event.x, event.y):
            return
        nxt = self._next_unplaced()
        if nxt is None:
            return
        ix, iy = self._canvas_to_img(event.x, event.y)
        self.electrodes[nxt] = (ix, iy)
        self._redraw()

    def _on_drag(self, event):
        if self.drag_target is None or self.image is None:
            return
        ix, iy = self._canvas_to_img(event.x, event.y)
        self.electrodes[self.drag_target] = (ix, iy)
        self._redraw()

    def _on_release(self, _event):
        self.drag_target = None
        self.canvas.config(cursor="crosshair")

    def _on_right_click(self, event):
        hit = self._find_at(event.x, event.y)
        if hit:
            del self.electrodes[hit]
            self._redraw()

    def _on_resize(self, _event):
        if self.image is not None:
            self._fit()
            self._redraw()


class StudioApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("FOCtave Studio")
        self.root.geometry("1400x900")
        self.root.minsize(900, 650)

        self.audio_path = tk.StringVar()
        self.image_path = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.project_name = tk.StringVar(value="untitled")
        self.preset_var = tk.StringVar(value="belgium")

        self.tune_vars = {
            "gamma": tk.DoubleVar(value=0.30),
            "percentile": tk.DoubleVar(value=75.0),
            "attack_ms": tk.DoubleVar(value=0.0),
            "release_ms": tk.DoubleVar(value=0.0),
            "floor": tk.DoubleVar(value=0.0),
            "volume_ramp": tk.DoubleVar(value=0.0),
        }
        self.video_vars = {
            "max_dim": tk.IntVar(value=1280),
            "fps": tk.IntVar(value=30),
            "bloom": tk.DoubleVar(value=0.45),
            "min_dim": tk.DoubleVar(value=0.55),
        }
        self.status_var = tk.StringVar(value="Pick an audio track and an image to begin.")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.render_busy = False

        self._build_top()
        self._build_main()
        self._build_bottom()

        self._apply_preset()  # initialise tune vars to belgium defaults

        # Thread-safe queue for progress updates from worker thread
        self.ui_queue: queue.Queue = queue.Queue()
        self.root.after(50, self._drain_queue)

    # --- UI ---

    def _build_top(self):
        top = tk.Frame(self.root, bg="#1e1e1e")
        top.pack(side="top", fill="x", padx=6, pady=6)

        LABEL_WIDTH = 9      # uniform label column
        ENTRY_WIDTH = 62     # characters; scrollable if path is longer

        # Row 0 - Project (editable text)
        tk.Label(top, text="Project:", width=LABEL_WIDTH, anchor="w",
                 fg="#ddd", bg="#1e1e1e").grid(row=0, column=0, sticky="w", padx=(4, 6))
        ttk.Entry(top, textvariable=self.project_name, width=ENTRY_WIDTH).grid(
            row=0, column=1, sticky="w", pady=2)
        tk.Label(top, text="(used for folder + file names)",
                 fg="#777", bg="#1e1e1e").grid(row=0, column=2, sticky="w", padx=10)

        # Rows 1-3 - file pickers: read-only path display + Browse button
        def file_row(row, label, var, cb):
            tk.Label(top, text=label, width=LABEL_WIDTH, anchor="w",
                     fg="#ddd", bg="#1e1e1e").grid(row=row, column=0, sticky="w", padx=(4, 6))
            entry = ttk.Entry(top, textvariable=var, width=ENTRY_WIDTH, state="readonly")
            entry.grid(row=row, column=1, sticky="w", pady=2)
            ttk.Button(top, text="Browse…", command=cb, width=10).grid(
                row=row, column=2, sticky="w", padx=(6, 4), pady=2)

        file_row(1, "Audio:", self.audio_path, self._browse_audio)
        file_row(2, "Image:", self.image_path, self._browse_image)
        file_row(3, "Output:", self.output_dir, self._browse_output)

    def _build_main(self):
        main = tk.Frame(self.root)
        main.pack(side="top", fill="both", expand=True)

        # Image canvas (left, expands)
        self.canvas_widget = ElectrodeCanvas(main, on_change=self._on_canvas_change)
        self.canvas_widget.pack(side="left", fill="both", expand=True)

        # Right control panel
        panel = tk.Frame(main, bg="#1e1e1e", width=320)
        panel.pack(side="right", fill="y")
        panel.pack_propagate(False)

        # Preset
        tk.Label(panel, text="Preset", fg="#fff", bg="#1e1e1e",
                 font=("Arial", 11, "bold")).pack(anchor="w", padx=10, pady=(10, 2))
        for name in PRESETS.keys():
            rb = ttk.Radiobutton(panel, text=name, value=name,
                                 variable=self.preset_var,
                                 command=self._apply_preset)
            rb.pack(anchor="w", padx=20)

        ttk.Separator(panel, orient="horizontal").pack(fill="x", pady=8, padx=10)

        # Tuning
        tk.Label(panel, text="Tuning", fg="#fff", bg="#1e1e1e",
                 font=("Arial", 11, "bold")).pack(anchor="w", padx=10)

        def tune_row(parent, label, var, frm, to, inc, fmt="%.2f"):
            f = tk.Frame(parent, bg="#1e1e1e")
            f.pack(fill="x", padx=10, pady=2)
            tk.Label(f, text=label, width=12, anchor="w",
                     fg="#ddd", bg="#1e1e1e").pack(side="left")
            sb = ttk.Spinbox(f, from_=frm, to=to, increment=inc,
                             textvariable=var, format=fmt, width=8)
            sb.pack(side="right")

        tune_row(panel, "gamma", self.tune_vars["gamma"], 0.1, 1.0, 0.05)
        tune_row(panel, "percentile", self.tune_vars["percentile"], 50.0, 100.0, 1.0, "%.0f")
        tune_row(panel, "attack ms", self.tune_vars["attack_ms"], 0.0, 200.0, 5.0, "%.0f")
        tune_row(panel, "release ms", self.tune_vars["release_ms"], 0.0, 500.0, 10.0, "%.0f")
        tune_row(panel, "floor", self.tune_vars["floor"], 0.0, 0.30, 0.01)
        tune_row(panel, "vol ramp %/min", self.tune_vars["volume_ramp"], 0.0, 2.0, 0.1, "%.1f")

        ttk.Separator(panel, orient="horizontal").pack(fill="x", pady=8, padx=10)

        # Video
        tk.Label(panel, text="Video", fg="#fff", bg="#1e1e1e",
                 font=("Arial", 11, "bold")).pack(anchor="w", padx=10)
        tune_row(panel, "max dim (px)", self.video_vars["max_dim"], 480, 3840, 160, "%.0f")
        tune_row(panel, "fps", self.video_vars["fps"], 15, 60, 1, "%.0f")
        tune_row(panel, "bloom", self.video_vars["bloom"], 0.0, 1.0, 0.05)
        tune_row(panel, "min dim (base)", self.video_vars["min_dim"], 0.1, 1.0, 0.05)

        ttk.Separator(panel, orient="horizontal").pack(fill="x", pady=8, padx=10)

        # Electrodes info + reset
        self.electrode_count_label = tk.Label(panel, text="Electrodes: 0/4",
                                              fg="#ddd", bg="#1e1e1e")
        self.electrode_count_label.pack(anchor="w", padx=10, pady=3)
        ttk.Button(panel, text="Reset electrodes",
                   command=self._reset_electrodes).pack(fill="x", padx=10, pady=4)

    def _build_bottom(self):
        bot = tk.Frame(self.root, bg="#1e1e1e")
        bot.pack(side="bottom", fill="x")

        self.render_button = ttk.Button(bot, text="▶  Render video",
                                        command=self._start_render)
        self.render_button.pack(side="right", padx=10, pady=6)

        self.progress_bar = ttk.Progressbar(bot, orient="horizontal",
                                            mode="determinate",
                                            variable=self.progress_var,
                                            maximum=100.0)
        self.progress_bar.pack(side="right", fill="x", expand=True,
                               padx=10, pady=6)

        self.status_label = tk.Label(bot, textvariable=self.status_var,
                                     anchor="w", fg="#ddd", bg="#1e1e1e")
        self.status_label.pack(side="left", fill="x", padx=10, pady=6)

    # --- File browsers ---

    def _browse_audio(self):
        p = filedialog.askopenfilename(
            title="Select audio track",
            filetypes=[("Audio", "*.wav *.mp3 *.flac *.m4a *.ogg"), ("All", "*.*")])
        if p:
            self.audio_path.set(p)
            self._autofill_output()

    def _browse_image(self):
        p = filedialog.askopenfilename(
            title="Select image",
            filetypes=[("Images", "*.jpg *.jpeg *.png *.webp *.bmp"), ("All", "*.*")])
        if p:
            self.image_path.set(p)
            img_path = Path(p)
            ep = img_path.with_suffix(".electrodes.json")
            self.canvas_widget.set_image(img_path, existing_json=ep)
            self._autofill_output()
            self._on_canvas_change()

    def _browse_output(self):
        p = filedialog.askdirectory(title="Select output folder")
        if p:
            self.output_dir.set(p)

    def _autofill_output(self):
        """When either source is picked, default output dir to image's parent."""
        if self.output_dir.get():
            return
        for var in (self.image_path, self.audio_path):
            if var.get():
                self.output_dir.set(str(Path(var.get()).parent))
                return

    # --- Preset / tune sync ---

    def _apply_preset(self):
        p = PRESETS[self.preset_var.get()]
        for k, v in p.items():
            if k in self.tune_vars:
                self.tune_vars[k].set(v)
        self.status_var.set(f"Preset: {self.preset_var.get()}")

    def _on_canvas_change(self):
        n = len(self.canvas_widget.get_electrodes())
        self.electrode_count_label.config(text=f"Electrodes: {n}/4")

    def _reset_electrodes(self):
        if not self.canvas_widget.get_electrodes():
            return
        if messagebox.askyesno("Reset", "Clear all placed electrodes?"):
            self.canvas_widget.reset_placements()
            self._on_canvas_change()

    # --- Rendering ---

    def _start_render(self):
        if self.render_busy:
            return
        # Validate
        name = slug(self.project_name.get())
        audio = self.audio_path.get().strip()
        image = self.image_path.get().strip()
        outdir = self.output_dir.get().strip()

        if not audio or not Path(audio).exists():
            messagebox.showerror("Audio missing", "Pick a valid audio file first.")
            return
        if not image or not Path(image).exists():
            messagebox.showerror("Image missing", "Pick a valid image first.")
            return
        if not outdir:
            messagebox.showerror("Output missing", "Pick an output folder first.")
            return
        if not self.canvas_widget.all_placed():
            if not messagebox.askyesno(
                "Incomplete placement",
                f"Only {len(self.canvas_widget.get_electrodes())}/4 electrodes placed. "
                "The ribbon needs all 4 to render meaningfully. Render anyway?"):
                return

        self.render_busy = True
        self.render_button.config(state="disabled")
        self.progress_var.set(0.0)
        self.status_var.set("Starting…")

        t = threading.Thread(
            target=self._render_worker,
            args=(name, Path(audio), Path(image), Path(outdir),
                  dict(self.canvas_widget.get_electrodes()),
                  {k: v.get() for k, v in self.tune_vars.items()},
                  {k: v.get() for k, v in self.video_vars.items()}),
            daemon=True,
        )
        t.start()

    def _render_worker(self, name, audio, image, outdir, electrodes, tune, video):
        try:
            proj_dir = outdir / name
            proj_dir.mkdir(parents=True, exist_ok=True)

            # 1. Save electrodes.json
            iw, ih = Image.open(image).size
            electrodes_json = proj_dir / f"{name}.electrodes.json"
            electrodes_json.write_text(json.dumps({
                "image": image.name,
                "image_size": {"w": iw, "h": ih},
                "electrodes": {k: {"x": v[0], "y": v[1]} for k, v in electrodes.items()},
            }, indent=2))
            self._post_status("Saved electrodes.json", 0.02)

            # 2. Convert audio -> funscripts
            def convert_progress(frac, msg):
                # Map to 0..40% of total progress
                self._post_status(msg, 0.02 + frac * 0.38)

            foctave.convert(
                input_path=audio,
                out_dir=proj_dir,
                out_rate_hz=30.0,
                smooth_hz=20.0,
                percentile=tune["percentile"],
                gamma=tune["gamma"],
                attack_ms=tune["attack_ms"],
                release_ms=tune["release_ms"],
                floor=tune["floor"],
                volume_ramp_pct_per_min=tune["volume_ramp"],
                output_stem=name,
                progress=convert_progress,
            )

            # 3. Load funscripts back for render
            funscripts = {}
            for ch in ["e1", "e2", "e3", "e4", "volume"]:
                funscripts[ch] = render_mod.load_funscript(
                    proj_dir / f"{name}.{ch}.funscript")

            # 4. Render video
            output_mp4 = proj_dir / f"{name}.mp4"

            def render_progress(frac, msg):
                # Map to 40..100% of total progress
                self._post_status(msg, 0.40 + frac * 0.60)

            render_mod.render(
                image_path=image,
                electrodes=electrodes,
                funscripts=funscripts,
                audio=audio,
                output=output_mp4,
                fps=video["fps"],
                max_dim=video["max_dim"],
                duration_s=None,
                bloom_strength=video["bloom"],
                base_dim_range=(video["min_dim"], 1.0),
                progress=render_progress,
            )

            self._post_status(f"✓ Done. Project at {proj_dir}", 1.0, done=True,
                              final_folder=proj_dir)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._post_status(f"Error: {e}", self.progress_var.get(), done=True)

    def _post_status(self, msg: str, fraction: float, done: bool = False,
                     final_folder: Path | None = None):
        self.ui_queue.put((msg, fraction, done, final_folder))

    def _drain_queue(self):
        try:
            while True:
                msg, frac, done, final_folder = self.ui_queue.get_nowait()
                self.status_var.set(msg)
                self.progress_var.set(frac * 100)
                if done:
                    self.render_busy = False
                    self.render_button.config(state="normal")
                    if final_folder:
                        if messagebox.askyesno("Render complete",
                                               f"Output: {final_folder}\n\nOpen folder?"):
                            import os
                            os.startfile(final_folder)  # Windows
        except queue.Empty:
            pass
        self.root.after(50, self._drain_queue)


def main() -> int:
    root = tk.Tk()
    StudioApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
