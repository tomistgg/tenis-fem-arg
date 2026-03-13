import json
import os
import sys


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _iter_images(root):
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext in IMAGE_EXTS:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root)
                yield rel


def _album_from_rel(rel_path):
    parts = rel_path.replace("\\", "/").split("/")
    return parts[0] if parts else "Unsorted"


def build_manifest(root):
    photos = []
    for rel in _iter_images(root):
        rel_posix = rel.replace("\\", "/")
        album = _album_from_rel(rel_posix)
        photos.append({
            "public_id": rel_posix,
            "tournament": album,
            "players": [],
            "caption": "",
            "date": ""
        })
    return photos


def main():
    if len(sys.argv) != 3:
        print("Usage: python build_gallery_manifest.py <photos_root> <output_json>")
        sys.exit(2)

    photos_root = sys.argv[1]
    output_json = sys.argv[2]

    if not os.path.isdir(photos_root):
        print(f"Error: folder not found: {photos_root}")
        sys.exit(1)

    manifest = build_manifest(photos_root)
    os.makedirs(os.path.dirname(output_json), exist_ok=True)
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"Wrote {len(manifest)} photos to {output_json}")


if __name__ == "__main__":
    main()
