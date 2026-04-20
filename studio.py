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


# ------- Persistence: per-user config (last-used dirs + recent files) -------

CONFIG_PATH = Path.home() / ".foctave_studio.json"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"Could not save {CONFIG_PATH}: {e}")


PROJECT_VERSION = 1


LABELS = ["e1", "e2", "e3", "e4"]
COLORS = ["#ff4040", "#ffaa30", "#40ff60", "#40aaff"]
MARKER_RADIUS = 12
HIT_RADIUS = 18
PRESETS = foctave.PRESETS


def slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    return s.strip("_") or "project"


class Tooltip:
    """Lightweight hover-tooltip that attaches to any widget."""
    def __init__(self, widget, text: str, delay_ms: int = 350):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id = None
        self._tip = None
        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<ButtonPress>", self._on_leave, add="+")

    def _on_enter(self, _e):
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _on_leave(self, _e):
        self._cancel()
        self._hide()

    def _cancel(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self):
        if self._tip is not None or not self.widget.winfo_exists():
            return
        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        self._tip.configure(bg="#000")
        tk.Label(self._tip, text=self.text, bg="#222", fg="#eee",
                 relief="solid", borderwidth=1, padx=8, pady=4,
                 font=("Segoe UI", 9), justify="left",
                 wraplength=320).pack()

    def _hide(self):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


