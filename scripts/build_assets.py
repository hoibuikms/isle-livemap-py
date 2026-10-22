"""Dev-only: derive the bundled map texture + zone data from the upstream assets.

Usage:  python -I scripts/build_assets.py <upstream Assets dir> <output assets dir>
Needs Pillow (dev dependency, not required at runtime).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image

Image.MAX_IMAGE_PIXELS = 100_000_000
TEXTURE_WIDTH = 3072


def round_points(points: list[dict]) -> list[list[float]]:
    return [[round(p["left"], 5), round(p["top"], 5)] for p in points]


def main(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    with Image.open(src / "GatewayMap.jpg") as img:
        w, h = img.size
        out = img.convert("RGB").resize((TEXTURE_WIDTH, round(TEXTURE_WIDTH * h / w)), Image.LANCZOS)
        out.save(dst / "gateway_map.jpg", quality=80, optimize=True)
    doc = json.loads((src / "GatewayMapLayers.json").read_text(encoding="utf-8"))
    zones = {"migration": [], "patrol": [], "food": []}
    for z in doc["zones"]:
        if z["kind"] in ("migration", "patrol"):
            zones[z["kind"]].append({"name": z["name"], "points": round_points(z["points"])})
    # The 13 default "food source" animal categories of the upstream map data.
    food_keys = doc["defaults"]["selectedResourceKeys"]["animals"]
    names: dict[str, str] = {}
    pts: dict[str, list] = {k: [] for k in food_keys}
    for r in doc["resources"]:
        if r["category"] == "animals" and r["key"] in pts:
            names[r["key"]] = r["name"]
            pts[r["key"]].append([round(r["point"]["left"], 5), round(r["point"]["top"], 5)])
    zones["food"] = [{"key": k, "name": names.get(k, k), "points": pts[k]} for k in food_keys]
    zones["source"] = "Derived from myislemap.com map data snapshot via Undo-IsleLiveMap (MIT)."
    (dst / "zones.json").write_text(json.dumps(zones, separators=(",", ":")), encoding="utf-8")
    print({k: len(v) for k, v in zones.items() if isinstance(v, list)})


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
