"""
FOCtave - GUI for placing electrode positions on a still image.

Open an image, click to place e1..e4 in order, drag any placed electrode to
reposition, right-click to delete, and save. Existing <image>.electrodes.json
files are auto-loaded when you open the matching image, so you can tweak a
previous placement without starting over.

Usage:
    python place.py                        # launch with no image loaded
    python place.py path/to/image.jpg      # open image at launch

Keyboard:
    O    Open image
    S    Save placement
    R    Reset (clear all placed electrodes)
"""

import argparse
import json
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk


LABELS = ["e1", "e2", "e3", "e4"]
COLORS = ["#ff4040", "#ffaa30", "#40ff60", "#40aaff"]
MARKER_RADIUS = 12
HIT_RADIUS = 18  # px; how close a click has to be to grab an electrode


class PlaceApp:
    def __init__(self, root: tk.Tk, initial_image: Path | None = None):
        self.root = root
        self.root.title("FOCtave — Place Electrodes")
        self.root.geometry("1200x850")
        self.root.minsize(600, 500)

        self.image_path: Path | None = None
        self.image: Image.Image | None = None
        self.photo: ImageTk.PhotoImage | None = None
        self.image_id: int | None = None
        self.scale: float = 1.0
        self.img_offset_x: int = 0
        self.img_offset_y: int = 0

        self.electrodes: dict[str, tuple[int, int]] = {}  # label -> image coords
        self.drag_target: str | None = None

        self._build_menu()
        self._build_toolbar()
        self._build_canvas()
        self._build_statusbar()

        # Redraw on window resize (image re-fits)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.root.bind("<Key>", self._on_key)

        if initial_image:
            # Delay load until the window is drawn so canvas has real size
            self.root.after(50, lambda: self.load_image(Path(initial_image)))

    # ---------- UI construction ----------

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Open image...", accelerator="O", command=self.open_dialog)
        filemenu.add_command(label="Save placement", accelerator="S", command=self.save)
        filemenu.add_separator()
        filemenu.add_command(label="Reset", accelerator="R", command=self.reset)
        filemenu.add_separator()
        filemenu.add_command(label="Quit", command=self.root.quit)
        menubar.add_cascade(label="File", menu=filemenu)
        self.root.config(menu=menubar)

    def _build_toolbar(self):
        bar = tk.Frame(self.root, bg="#1e1e1e")
        bar.pack(side="top", fill="x")
        self.image_label = tk.Label(bar, text="(no image loaded)",
                                    fg="#cccccc", bg="#1e1e1e",
                                    font=("Arial", 10), anchor="w")
        self.image_label.pack(side="left", padx=8, pady=6)
        ttk.Button(bar, text="Open...", command=self.open_dialog).pack(side="right", padx=3, pady=4)
        ttk.Button(bar, text="Save", command=self.save).pack(side="right", padx=3, pady=4)
        ttk.Button(bar, text="Reset", command=self.reset).pack(side="right", padx=3, pady=4)

    def _build_canvas(self):
        frame = tk.Frame(self.root, bg="#111")
        frame.pack(side="top", fill="both", expand=True)
        self.canvas = tk.Canvas(frame, bg="#111", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._on_left_click)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind("<Motion>", self._on_mouse_move)

    def _build_statusbar(self):
        bar = tk.Frame(self.root, bd=1, relief="sunken", bg="#222")
        bar.pack(side="bottom", fill="x")
        self.status = tk.Label(bar, text="Open an image to start. (File > Open or press O)",
                               anchor="w", fg="#ddd", bg="#222")
        self.status.pack(side="left", fill="x", expand=True, padx=6, pady=2)
        self.coords = tk.Label(bar, text="", anchor="e", fg="#888", bg="#222",
                               font=("Consolas", 9))
        self.coords.pack(side="right", padx=6)
        self.count = tk.Label(bar, text="0/4 placed", anchor="e", fg="#ddd", bg="#222")
        self.count.pack(side="right", padx=10)

    # ---------- Coordinate mapping ----------

    def img_to_canvas(self, ix: int, iy: int) -> tuple[int, int]:
        return (int(ix * self.scale) + self.img_offset_x,
                int(iy * self.scale) + self.img_offset_y)

    def canvas_to_img(self, cx: int, cy: int) -> tuple[int, int]:
        if self.image is None or self.scale <= 0:
            return (0, 0)
        ix = int(round((cx - self.img_offset_x) / self.scale))
        iy = int(round((cy - self.img_offset_y) / self.scale))
        iw, ih = self.image.size
        return (max(0, min(iw - 1, ix)), max(0, min(ih - 1, iy)))

    def _click_inside_image(self, cx: int, cy: int) -> bool:
        if self.image is None:
            return False
        iw, ih = self.image.size
        dw, dh = int(iw * self.scale), int(ih * self.scale)
        return (self.img_offset_x <= cx < self.img_offset_x + dw
                and self.img_offset_y <= cy < self.img_offset_y + dh)

    # ---------- Image loading ----------

    def open_dialog(self):
        path = filedialog.askopenfilename(
            title="Select image",
            filetypes=[("Images", "*.jpg *.jpeg *.png *.webp *.bmp"), ("All files", "*.*")],
        )
        if path:
            self.load_image(Path(path))

    def load_image(self, path: Path):
        try:
            img = Image.open(path).convert("RGB")
        except Exception as e:
            messagebox.showerror("Could not open image", str(e))
            return
        self.image_path = path.resolve()
        self.image = img
        self.image_label.config(text=str(path.name))
        self._fit_image_to_canvas()

        # Auto-load existing placement if present
        ep = self.image_path.with_suffix(".electrodes.json")
        self.electrodes = {}
        if ep.exists():
            try:
                data = json.loads(ep.read_text(encoding="utf-8"))
                for label, pos in data.get("electrodes", {}).items():
                    if label in LABELS and "x" in pos and "y" in pos:
                        self.electrodes[label] = (int(pos["x"]), int(pos["y"]))
                self.status.config(text=f"Loaded existing placement from {ep.name} "
                                        f"({len(self.electrodes)}/4). Drag to adjust.")
            except Exception as e:
                self.status.config(text=f"Couldn't read {ep.name}: {e}")
        else:
            self.status.config(text="Click to place e1. Drag existing points to move, "
                                    "right-click to delete.")
        self._redraw_markers()

    def _fit_image_to_canvas(self):
        if self.image is None:
            return
        iw, ih = self.image.size
        self.canvas.update_idletasks()
        cw = max(100, self.canvas.winfo_width())
        ch = max(100, self.canvas.winfo_height())
        self.scale = min(cw / iw, ch / ih, 1.0)
        dw, dh = int(iw * self.scale), int(ih * self.scale)
        self.img_offset_x = (cw - dw) // 2
        self.img_offset_y = (ch - dh) // 2

        disp = self.image.resize((dw, dh), Image.LANCZOS) if self.scale < 1.0 else self.image
        self.photo = ImageTk.PhotoImage(disp)
        self.canvas.delete("image")
        self.image_id = self.canvas.create_image(self.img_offset_x, self.img_offset_y,
                                                 anchor="nw", image=self.photo, tags=("image",))
        self.canvas.tag_lower("image")  # keep markers on top

    def _on_canvas_resize(self, _event):
        if self.image:
            self._fit_image_to_canvas()
            self._redraw_markers()

    # ---------- Marker rendering ----------

    def _redraw_markers(self):
        self.canvas.delete("marker")
        for i, label in enumerate(LABELS):
            if label not in self.electrodes:
                continue
            ix, iy = self.electrodes[label]
            cx, cy = self.img_to_canvas(ix, iy)
            color = COLORS[i]
            self.canvas.create_oval(cx - MARKER_RADIUS, cy - MARKER_RADIUS,
                                    cx + MARKER_RADIUS, cy + MARKER_RADIUS,
                                    outline=color, width=3, tags=("marker", label))
            self.canvas.create_oval(cx - 3, cy - 3, cx + 3, cy + 3,
                                    fill=color, outline="", tags=("marker", label))
            self.canvas.create_text(cx + MARKER_RADIUS + 4, cy, text=label,
                                    fill=color, font=("Arial", 13, "bold"),
                                    anchor="w", tags=("marker", label))
        # Draw a faint polyline connecting them in order, preview of the ribbon path
        if len(self.electrodes) >= 2:
            pts = []
            for label in LABELS:
                if label in self.electrodes:
                    pts.append(self.img_to_canvas(*self.electrodes[label]))
            for i in range(len(pts) - 1):
                self.canvas.create_line(*pts[i], *pts[i + 1],
                                        fill="#888", width=1, dash=(3, 3),
                                        tags=("marker",))
        self.count.config(text=f"{len(self.electrodes)}/4 placed")

    # ---------- Interactions ----------

    def _find_electrode_at(self, cx: int, cy: int) -> str | None:
        for label, (ix, iy) in self.electrodes.items():
            ex, ey = self.img_to_canvas(ix, iy)
            if (cx - ex) ** 2 + (cy - ey) ** 2 <= HIT_RADIUS ** 2:
                return label
        return None

    def _next_unplaced_label(self) -> str | None:
        for label in LABELS:
            if label not in self.electrodes:
                return label
        return None

    def _on_left_click(self, event):
        if not self.image:
            return
        hit = self._find_electrode_at(event.x, event.y)
        if hit:
            self.drag_target = hit
            self.canvas.config(cursor="fleur")
            return
        if not self._click_inside_image(event.x, event.y):
            return
        next_label = self._next_unplaced_label()
        if next_label is None:
            self.status.config(text="All 4 placed. Drag to move, right-click to delete, "
                                    "or Reset to start over.")
            return
        ix, iy = self.canvas_to_img(event.x, event.y)
        self.electrodes[next_label] = (ix, iy)
        self._redraw_markers()
        self.status.config(text=f"Placed {next_label} at ({ix}, {iy}). "
                                f"Next: {self._next_unplaced_label() or 'save when ready'}")

    def _on_drag(self, event):
        if not self.drag_target or not self.image:
            return
        ix, iy = self.canvas_to_img(event.x, event.y)
        self.electrodes[self.drag_target] = (ix, iy)
        self._redraw_markers()
        self.status.config(text=f"Moving {self.drag_target} to ({ix}, {iy})")

    def _on_release(self, _event):
        if self.drag_target:
            pos = self.electrodes.get(self.drag_target)
            self.status.config(text=f"{self.drag_target} set to {pos}")
            self.drag_target = None
            self.canvas.config(cursor="crosshair")

    def _on_right_click(self, event):
        hit = self._find_electrode_at(event.x, event.y)
        if hit:
            del self.electrodes[hit]
            self._redraw_markers()
            self.status.config(text=f"Removed {hit}. Click to place it again.")

    def _on_mouse_move(self, event):
        if not self.image or not self._click_inside_image(event.x, event.y):
            self.coords.config(text="")
            return
        ix, iy = self.canvas_to_img(event.x, event.y)
        self.coords.config(text=f"({ix}, {iy})")

    def _on_key(self, event):
        key = event.keysym.lower()
        if key == "s":
            self.save()
        elif key == "o":
            self.open_dialog()
        elif key == "r":
            self.reset()

    # ---------- Actions ----------

    def reset(self):
        if not self.electrodes:
            return
        if not messagebox.askyesno("Reset", "Clear all placed electrodes?"):
            return
        self.electrodes = {}
        self._redraw_markers()
        self.status.config(text="Reset. Click to place e1.")

    def save(self):
        if not self.image_path or not self.image:
            messagebox.showwarning("No image", "Open an image first.")
            return
        if len(self.electrodes) != 4:
            if not messagebox.askyesno(
                "Incomplete placement",
                f"Only {len(self.electrodes)}/4 electrodes placed. Save anyway?"):
                return
        iw, ih = self.image.size
        payload = {
            "image": self.image_path.name,
            "image_size": {"w": iw, "h": ih},
            "electrodes": {k: {"x": v[0], "y": v[1]} for k, v in self.electrodes.items()},
        }
        out = self.image_path.with_suffix(".electrodes.json")
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.status.config(text=f"Saved to {out.name}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", nargs="?", type=Path, default=None,
                    help="Optional image to open at launch")
    args = ap.parse_args()

    if args.image and not args.image.exists():
        print(f"Not found: {args.image}", file=sys.stderr)
        return 1

    root = tk.Tk()
    PlaceApp(root, initial_image=args.image)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
