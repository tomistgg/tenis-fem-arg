from pathlib import Path

from PIL import Image

import build_deploy_site
import main


def test_deploy_photos_are_real_webp_with_bounded_dimensions(tmp_path):
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source = source_root / "Player" / "portrait.jpg"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (3200, 2000), (30, 120, 210)).save(source, format="JPEG", quality=95)

    copied = build_deploy_site._copy_optimized_photos(
        source_root,
        destination_root,
        max_edge=2400,
        quality=88,
    )

    output = destination_root / "Player" / "portrait.webp"
    assert copied == 1
    assert output.exists()
    with Image.open(output) as image:
        assert image.format == "WEBP"
        assert image.size == (2400, 1500)
        assert not image.info.get("exif")


def test_photo_manifest_points_legacy_source_names_to_webp(tmp_path, monkeypatch):
    photos_root = tmp_path / "photos"
    player_root = photos_root / "Player"
    player_root.mkdir(parents=True)
    (player_root / "one.jpeg").write_bytes(b"fixture")
    (player_root / "two.jpg").write_bytes(b"fixture")
    manifest_path = tmp_path / "photos_by_player.json"
    monkeypatch.setattr(main, "PHOTO_SLOTS_PER_PLAYER", 2)

    manifest = main.build_photos_by_player_manifest(manifest_path, photos_root)

    assert manifest == {
        "Player": [
            "photos/Player/one.webp",
            "photos/Player/two.webp",
        ]
    }
