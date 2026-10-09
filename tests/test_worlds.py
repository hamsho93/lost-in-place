from pathlib import Path

import numpy as np

from lost_in_place.worlds import FloorSpec, apply_contrast, build_world_sdf, generate, noise_texture

TEMPLATE = """<sdf version="1.9"><world name="default"><scene><shadows>true</shadows></scene>
<model name="ground_plane"><link name="link"><visual name="visual"><geometry><plane/></geometry></visual>
</link></model></world></sdf>"""


def test_noise_texture_is_deterministic_and_normalised() -> None:
    a, b = noise_texture(3, size=256), noise_texture(3, size=256)
    assert np.array_equal(a, b)
    assert a.min() == 0.0 and a.max() == 1.0


def test_low_contrast_has_few_grey_levels() -> None:
    base = noise_texture(0, size=256)
    assert len(np.unique(apply_contrast(base, 0.03))) <= 9
    assert apply_contrast(base, 1.0).std() > 30


def test_world_sdf_swaps_floor_and_disables_shadows() -> None:
    sdf = build_world_sdf(TEMPLATE, FloorSpec("lip_test", 1.0), "model://x/floor.png")
    assert '<world name="lip_test">' in sdf
    assert "<shadows>false</shadows>" in sdf
    assert "model://x/floor.png" in sdf and "<size>40 40</size>" in sdf


def test_flat_floor_has_no_texture_and_patch_is_added() -> None:
    flat = build_world_sdf(TEMPLATE, FloorSpec("f", None), None)
    assert "albedo_map" not in flat
    mixed = build_world_sdf(TEMPLATE, FloorSpec("m", None, patch_m=6.0), "model://p.png")
    assert mixed.count("albedo_map") == 2 and 'name="patch"' in mixed  # open + close tags of one map


def test_generate_writes_worlds_and_textures(tmp_path: Path) -> None:
    (tmp_path / "px4").mkdir()
    (tmp_path / "px4" / "default.sdf").write_text(TEMPLATE)
    specs = (FloorSpec("lip_a", 0.5), FloorSpec("lip_b", None))
    out = generate(tmp_path / "px4", tmp_path / "out", specs=specs)
    assert [p.name for p in out] == ["lip_a.sdf", "lip_b.sdf"]
    assert (tmp_path / "out" / "models" / "lip_a_floor" / "materials" / "textures" / "floor.png").exists()
    assert not (tmp_path / "out" / "models" / "lip_b_floor").exists()