TOOLTIPS = {
    # Tuning
    "gamma": "Audio-envelope compression curve. Lower = punchier (more time "
             "pegged at peak). Higher = more dynamic (more time mid-level). "
             "0.30 matches FunBelgium-style saturation; 0.50 ≈ sqrt; 1.0 ≈ linear.",
    "percentile": "Normalization reference point. Lower = more saturation "
                  "(peaks clip to 100 sooner). 75 matches FunBelgium; "
                  "100 = peak-faithful. Use 95+ for dynamic preset.",
    "attack_ms": "How fast the envelope rises on a new transient. "
                 "0 = instant (symmetric smoothing). Try 10-30 ms for a "
                 "musical, punchy feel that still catches beats.",
    "release_ms": "How slowly the envelope decays after a peak. "
                  "0 = instant. Try 80-200 ms for a smoother, less choppy "
                  "feel that lets sustained passages breathe.",
    "floor": "Minimum intensity (0-1). 0.05 = never below 5%. Prevents "
             "quiet moments from dropping to zero, which can feel like "
             "'did the connection disconnect?'",
    "volume_ramp": "Add N %/minute linearly to the volume channel. 0.5 "
                   "matches the restim wiki's long-session recommendation. "
                   "Intensity builds gradually over time.",
    # Video
    "max_dim": "Largest side of the output video in pixels. Larger = higher "
               "quality but slower render. 720 is fast, 1280 balanced, "
               "1920+ is high quality.",
    "fps": "Frames per second. 30 is standard and fine for e-stim. "
           "24 gives a film feel; 60 is smoother but doubles render time.",
    "bloom": "Strength of the volume-driven base-image glow (0-1). The "
             "whole image brightens in sync with the volume envelope. "
             "0 = no bloom, 0.5 = moderate, 1 = strong.",
    "min_dim": "Base image brightness when audio is silent (0-1). "
               "1 = image always at full brightness; 0.5 = dims to half "
               "during quiet passages for more contrast.",
    "effect_opacity": "How opaque the electrode glow and ribbon are vs the "
                      "base image (0-1). Lower = base image 'scales' shine "
                      "through the glow. 0.55 is a balanced default; 0.3 "
                      "= subtle accent; 1.0 = full old behaviour.",
    # Scenes
    "scene_duration": "Seconds each image is shown before rotating to the "
                      "next. Ignored if only one image is in the list.",
    "crossfade": "Fade between scenes over this many seconds at each "
                 "rotation. Set to 0 (or uncheck) for hard cuts.",
}


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

        # Persistent per-user config (last dirs, recent projects)
        self.config = load_config()
        self.current_project_path: Path | None = None

        self.audio_path = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.project_name = tk.StringVar(value="untitled")
        self.preset_var = tk.StringVar(value="belgium")

        # Scenes: list[{"path": Path, "electrodes": dict, "overrides": {...}}]
        self.scenes: list[dict] = []
        self.active_scene_idx: int = -1
        # Per-scene effect opacity override (mirrored for the active scene)
        self.scene_opacity_override_enabled = tk.BooleanVar(value=False)
        self.scene_opacity_override_value = tk.DoubleVar(value=0.55)

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
            "effect_opacity": tk.DoubleVar(value=0.55),
        }
        self.scene_duration_var = tk.DoubleVar(value=20.0)
        self.crossfade_var = tk.DoubleVar(value=0.5)
        self.crossfade_enabled = tk.BooleanVar(value=True)

        self.status_var = tk.StringVar(value="Pick an audio track and add an image to begin.")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.render_busy = False

        self._build_menu()
        self._build_top()
        self._build_main()
        self._build_bottom()
        self._apply_preset()

        # Keyboard shortcuts for project file actions
        self.root.bind("<Control-n>", lambda e: self._project_new())
        self.root.bind("<Control-o>", lambda e: self._project_open())
        self.root.bind("<Control-s>", lambda e: self._project_save())
        self.root.bind("<Control-Shift-s>", lambda e: self._project_save_as())
        self.root.bind("<Control-S>", lambda e: self._project_save_as())

        self.ui_queue: queue.Queue = queue.Queue()
        self.root.after(50, self._drain_queue)

    # --- Menu ---

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="New project", accelerator="Ctrl+N",
                             command=self._project_new)
        filemenu.add_command(label="Open project…", accelerator="Ctrl+O",
                             command=self._project_open)
        filemenu.add_command(label="Save project", accelerator="Ctrl+S",
                             command=self._project_save)
        filemenu.add_command(label="Save project as…", accelerator="Ctrl+Shift+S",
                             command=self._project_save_as)
        filemenu.add_separator()
        filemenu.add_command(label="Quit", command=self.root.quit)
        menubar.add_cascade(label="File", menu=filemenu)
        self.root.config(menu=menubar)

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
        sd_spin = ttk.Spinbox(row, from_=1.0, to=600.0, increment=1.0,
                              textvariable=self.scene_duration_var,
                              format="%.1f", width=7)
        sd_spin.pack(side="left")
        tk.Label(row, text="sec", fg="#ddd", bg="#1a1a1a").pack(side="left", padx=4)
        Tooltip(sd_spin, TOOLTIPS["scene_duration"])

        row2 = tk.Frame(panel, bg="#1a1a1a")
        row2.pack(fill="x", padx=10, pady=2)
        cf_check = ttk.Checkbutton(row2, text="crossfade",
                                    variable=self.crossfade_enabled)
        cf_check.pack(side="left")
        cf_spin = ttk.Spinbox(row2, from_=0.0, to=3.0, increment=0.1,
                               textvariable=self.crossfade_var,
                               format="%.1f", width=6)
        cf_spin.pack(side="right")
        Tooltip(cf_check, TOOLTIPS["crossfade"])
        Tooltip(cf_spin, TOOLTIPS["crossfade"])

        ttk.Separator(panel, orient="horizontal").pack(fill="x", pady=6, padx=8)

        # Per-scene effect override
        tk.Label(panel, text="Per-scene effect", fg="#fff", bg="#1a1a1a",
                 font=("Arial", 10, "bold")).pack(anchor="w", padx=10)
        tk.Label(panel, text="Override global opacity\nfor the selected scene.",
                 fg="#888", bg="#1a1a1a", font=("Arial", 8),
                 justify="left").pack(anchor="w", padx=10, pady=(0, 2))
        ovr_row = tk.Frame(panel, bg="#1a1a1a")
        ovr_row.pack(fill="x", padx=10, pady=2)
        self._override_check = ttk.Checkbutton(
            ovr_row, text="override",
            variable=self.scene_opacity_override_enabled,
            command=self._on_scene_override_change)
        self._override_check.pack(side="left")
        self._override_spin = ttk.Spinbox(
            ovr_row, from_=0.0, to=1.0, increment=0.05,
            textvariable=self.scene_opacity_override_value,
            format="%.2f", width=6,
            command=self._on_scene_override_change)
        self._override_spin.pack(side="right")
        # Also update when the spinbox loses focus or is typed into
        self._override_spin.bind("<FocusOut>",
                                 lambda _e: self._on_scene_override_change())
        Tooltip(self._override_check,
                "When checked, this specific scene uses the opacity value "
                "on the right instead of the global effect opacity. Lets you "
                "dial each image independently.")
        Tooltip(self._override_spin,
                "Effect opacity for just this scene (0-1). Only used when the "
                "'override' checkbox is ticked.")

        ttk.Separator(panel, orient="horizontal").pack(fill="x", pady=6, padx=8)

        # Electrodes info
        self.electrode_count_label = tk.Label(panel, text="Electrodes: 0/4",
                                              fg="#ddd", bg="#1a1a1a")
        self.electrode_count_label.pack(anchor="w", padx=10, pady=2)
        ttk.Button(panel, text="Reset this scene's electrodes",
                   command=self._reset_electrodes).pack(fill="x", padx=10, pady=(2, 10))

    def _build_controls_panel(self, parent):
        panel = tk.Frame(parent, bg="#1e1e1e", width=340)
        panel.pack(side="right", fill="y")
        panel.pack_propagate(False)

        def _bg(w, fallback="#1e1e1e"):
            # ttk widgets don't expose -bg via cget; use a fallback.
            try:
                return w.cget("bg")
            except tk.TclError:
                return fallback

        def spin_row(parent, label, var, key, frm, to, inc, fmt="%.2f"):
            """Label + spinbox row. Attaches a tooltip based on `key`."""
            bg = _bg(parent)
            row = tk.Frame(parent, bg=bg)
            row.pack(fill="x", padx=10, pady=2)
            lbl = tk.Label(row, text=label, width=12, anchor="w",
                           fg="#ddd", bg=bg)
            lbl.pack(side="left")
            sb = ttk.Spinbox(row, from_=frm, to=to, increment=inc,
                             textvariable=var, format=fmt, width=8)
            sb.pack(side="right")
            if key in TOOLTIPS:
                Tooltip(lbl, TOOLTIPS[key])
                Tooltip(sb, TOOLTIPS[key])

        # ---- Card 1: Audio / signal processing ----
        card_tuning = ttk.LabelFrame(panel, text=" Signal processing ",
                                     padding=(6, 6))
        card_tuning.pack(fill="x", padx=8, pady=(10, 6))

        # Preset subsection
        tk.Label(card_tuning, text="Preset",
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 2))
        preset_frame = tk.Frame(card_tuning)
        preset_frame.pack(anchor="w", padx=4)
        for name in PRESETS.keys():
            rb = ttk.Radiobutton(preset_frame, text=name, value=name,
                                 variable=self.preset_var,
                                 command=self._apply_preset)
            rb.pack(anchor="w")

        # Tuning subsection
        ttk.Separator(card_tuning, orient="horizontal").pack(fill="x", pady=6)
        tk.Label(card_tuning, text="Tuning",
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 2))
        spin_row(card_tuning, "gamma", self.tune_vars["gamma"], "gamma",
                 0.1, 1.0, 0.05)
        spin_row(card_tuning, "percentile", self.tune_vars["percentile"],
                 "percentile", 50.0, 100.0, 1.0, "%.0f")
        spin_row(card_tuning, "attack ms", self.tune_vars["attack_ms"],
                 "attack_ms", 0.0, 200.0, 5.0, "%.0f")
        spin_row(card_tuning, "release ms", self.tune_vars["release_ms"],
                 "release_ms", 0.0, 500.0, 10.0, "%.0f")
        spin_row(card_tuning, "floor", self.tune_vars["floor"], "floor",
                 0.0, 0.30, 0.01)
        spin_row(card_tuning, "vol ramp %/min", self.tune_vars["volume_ramp"],
                 "volume_ramp", 0.0, 2.0, 0.1, "%.1f")

        # ---- Card 2: Video render ----
        card_video = ttk.LabelFrame(panel, text=" Video render ",
                                    padding=(6, 6))
        card_video.pack(fill="x", padx=8, pady=6)

        spin_row(card_video, "max dim (px)", self.video_vars["max_dim"],
                 "max_dim", 480, 3840, 160, "%.0f")
        spin_row(card_video, "fps", self.video_vars["fps"], "fps",
                 15, 60, 1, "%.0f")
        spin_row(card_video, "bloom", self.video_vars["bloom"], "bloom",
                 0.0, 1.0, 0.05)
        spin_row(card_video, "min dim (base)", self.video_vars["min_dim"],
                 "min_dim", 0.1, 1.0, 0.05)
        spin_row(card_video, "effect opacity", self.video_vars["effect_opacity"],
                 "effect_opacity", 0.0, 1.0, 0.05)

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

    # --- File browsers (with per-dialog last-used directories) ---

    def _dlg_initialdir(self, key: str) -> str | None:
        """Return a remembered directory for a given dialog key, or None."""
        last = self.config.get("last_dirs", {}).get(key)
        return last if last and Path(last).is_dir() else None

    def _dlg_remember(self, key: str, path: str | Path) -> None:
        d = str(Path(path).parent if Path(path).is_file() else Path(path))
        self.config.setdefault("last_dirs", {})[key] = d
        save_config(self.config)

    def _browse_audio(self):
        p = filedialog.askopenfilename(
            title="Select audio track",
            initialdir=self._dlg_initialdir("audio"),
            filetypes=[("Audio", "*.wav *.mp3 *.flac *.m4a *.ogg"), ("All", "*.*")])
        if p:
            self.audio_path.set(p)
            self._dlg_remember("audio", p)
            self._autofill_output()

    def _browse_output(self):
        p = filedialog.askdirectory(
            title="Select output folder",
            initialdir=self._dlg_initialdir("output"))
        if p:
            self.output_dir.set(p)
            self._dlg_remember("output", p)

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
            initialdir=self._dlg_initialdir("image"),
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
            self.scenes.append({"path": pth, "electrodes": electrodes, "overrides": {}})
        self._dlg_remember("image", paths[0])
        self._refresh_scene_list()
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
            self._sync_scene_override_ui()
            return
        s = self.scenes[self.active_scene_idx]
        # Ensure overrides dict exists (projects saved before this feature)
        s.setdefault("overrides", {})
        self.canvas_widget.set_image(s["path"], electrodes=s["electrodes"])
        self._sync_scene_override_ui()

    def _sync_scene_override_ui(self):
        """Reflect the active scene's overrides in the checkbox / spinbox."""
        if 0 <= self.active_scene_idx < len(self.scenes):
            ovr = self.scenes[self.active_scene_idx].get("overrides", {})
            if "effect_opacity" in ovr:
                self.scene_opacity_override_enabled.set(True)
                self.scene_opacity_override_value.set(float(ovr["effect_opacity"]))
            else:
                self.scene_opacity_override_enabled.set(False)
                # Mirror the global value as the starting point for editing
                self.scene_opacity_override_value.set(
                    float(self.video_vars["effect_opacity"].get()))
        else:
            self.scene_opacity_override_enabled.set(False)

    def _on_scene_override_change(self):
        """Called when the scene override checkbox or spinbox changes."""
        if self.active_scene_idx < 0 or self.active_scene_idx >= len(self.scenes):
            return
        s = self.scenes[self.active_scene_idx]
        s.setdefault("overrides", {})
        if self.scene_opacity_override_enabled.get():
            try:
                s["overrides"]["effect_opacity"] = float(self.scene_opacity_override_value.get())
            except (tk.TclError, ValueError):
                pass
        else:
            s["overrides"].pop("effect_opacity", None)

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

        scenes_snapshot = [{"path": Path(s["path"]),
                            "electrodes": dict(s["electrodes"]),
                            "overrides": dict(s.get("overrides", {}))}
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

            render_scenes = [{"image_path": s["path"],
                              "electrodes": s["electrodes"],
                              "overrides": s.get("overrides", {})}
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
                effect_opacity=video["effect_opacity"],
                progress=render_progress,
            )

            self._post_status(f"✓ Done. Project at {proj_dir}", 1.0, done=True,
                              final_folder=proj_dir)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._post_status(f"Error: {e}", self.progress_var.get(), done=True)

    # --- Project save / load ---

    def _project_new(self):
        if self.scenes or self.audio_path.get():
            if not messagebox.askyesno(
                "New project",
                "Discard the current project and start fresh?"):
                return
        self.current_project_path = None
        self.project_name.set("untitled")
        self.audio_path.set("")
        self.output_dir.set("")
        self.scenes = []
        self.active_scene_idx = -1
        self._refresh_scene_list()
        self.canvas_widget.set_image(None)
        self._sync_scene_override_ui()
        self.preset_var.set("belgium")
        self._apply_preset()
        # Reset video vars to their construction defaults (matching __init__)
        self.video_vars["max_dim"].set(1280)
        self.video_vars["fps"].set(30)
        self.video_vars["bloom"].set(0.45)
        self.video_vars["min_dim"].set(0.55)
        self.video_vars["effect_opacity"].set(0.55)
        self.scene_duration_var.set(20.0)
        self.crossfade_enabled.set(True)
        self.crossfade_var.set(0.5)
        self.status_var.set("New project.")
        self.root.title("FOCtave Studio")

    def _project_open(self):
        p = filedialog.askopenfilename(
            title="Open FOCtave project",
            initialdir=self._dlg_initialdir("project"),
            filetypes=[("FOCtave project", "*.foctave.json"),
                       ("JSON", "*.json"), ("All", "*.*")])
        if not p:
            return
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception as e:
            messagebox.showerror("Open failed", f"Could not read project: {e}")
            return
        self._apply_project_data(data)
        self.current_project_path = Path(p).resolve()
        self._dlg_remember("project", p)
        self.status_var.set(f"Loaded {Path(p).name}")
        self.root.title(f"FOCtave Studio — {Path(p).name}")

    def _project_save(self):
        if self.current_project_path is None:
            return self._project_save_as()
        self._write_project_to(self.current_project_path)

    def _project_save_as(self):
        default_name = slug(self.project_name.get()) + ".foctave.json"
        p = filedialog.asksaveasfilename(
            title="Save FOCtave project",
            initialdir=self._dlg_initialdir("project"),
            initialfile=default_name,
            defaultextension=".foctave.json",
            filetypes=[("FOCtave project", "*.foctave.json"),
                       ("JSON", "*.json"), ("All", "*.*")])
        if not p:
            return
        self.current_project_path = Path(p).resolve()
        self._dlg_remember("project", p)
        self._write_project_to(self.current_project_path)

    def _write_project_to(self, path: Path):
        # Ensure any in-flight canvas edits are captured
        if 0 <= self.active_scene_idx < len(self.scenes):
            self.scenes[self.active_scene_idx]["electrodes"] = \
                self.canvas_widget.get_electrodes()
        data = self._collect_project_data()
        try:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            self.status_var.set(f"Saved {path.name}")
            self.root.title(f"FOCtave Studio — {path.name}")
        except Exception as e:
            messagebox.showerror("Save failed", f"Could not write project: {e}")

    def _collect_project_data(self) -> dict:
        return {
            "version": PROJECT_VERSION,
            "project_name": self.project_name.get(),
            "audio_path": self.audio_path.get(),
            "output_dir": self.output_dir.get(),
            "preset": self.preset_var.get(),
            "tune": {k: float(v.get()) for k, v in self.tune_vars.items()},
            "video": {k: (int(v.get()) if isinstance(v, tk.IntVar) else float(v.get()))
                      for k, v in self.video_vars.items()},
            "scene_duration_s": float(self.scene_duration_var.get()),
            "crossfade_enabled": bool(self.crossfade_enabled.get()),
            "crossfade_s": float(self.crossfade_var.get()),
            "scenes": [{
                "path": str(s["path"]),
                "electrodes": {k: {"x": v[0], "y": v[1]} for k, v in s["electrodes"].items()},
                "overrides": dict(s.get("overrides", {})),
            } for s in self.scenes],
        }

    def _apply_project_data(self, data: dict):
        self.project_name.set(data.get("project_name", "untitled"))
        self.audio_path.set(data.get("audio_path", ""))
        self.output_dir.set(data.get("output_dir", ""))

        for k, v in data.get("tune", {}).items():
            if k in self.tune_vars:
                try:
                    self.tune_vars[k].set(float(v))
                except Exception:
                    pass
        for k, v in data.get("video", {}).items():
            if k in self.video_vars:
                try:
                    self.video_vars[k].set(v)
                except Exception:
                    pass
        if "preset" in data:
            self.preset_var.set(data["preset"])
            # Don't re-apply preset defaults; tune values above win
        self.scene_duration_var.set(float(data.get("scene_duration_s", 20.0)))
        self.crossfade_enabled.set(bool(data.get("crossfade_enabled", True)))
        self.crossfade_var.set(float(data.get("crossfade_s", 0.5)))

        self.scenes = []
        missing = []
        for s in data.get("scenes", []):
            path = Path(s.get("path", ""))
            if not path.exists():
                missing.append(str(path))
                continue
            electrodes = {}
            for lab, pos in s.get("electrodes", {}).items():
                if lab in LABELS:
                    electrodes[lab] = (int(pos["x"]), int(pos["y"]))
            self.scenes.append({
                "path": path.resolve(),
                "electrodes": electrodes,
                "overrides": dict(s.get("overrides", {})),
            })
        self.active_scene_idx = 0 if self.scenes else -1
        self._refresh_scene_list()
        self._load_active_scene()

        if missing:
            preview = "\n".join(missing[:5])
            more = "" if len(missing) <= 5 else f"\n...and {len(missing)-5} more"
            messagebox.showwarning(
                "Missing images",
                f"{len(missing)} image(s) from this project could not be found:\n\n"
                f"{preview}{more}")

    # --- Status queue plumbing ---

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
