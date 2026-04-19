"""
Click-to-place electrode positions on a still image.

Open the image in a window, click 4 times in order (e1, e2, e3, e4),
and the positions get saved alongside the image as
<image_stem>.electrodes.json, ready to feed into render.py.

Usage:
    python place.py path/to/image.jpg
    python place.py path/to/image.png --out custom.electrodes.json
"""

import argparse
import json
import sys
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageTk

MAX_DISPLAY = 1200
LABELS = ["e1", "e2", "e3", "e4"]
COLORS = ["#ff4040", "#ffaa30", "#40ff60", "#40aaff"]  # red / orange / green / blue


def run(image_path: Path, out_path: Path) -> int:
    img = Image.open(image_path).convert("RGB")
    iw, ih = img.size

    # Scale for display
    scale = min(MAX_DISPLAY / iw, MAX_DISPLAY / ih, 1.0)
    dw, dh = int(iw * scale), int(ih * scale)
    disp = img.resize((dw, dh), Image.LANCZOS) if scale < 1.0 else img

    root = tk.Tk()
    root.title(f"Click {LABELS[0]} position  (1/4)")

    photo = ImageTk.PhotoImage(disp)
    canvas = tk.Canvas(root, width=dw, height=dh, cursor="crosshair", highlightthickness=0)
    canvas.pack()
    canvas.create_image(0, 0, anchor="nw", image=photo)

    positions: list[tuple[int, int]] = []
    marker_ids: list[list[int]] = []  # each inner list = ids for undo

    def redraw_title():
        n = len(positions)
        if n < 4:
            root.title(f"Click {LABELS[n]} position  ({n + 1}/4)  —  right-click to undo")
        else:
            root.title("All 4 placed — saving and closing")

    def on_click(event):
        if len(positions) >= 4:
            return
        idx = len(positions)
        # Image coords (unscaled)
        ix = int(round(event.x / scale))
        iy = int(round(event.y / scale))
        positions.append((ix, iy))

        ids = []
        r = 10
        ids.append(canvas.create_oval(event.x - r, event.y - r, event.x + r, event.y + r,
                                      outline=COLORS[idx], width=3))
        ids.append(canvas.create_oval(event.x - 2, event.y - 2, event.x + 2, event.y + 2,
                                      fill=COLORS[idx], outline=""))
        ids.append(canvas.create_text(event.x + r + 4, event.y,
                                      text=LABELS[idx], fill=COLORS[idx],
                                      font=("Arial", 14, "bold"), anchor="w"))
        marker_ids.append(ids)
        redraw_title()

        if len(positions) == 4:
            root.after(400, root.destroy)

    def on_right_click(event):
        if not positions:
            return
        positions.pop()
        for mid in marker_ids.pop():
            canvas.delete(mid)
        redraw_title()

    canvas.bind("<Button-1>", on_click)
    canvas.bind("<Button-3>", on_right_click)  # right click undo
    root.mainloop()

    if len(positions) != 4:
        print("Cancelled — no file written.")
        return 1

    payload = {
        "image": image_path.name,
        "image_size": {"w": iw, "h": ih},
        "electrodes": {LABELS[i]: {"x": positions[i][0], "y": positions[i][1]} for i in range(4)},
    }
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"Saved {out_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", type=Path, help="Path to a still image (JPG/PNG)")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output JSON path (default: <image>.electrodes.json)")
    args = ap.parse_args()

    img_path = args.image.resolve()
    if not img_path.exists():
        print(f"Not found: {img_path}", file=sys.stderr)
        return 1

    out_path = args.out or img_path.with_suffix(".electrodes.json")
    return run(img_path, out_path.resolve())


if __name__ == "__main__":
    sys.exit(main())
