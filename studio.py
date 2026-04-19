"""
FOCtave Studio - one-window GUI for the full multi-image pipeline.

Pick an audio track, add one or more images, place electrodes on each,
choose a preset, tune, hit Render. Each image's placement is also saved
as a sidecar `<image>.electrodes.json` next to the image, so reusing the
image in a future project auto-loads the placement - your image library
builds itself as you work.

Output layout per project:

    <output>/<project_name>/
        <project>.{e1..e4, volume}.funscript
        <project>.electrodes.json   (combined record of scenes)
        <project>.mp4
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
    """Reusable canvas widget for placing e1..e4 on an image."""

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

    def set_image(self, image_path: Path | None, electrodes: dict | None = None):
        if image_path is None:
            self.image = None
            self.electrodes = {}
            self.canvas.delete("all")
            if self.on_change:
                self.on_change()
            return
        self.image = Image.open(image_path).convert("RGB")
        self.electrodes = dict(electrodes or {})
        self._fit()
        self._redraw()

    def reset_placements(self):
        self.electrodes = {}
        self._redraw()

    def get_electrodes(self) -> dict:
        return dict(self.electrodes)

    def all_placed(self) -> bool:
        return len(self.electrodes) == 4

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
                flat.extend(self._img_to_canvas(*self.electrodes[lab]))
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
        self.electrodes[nxt] = self._canvas_to_img(event.x, event.y)
        self._redraw()

    def _on_drag(self, event):
        if self.drag_target is None or self.image is None:
            return
        self.electrodes[self.drag_target] = self._canvas_to_img(event.x, event.y)
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
        self.root.geometry("1500x900")
        self.root.minsize(1000, 650)

        self.audio_path = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.project_name = tk.StringVar(value="untitled")
        self.preset_var = tk.StringVar(value="belgium")

        # Scenes: list[{"path": Path, "electrodes": dict}]
        self.scenes: list[dict] = []
        self.active_scene_idx: int = -1

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
        self.scene_duration_var = tk.DoubleVar(value=20.0)
        self.crossfade_var = tk.DoubleVar(value=0.5)
        self.crossfade_enabled = tk.BooleanVar(value=True)

        self.status_var = tk.StringVar(value="Pick an audio track and add an image to begin.")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.render_busy = False

        self._build_top()
        self._build_main()
        self._build_bottom()
        self._apply_preset()

        self.ui_queue: queue.Queue = queue.Queue()
        self.root.after(50, self._drain_queue)

    # --- Top (file pickers) ---

    def _build_top(self):
        top = tk.Frame(self.root, bg="#1e1e1e")
        top.pack(side="top", fill="x", padx=6, pady=6)
        LW, EW = 9, 62

        tk.Label(top, text="Project:", width=LW, anchor="w",
                 fg="#ddd", bg="#1e1e1e").grid(row=0, column=0, sticky="w", padx=(4, 6))
        ttk.Entry(top, textvariable=self.project_name, width=EW).grid(
            row=0, column=1, sticky="w", pady=2)
        tk.Label(top, text="(folder + file stem)", fg="#777", bg="#1e1e1e").grid(
            row=0, column=2, sticky="w", padx=10)

        def file_row(row, label, var, cb):
            tk.Label(top, text=label, width=LW, anchor="w",
                     fg="#ddd", bg="#1e1e1e").grid(row=row, column=0, sticky="w", padx=(4, 6))
            ttk.Entry(top, textvariable=var, width=EW, state="readonly").grid(
                row=row, column=1, sticky="w", pady=2)
            ttk.Button(top, text="Browse…", command=cb, width=10).grid(
                row=row, column=2, sticky="w", padx=(6, 4), pady=2)

        file_row(1, "Audio:", self.audio_path, self._browse_audio)
        file_row(2, "Output:", self.output_dir, self._browse_output)

    # --- Main (scenes panel + canvas + controls panel) ---

    def _build_main(self):
        main = tk.Frame(self.root)
        main.pack(side="top", fill="both", expand=True)

        # Left: scenes panel
        self._build_scenes_panel(main)

        # Middle: image canvas
        self.canvas_widget = ElectrodeCanvas(main, on_change=self._on_canvas_change)
        self.canvas_widget.pack(side="left", fill="both", expand=True)

        # Right: controls panel
        self._build_controls_panel(main)

    def _build_scenes_panel(self, parent):
        panel = tk.Frame(parent, bg="#1a1a1a", width=240)
        panel.pack(side="left", fill="y")
        panel.pack_propagate(False)

        tk.Label(panel, text="Scenes", fg="#fff", bg="#1a1a1a",
                 font=("Arial", 11, "bold")).pack(anchor="w", padx=10, pady=(10, 2))
        tk.Label(panel, text="Images rotate through\nthe video in order.",
                 fg="#888", bg="#1a1a1a", font=("Arial", 8),
                 justify="left").pack(anchor="w", padx=10, pady=(0, 6))

        lb_frame = tk.Frame(panel, bg="#1a1a1a")
        lb_frame.pack(fill="both", expand=True, padx=6, pady=2)
        sb = ttk.Scrollbar(lb_frame, orient="vertical")
        self.scene_listbox = tk.Listbox(lb_frame, bg="#0f0f0f", fg="#ddd",
                                        selectbackground="#3a6",
                                        selectforeground="#fff",
                                        highlightthickness=0, activestyle="none",
                                        font=("Consolas", 9),
                                        yscrollcommand=sb.set, exportselection=False)
        sb.config(command=self.scene_listbox.yview)
        sb.pack(side="right", fill="y")
        self.scene_listbox.pack(side="left", fill="both", expand=True)
        self.scene_listbox.bind("<<ListboxSelect>>", self._on_scene_select)

        btns = tk.Frame(panel, bg="#1a1a1a")
        btns.pack(fill="x", padx=6, pady=4)
        ttk.Button(btns, text="+ Add image", command=self._add_scene).pack(side="left", padx=2)
        ttk.Button(btns, text="− Remove", command=self._remove_scene).pack(side="left", padx=2)

        ttk.Separator(panel, orient="horizontal").pack(fill="x", pady=6, padx=8)

        # Rotation controls
        tk.Label(panel, text="Rotation", fg="#fff", bg="#1a1a1a",
                 font=("Arial", 10, "bold")).pack(anchor="w", padx=10)
        row = tk.Frame(panel, bg="#1a1a1a")
        row.pack(fill="x", padx=10, pady=2)
        tk.Label(row, text="every", width=6, anchor="w",
                 fg="#ddd", bg="#1a1a1a").pack(side="left")
        ttk.Spinbox(row, from_=1.0, to=600.0, increment=1.0,
                    textvariable=self.scene_duration_var,
                    format="%.1f", width=7).pack(side="left")
        tk.Label(row, text="sec", fg="#ddd", bg="#1a1a1a").pack(side="left", padx=4)

        row2 = tk.Frame(panel, bg="#1a1a1a")
        row2.pack(fill="x", padx=10, pady=2)
        ttk.Checkbutton(row2, text="crossfade",
                        variable=self.crossfade_enabled).pack(side="left")
        ttk.Spinbox(row2, from_=0.0, to=3.0, increment=0.1,
                    textvariable=self.crossfade_var,
                    format="%.1f", width=6).pack(side="right")

        ttk.Separator(panel, orient="horizontal").pack(fill="x", pady=6, padx=8)

        # Electrodes info
        self.electrode_count_label = tk.Label(panel, text="Electrodes: 0/4",
                                              fg="#ddd", bg="#1a1a1a")
        self.electrode_count_label.pack(anchor="w", padx=10, pady=2)
        ttk.Button(panel, text="Reset this scene's electrodes",
                   command=self._reset_electrodes).pack(fill="x", padx=10, pady=(2, 10))

    def _build_controls_panel(self, parent):
        panel = tk.Frame(parent, bg="#1e1e1e", width=320)
        panel.pack(side="right", fill="y")
        panel.pack_propagate(False)

        # Preset
        tk.Label(panel, text="Preset", fg="#fff", bg="#1e1e1e",
                 font=("Arial", 11, "bold")).pack(anchor="w", padx=10, pady=(10, 2))
        for name in PRESETS.keys():
            ttk.Radiobutton(panel, text=name, value=name,
                            variable=self.preset_var,
                            command=self._apply_preset).pack(anchor="w", padx=20)

        ttk.Separator(panel, orient="horizontal").pack(fill="x", pady=8, padx=10)

        tk.Label(panel, text="Tuning", fg="#fff", bg="#1e1e1e",
                 font=("Arial", 11, "bold")).pack(anchor="w", padx=10)

        def tune_row(parent, label, var, frm, to, inc, fmt="%.2f"):
            f = tk.Frame(parent, bg="#1e1e1e")
            f.pack(fill="x", padx=10, pady=2)
            tk.Label(f, text=label, width=12, anchor="w",
                     fg="#ddd", bg="#1e1e1e").pack(side="left")
            ttk.Spinbox(f, from_=frm, to=to, increment=inc,
                        textvariable=var, format=fmt, width=8).pack(side="right")

        tune_row(panel, "gamma", self.tune_vars["gamma"], 0.1, 1.0, 0.05)
        tune_row(panel, "percentile", self.tune_vars["percentile"], 50.0, 100.0, 1.0, "%.0f")
        tune_row(panel, "attack ms", self.tune_vars["attack_ms"], 0.0, 200.0, 5.0, "%.0f")
        tune_row(panel, "release ms", self.tune_vars["release_ms"], 0.0, 500.0, 10.0, "%.0f")
        tune_row(panel, "floor", self.tune_vars["floor"], 0.0, 0.30, 0.01)
        tune_row(panel, "vol ramp %/min", self.tune_vars["volume_ramp"], 0.0, 2.0, 0.1, "%.1f")

        ttk.Separator(panel, orient="horizontal").pack(fill="x", pady=8, padx=10)

        tk.Label(panel, text="Video", fg="#fff", bg="#1e1e1e",
                 font=("Arial", 11, "bold")).pack(anchor="w", padx=10)
        tune_row(panel, "max dim (px)", self.video_vars["max_dim"], 480, 3840, 160, "%.0f")
        tune_row(panel, "fps", self.video_vars["fps"], 15, 60, 1, "%.0f")
        tune_row(panel, "bloom", self.video_vars["bloom"], 0.0, 1.0, 0.05)
        tune_row(panel, "min dim (base)", self.video_vars["min_dim"], 0.1, 1.0, 0.05)

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
        tk.Label(bot, textvariable=self.status_var,
                 anchor="w", fg="#ddd", bg="#1e1e1e").pack(side="left", fill="x", padx=10, pady=6)

    # --- File browsers ---

    def _browse_audio(self):
        p = filedialog.askopenfilename(
            title="Select audio track",
            filetypes=[("Audio", "*.wav *.mp3 *.flac *.m4a *.ogg"), ("All", "*.*")])
        if p:
            self.audio_path.set(p)
            self._autofill_output()

    def _browse_output(self):
        p = filedialog.askdirectory(title="Select output folder")
        if p:
            self.output_dir.set(p)

    def _autofill_output(self):
        if self.output_dir.get():
            return
        if self.audio_path.get():
            self.output_dir.set(str(Path(self.audio_path.get()).parent))
            return
        if self.scenes:
            self.output_dir.set(str(Path(self.scenes[0]["path"]).parent))

    # --- Scene management ---

    def _add_scene(self):
        paths = filedialog.askopenfilenames(
            title="Select image(s) to add",
            filetypes=[("Images", "*.jpg *.jpeg *.png *.webp *.bmp"), ("All", "*.*")])
        if not paths:
            return
        for p in paths:
            pth = Path(p).resolve()
            # Library auto-load from sidecar
            sidecar = pth.with_suffix(".electrodes.json")
            electrodes = {}
            if sidecar.exists():
                try:
                    data = json.loads(sidecar.read_text(encoding="utf-8"))
                    for lab, pos in data.get("electrodes", {}).items():
                        if lab in LABELS:
                            electrodes[lab] = (int(pos["x"]), int(pos["y"]))
                except Exception:
                    pass
            self.scenes.append({"path": pth, "electrodes": electrodes})
        self._refresh_scene_list()
        # Auto-select the first added scene if nothing was selected
        if self.active_scene_idx < 0:
            self.active_scene_idx = 0
            self._load_active_scene()
        self._autofill_output()

    def _remove_scene(self):
        if self.active_scene_idx < 0 or not self.scenes:
            return
        idx = self.active_scene_idx
        del self.scenes[idx]
        if not self.scenes:
            self.active_scene_idx = -1
            self.canvas_widget.set_image(None)
        else:
            self.active_scene_idx = min(idx, len(self.scenes) - 1)
            self._load_active_scene()
        self._refresh_scene_list()

    def _refresh_scene_list(self):
        self.scene_listbox.delete(0, tk.END)
        for i, scene in enumerate(self.scenes):
            placed = len(scene["electrodes"])
            name = Path(scene["path"]).name
            short = name if len(name) <= 22 else name[:19] + "..."
            self.scene_listbox.insert(tk.END, f"{short} ({placed}/4)")
        if 0 <= self.active_scene_idx < len(self.scenes):
            self.scene_listbox.selection_set(self.active_scene_idx)
            self.scene_listbox.see(self.active_scene_idx)

    def _on_scene_select(self, _event):
        sel = self.scene_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        # Save current scene's placements back before switching
        if 0 <= self.active_scene_idx < len(self.scenes):
            self.scenes[self.active_scene_idx]["electrodes"] = self.canvas_widget.get_electrodes()
        self.active_scene_idx = idx
        self._load_active_scene()

    def _load_active_scene(self):
        if self.active_scene_idx < 0 or self.active_scene_idx >= len(self.scenes):
            self.canvas_widget.set_image(None)
            return
        s = self.scenes[self.active_scene_idx]
        self.canvas_widget.set_image(s["path"], electrodes=s["electrodes"])

    def _on_canvas_change(self):
        if 0 <= self.active_scene_idx < len(self.scenes):
            self.scenes[self.active_scene_idx]["electrodes"] = self.canvas_widget.get_electrodes()
            # Update listbox entry in place
            placed = len(self.scenes[self.active_scene_idx]["electrodes"])
            name = Path(self.scenes[self.active_scene_idx]["path"]).name
            short = name if len(name) <= 22 else name[:19] + "..."
            self.scene_listbox.delete(self.active_scene_idx)
            self.scene_listbox.insert(self.active_scene_idx, f"{short} ({placed}/4)")
            self.scene_listbox.selection_set(self.active_scene_idx)
        n = len(self.canvas_widget.get_electrodes())
        self.electrode_count_label.config(text=f"Electrodes: {n}/4")

    def _reset_electrodes(self):
        if not self.canvas_widget.get_electrodes():
            return
        if messagebox.askyesno("Reset", "Clear placements on the current scene?"):
            self.canvas_widget.reset_placements()
            self._on_canvas_change()

    # --- Preset sync ---

    def _apply_preset(self):
        p = PRESETS[self.preset_var.get()]
        for k, v in p.items():
            if k in self.tune_vars:
                self.tune_vars[k].set(v)
        self.status_var.set(f"Preset: {self.preset_var.get()}")

    # --- Rendering ---

    def _start_render(self):
        if self.render_busy:
            return
        name = slug(self.project_name.get())
        audio = self.audio_path.get().strip()
        outdir = self.output_dir.get().strip()

        if not audio or not Path(audio).exists():
            messagebox.showerror("Audio missing", "Pick a valid audio file first.")
            return
        if not self.scenes:
            messagebox.showerror("No scenes", "Add at least one image first.")
            return
        if not outdir:
            messagebox.showerror("Output missing", "Pick an output folder first.")
            return

        # Sync current canvas back to active scene before we snapshot
        if 0 <= self.active_scene_idx < len(self.scenes):
            self.scenes[self.active_scene_idx]["electrodes"] = self.canvas_widget.get_electrodes()

        incomplete = [i for i, s in enumerate(self.scenes) if len(s["electrodes"]) != 4]
        if incomplete:
            names = ", ".join(Path(self.scenes[i]["path"]).name for i in incomplete[:3])
            more = "" if len(incomplete) <= 3 else f" +{len(incomplete)-3} more"
            if not messagebox.askyesno(
                "Incomplete placements",
                f"{len(incomplete)} scene(s) missing electrodes: {names}{more}.\n"
                "Render anyway?"):
                return

        self.render_busy = True
        self.render_button.config(state="disabled")
        self.progress_var.set(0.0)
        self.status_var.set("Starting…")

        scenes_snapshot = [{"path": Path(s["path"]), "electrodes": dict(s["electrodes"])}
                           for s in self.scenes]

        t = threading.Thread(
            target=self._render_worker,
            args=(name, Path(audio), Path(outdir), scenes_snapshot,
                  {k: v.get() for k, v in self.tune_vars.items()},
                  {k: v.get() for k, v in self.video_vars.items()},
                  float(self.scene_duration_var.get()),
                  float(self.crossfade_var.get()) if self.crossfade_enabled.get() else 0.0),
            daemon=True,
        )
        t.start()

    def _render_worker(self, name, audio, outdir, scenes, tune, video,
                       scene_duration_s, crossfade_s):
        try:
            proj_dir = outdir / name
            proj_dir.mkdir(parents=True, exist_ok=True)

            # 1. Save combined scenes JSON for the project + update per-image sidecars
            combined = {"scenes": []}
            for s in scenes:
                img_path = s["path"]
                iw, ih = Image.open(img_path).size
                entry = {
                    "image": str(img_path),
                    "image_size": {"w": iw, "h": ih},
                    "electrodes": {k: {"x": v[0], "y": v[1]} for k, v in s["electrodes"].items()},
                }
                combined["scenes"].append(entry)
                # Library sidecar next to the original image
                sidecar = img_path.with_suffix(".electrodes.json")
                try:
                    sidecar.write_text(json.dumps({
                        "image": img_path.name,
                        "image_size": {"w": iw, "h": ih},
                        "electrodes": entry["electrodes"],
                    }, indent=2), encoding="utf-8")
                except Exception as e:
                    print(f"Warning: couldn't write library sidecar {sidecar}: {e}")

            (proj_dir / f"{name}.electrodes.json").write_text(
                json.dumps(combined, indent=2), encoding="utf-8")
            self._post_status("Saved scenes and library sidecars", 0.02)

            # 2. Convert audio -> funscripts
            def convert_progress(frac, msg):
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
            funscripts = {ch: render_mod.load_funscript(
                proj_dir / f"{name}.{ch}.funscript")
                for ch in ["e1", "e2", "e3", "e4", "volume"]}

            # 4. Render video
            output_mp4 = proj_dir / f"{name}.mp4"

            def render_progress(frac, msg):
                self._post_status(msg, 0.40 + frac * 0.60)

            render_scenes = [{"image_path": s["path"], "electrodes": s["electrodes"]}
                             for s in scenes]

            render_mod.render_multi(
                scenes=render_scenes,
                funscripts=funscripts,
                audio=audio,
                output=output_mp4,
                fps=video["fps"],
                max_dim=video["max_dim"],
                duration_s=None,
                bloom_strength=video["bloom"],
                base_dim_range=(video["min_dim"], 1.0),
                scene_duration_s=scene_duration_s,
                crossfade_s=crossfade_s,
                progress=render_progress,
            )

            self._post_status(f"✓ Done. Project at {proj_dir}", 1.0, done=True,
                              final_folder=proj_dir)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._post_status(f"Error: {e}", self.progress_var.get(), done=True)

    def _post_status(self, msg, fraction, done=False, final_folder=None):
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
                            os.startfile(final_folder)
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
