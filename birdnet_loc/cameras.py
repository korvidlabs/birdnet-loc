"""Camera placements, loaded from cameras.yaml in MAP_DIR.

Pixel origin is top-left, north is up, so the unit vector for a compass bearing
b (0 = N, 90 = E) is (sin b, -cos b) in the pixel/metre frame used here.
Metres are pixels scaled by `scale_m_per_px`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import yaml


@dataclass(frozen=True)
class Camera:
    name: str
    x_px: float
    y_px: float
    bearing: float          # compass degrees
    fov_deg: float
    scale_m_per_px: float

    @property
    def x_m(self) -> float:
        return self.x_px * self.scale_m_per_px

    @property
    def y_m(self) -> float:
        return self.y_px * self.scale_m_per_px

    @property
    def unit(self) -> tuple[float, float]:
        """Unit vector along the camera's bearing, in the y-down map frame."""
        rad = math.radians(self.bearing)
        return (math.sin(rad), -math.cos(rad))

    def in_field(self, x_m: float, y_m: float) -> bool:
        """True when (x_m, y_m) falls inside the camera's fov, centred on its bearing."""
        dx, dy = x_m - self.x_m, y_m - self.y_m
        if dx == 0.0 and dy == 0.0:
            return True
        ux, uy = self.unit
        norm = math.hypot(dx, dy)
        cos_a = max(-1.0, min(1.0, (dx * ux + dy * uy) / norm))
        return math.degrees(math.acos(cos_a)) <= self.fov_deg / 2.0


@dataclass(frozen=True)
class CameraSet:
    cameras: dict[str, Camera]
    image: str
    image_size: tuple[int, int]
    scale_m_per_px: float

    def get(self, name: str) -> Camera | None:
        return self.cameras.get(name)

    def __iter__(self):
        return iter(self.cameras.values())

    def __len__(self) -> int:
        return len(self.cameras)


def load_cameras(path: str) -> CameraSet:
    with open(path, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    scale = float(doc["scale_m_per_px"])
    size = tuple(int(v) for v in doc.get("image_size", [1071, 1008]))
    cams: dict[str, Camera] = {}
    for entry in doc.get("cameras", []):
        name = str(entry["name"]).strip()
        px = entry["px"]
        cams[name] = Camera(
            name=name,
            x_px=float(px[0]),
            y_px=float(px[1]),
            bearing=float(entry["bearing"]),
            fov_deg=float(entry.get("fov_deg", 180.0)),
            scale_m_per_px=scale,
        )
    return CameraSet(
        cameras=cams,
        image=str(doc.get("image", "map.png")),
        image_size=(size[0], size[1]),
        scale_m_per_px=scale,
    )
