import argparse
import os
import shutil
import uuid
from pathlib import Path

from runtime_logging import get_logger
from runtime_paths import DATA_DIR, SITE_ROOT


logger = get_logger("build")
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = BASE_DIR / ".site"
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
)
COPY_DATA_GLOBS = (
    "wta_rankings_latest_bundle.js",
    "wta_rankings_[0-9][0-9][0-9][0-9]_bundle.js",
)
COPY_DATA_DIRS = ("flags",)


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


def _site_source(relative_path):
    staged = SITE_ROOT / relative_path
    return staged if staged.exists() else BASE_DIR / relative_path


def _build_site_contents(output_dir):
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
        destination = output_dir / dirname
        base_source = BASE_DIR / dirname
        staged_source = SITE_ROOT / dirname
        if base_source.exists():
            _copy_tree(base_source, destination)
        if staged_source.exists() and staged_source.resolve() != base_source.resolve():
            _copy_tree(staged_source, destination)

    return _copy_deploy_data(output_dir)


def build_site(output_dir):
    """Build completely off-path, then replace the deploy directory."""

    destination = _ensure_within_base(Path(output_dir))
    transaction_id = uuid.uuid4().hex
    staged = destination.with_name(f".{destination.name}.{transaction_id}.tmp")
    backup = destination.with_name(f".{destination.name}.{transaction_id}.backup")
    try:
        data_copied = _build_site_contents(staged)
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
                logger.warning(f"Warning: deploy backup cleanup failed: {backup}: {exc}")
        print(f"Built deploy site at {destination} with {data_copied} data files.")
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def main():
    parser = argparse.ArgumentParser(description="Build a deployable site directory.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help="Output directory for the deploy site.")
    args = parser.parse_args()

    build_site(Path(args.output))


if __name__ == "__main__":
    main()
