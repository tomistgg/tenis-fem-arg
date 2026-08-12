from pathlib import Path

from generated_artifacts import remove_generated_data_artifacts

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_generated_bundle_cleanup_preserves_canonical_rankings_and_history(tmp_path):
    canonical_files = (
        "wta_rankings_20_29.csv",
        "wta_matches_arg.csv",
        "player_aliases_wta_itf.json",
    )
    generated_files = (
        "history_data_bundle.js",
        "player_aliases_wta_itf_bundle.js",
        "wta_rankings_latest_bundle.js",
        "wta_rankings_2026_bundle.js",
    )
    for filename in canonical_files + generated_files:
        (tmp_path / filename).write_text(filename, encoding="utf-8")

    removed = remove_generated_data_artifacts(tmp_path)

    assert {path.name for path in removed} == set(generated_files)
    assert all((tmp_path / filename).is_file() for filename in canonical_files)
    assert all(not (tmp_path / filename).exists() for filename in generated_files)


def test_generated_deploy_outputs_are_ignored_at_the_repository_boundary():
    ignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")

    for pattern in (
        "/app.html",
        "/assets/js/",
        "/data/history_data_bundle.js",
        "/data/player_aliases_wta_itf_bundle.js",
        "/data/wta_rankings_[0-9][0-9][0-9][0-9]_bundle.js",
    ):
        assert pattern in ignore
