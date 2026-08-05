import argparse
import os
import shutil
import uuid
from pathlib import Path

from PIL import Image, ImageOps
from runtime_paths import DATA_DIR, SITE_ROOT


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = BASE_DIR / ".site"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
COPY_ROOT_FILES = ("app.html", "index.html", "404.html", "CNAME", "site.webmanifest")
APPLE_TOUCH_ICON_FALLBACKS = (
    "apple-touch-icon.png",
    "apple-touch-icon-precomposed.png",
    "apple-touch-icon-180x180.png",
)
COPY_ROOT_DIRS = (
    "assets",
    "calendar",
    "draws",
    "entrylists",
    "fedbcup",
    "history",
    "rankings",
    "roadtogs",
    "tstrength",
    "upcoming",
)
COPY_DATA_FILES = (
    "history_data_bundle.js",
    "player_aliases_wta_itf_bundle.js",
    "photos_by_player.json",
)
COPY_DATA_GLOBS = (
    "wta_rankings_latest_bundle.js",
    "wta_rankings_[0-9][0-9][0-9][0-9]_bundle.js",
)
COPY_DATA_DIRS = ("flags",)
COPY_ROUTE_FILES = ("photos/index.html",)
DEFAULT_MAX_EDGE = 2400
DEFAULT_QUALITY = 88


def _ensure_within_base(path):
    resolved = path.resolve()
    try:
        resolved.relative_to(BASE_DIR)
    except ValueError as exc:
        raise ValueError(f"Refusing to write outside repo root: {resolved}") from exc
    return resolved


def _prepare_output_dir(output_dir):
    output_dir = _ensure_within_base(output_dir)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    return output_dir


def _copy_file(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _copy_tree(src_dir, dst_dir):
    if not src_dir.exists():
        return 0
    copied = 0
    for src in src_dir.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(src_dir)
        _copy_file(src, dst_dir / rel)
        copied += 1
    return copied


def _copy_deploy_data(output_dir):
    src_root = DATA_DIR
    dst_root = output_dir / "data"
    if not src_root.exists():
        return 0

    copied = 0
    for filename in COPY_DATA_FILES:
        src = src_root / filename
        if src.exists():
            _copy_file(src, dst_root / filename)
            copied += 1

    copied_ranking_files = set()
    for pattern in COPY_DATA_GLOBS:
        for src in sorted(src_root.glob(pattern)):
            if not src.is_file() or src.name in copied_ranking_files:
                continue
            _copy_file(src, dst_root / src.name)
            copied_ranking_files.add(src.name)
            copied += 1

    for dirname in COPY_DATA_DIRS:
        copied += _copy_tree(src_root / dirname, dst_root / dirname)
    return copied


def _save_webp_copy(src, dst, max_edge, quality):
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as img:
        icc_profile = img.info.get("icc_profile")
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            if "A" in img.getbands():
                bg = Image.new("RGB", img.size, (255, 255, 255))
                alpha = img.getchannel("A")
                bg.paste(img.convert("RGBA"), mask=alpha)
                img = bg
            else:
                img = img.convert("RGB")
        if max(img.size) > max_edge:
            img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        elif img.mode != "RGB":
            img = img.convert("RGB")

        save_kwargs = {
            "format": "WEBP",
            "quality": quality,
            "method": 6,
        }
        # EXIF can contain GPS, device, and editing metadata. Orientation has
        # already been baked in by exif_transpose(), so deploy copies drop EXIF.
        if icc_profile:
            save_kwargs["icc_profile"] = icc_profile
        img.save(dst, **save_kwargs)


def _copy_optimized_photos(src_root, dst_root, max_edge, quality):
    if not src_root.exists():
        return 0

    copied = 0
    for src in src_root.rglob("*"):
        if src.is_dir():
            continue
        if src.suffix.lower() not in IMAGE_EXTS:
            continue
        rel = src.relative_to(src_root)
        dst = (dst_root / rel).with_suffix(".webp")
        dst.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as img:
            can_copy = (
                src.suffix.lower() == ".webp"
                and max(img.size) <= max_edge
                and not img.info.get("exif")
            )
        if can_copy:
            _copy_file(src, dst)
        else:
            _save_webp_copy(src, dst, max_edge, quality)
        copied += 1
    return copied


def _site_source(relative_path):
    staged = SITE_ROOT / relative_path
    return staged if staged.exists() else BASE_DIR / relative_path


def _build_site_contents(output_dir, max_edge, quality):
    output_dir = _prepare_output_dir(output_dir)

    for filename in COPY_ROOT_FILES:
        src = _site_source(filename)
        if src.exists():
            _copy_file(src, output_dir / filename)

    apple_touch_icon = BASE_DIR / "assets" / "apple-touch-icon.png"
    if apple_touch_icon.exists():
        for filename in APPLE_TOUCH_ICON_FALLBACKS:
            _copy_file(apple_touch_icon, output_dir / filename)

    for dirname in COPY_ROOT_DIRS:
        src = _site_source(dirname)
        if src.exists():
            _copy_tree(src, output_dir / dirname)

    data_copied = _copy_deploy_data(output_dir)
    # The Photos section currently displays a retirement notice. Keep source
    # images out of the deploy artifact while that notice remains active.
    photos_copied = 0
    for rel_path in COPY_ROUTE_FILES:
        src = _site_source(rel_path)
        if src.exists():
            _copy_file(src, output_dir / rel_path)
    return photos_copied, data_copied


def build_site(output_dir, max_edge=DEFAULT_MAX_EDGE, quality=DEFAULT_QUALITY):
    """Build completely off-path, then replace the deploy directory."""

    destination = _ensure_within_base(Path(output_dir))
    transaction_id = uuid.uuid4().hex
    staged = destination.with_name(f".{destination.name}.{transaction_id}.tmp")
    backup = destination.with_name(f".{destination.name}.{transaction_id}.backup")
    try:
        photos_copied, data_copied = _build_site_contents(staged, max_edge, quality)
        if destination.exists():
            os.replace(destination, backup)
        try:
            os.replace(staged, destination)
        except BaseException:
            if backup.exists():
                os.replace(backup, destination)
            raise
        if backup.exists():
            try:
                shutil.rmtree(backup)
            except OSError as exc:
                print(f"Warning: deploy backup cleanup failed: {backup}: {exc}")
        print(
            f"Built deploy site at {destination} with {photos_copied} optimized photos "
            f"and {data_copied} data files."
        )
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def main():
    parser = argparse.ArgumentParser(description="Build a deployable site directory with optimized photos.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help="Output directory for the deploy site.")
    parser.add_argument("--max-edge", type=int, default=DEFAULT_MAX_EDGE, help="Maximum width/height for web images.")
    parser.add_argument("--quality", type=int, default=DEFAULT_QUALITY, help="WebP quality for optimized photos.")
    args = parser.parse_args()

    build_site(Path(args.output), max_edge=args.max_edge, quality=args.quality)


if __name__ == "__main__":
    main()
