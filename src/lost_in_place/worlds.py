"""Floor-texture worlds for the optical-flow experiments.

Each world is PX4's `default.sdf` with the ground visual replaced by a 40 x 40 m plane carrying
a multi-scale noise texture (about 1 cm per pixel, close to the flow camera's footprint at
1.5 m). Contrast is the knob: 1.0 is a well-textured floor, 0.03 is a near-featureless one.
Generation is deterministic for a given seed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from PIL import Image

FLOOR_SIZE_M = 40
TEXTURE_PX = 4096


@dataclass(frozen=True)
class FloorSpec:
    world: str
    contrast: float | None  # None = flat colour, no texture
    patch_m: float | None = None  # textured square of this size at the origin on a flat floor


FIXTURES = (
    FloorSpec("lip_textured", 1.0),
    FloorSpec("lip_lowtex", 0.03),
    FloorSpec("lip_flat", None),
    FloorSpec("lip_mixed", None, patch_m=6.0),
)


def noise_texture(seed: int, size: int = TEXTURE_PX) -> NDArray[np.float32]:
    """Sum of value-noise octaves (4 to 256 px cells), normalised to [0, 1]."""
    rng = np.random.default_rng(seed)
    img = np.zeros((size, size), np.float32)
    for cell, weight in ((4, 0.35), (16, 0.3), (64, 0.2), (256, 0.15)):
        grid = rng.random((size // cell, size // cell)).astype(np.float32)
        img += weight * np.kron(grid, np.ones((cell, cell), np.float32))
    img -= np.min(img)
    normalised: NDArray[np.float32] = img / np.max(img)
    return normalised


def apply_contrast(base: NDArray[np.float32], contrast: float) -> NDArray[np.uint8]:
    out = np.clip(0.5 + (base - 0.5) * contrast, 0.0, 1.0)
    pixels: NDArray[np.uint8] = (out * 255).astype(np.uint8)
    return pixels


def _visual(name: str, size_m: float, texture_uri: str | None, z: float = 0.0) -> str:
    pbr = ""
    if texture_uri:
        pbr = (
            f"<pbr><metal><albedo_map>{texture_uri}</albedo_map>"
            "<roughness>1.0</roughness><metalness>0.0</metalness></metal></pbr>"
        )
    return (
        f'<visual name="{name}"><pose>0 0 {z} 0 0 0</pose>'
        f"<geometry><plane><normal>0 0 1</normal><size>{size_m} {size_m}</size></plane></geometry>"
        "<material><ambient>0.8 0.8 0.8 1</ambient><diffuse>0.8 0.8 0.8 1</diffuse>"
        f"<specular>0 0 0 1</specular>{pbr}</material></visual>"
    )


def build_world_sdf(template: str, spec: FloorSpec, texture_uri: str | None) -> str:
    """Swap the ground visual of PX4's default world and rename the world. Shadows off."""
    start = template.index('<visual name="visual">')
    end = template.index("</visual>", start) + len("</visual>")
    floor_uri = texture_uri if spec.contrast is not None else None
    visuals = _visual("visual", FLOOR_SIZE_M, floor_uri)
    if spec.patch_m:
        visuals += _visual("patch", spec.patch_m, texture_uri, z=0.002)
    sdf = template[:start] + visuals + template[end:]
    sdf = sdf.replace('<world name="default">', f'<world name="{spec.world}">')
    return sdf.replace("<shadows>true</shadows>", "<shadows>false</shadows>")


def generate(
    px4_worlds: Path, out_dir: Path, seed: int = 0, specs: tuple[FloorSpec, ...] = FIXTURES
) -> list[Path]:
    """Write textures under `out_dir/models` and worlds under `out_dir`; returns world paths."""
    template = (px4_worlds / "default.sdf").read_text()
    base = noise_texture(seed)
    written = []
    for spec in specs:
        uri = None
        contrast = spec.contrast if spec.contrast is not None else (1.0 if spec.patch_m else None)
        if contrast is not None:
            model = f"{spec.world}_floor"
            tex_dir = out_dir / "models" / model / "materials" / "textures"
            tex_dir.mkdir(parents=True, exist_ok=True)
            Image.fromarray(apply_contrast(base, contrast)).convert("RGB").save(tex_dir / "floor.png")
            uri = f"model://{model}/materials/textures/floor.png"
        path = out_dir / f"{spec.world}.sdf"
        path.write_text(build_world_sdf(template, spec, uri))
        written.append(path)
    return written
