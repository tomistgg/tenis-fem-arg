import re
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = PROJECT_DIR / ".github" / "workflows"


def test_lock_file_uses_sha256_hashes_for_every_requirement():
    lock = (PROJECT_DIR / "requirements.lock").read_text(encoding="utf-8")
    requirement_lines = [
        line for line in lock.splitlines()
        if line and not line.startswith(("#", " ", "-", "\\"))
    ]
    assert requirement_lines
    assert "--generate-hashes" in lock
    assert all("==" in line for line in requirement_lines)
    assert lock.count("--hash=sha256:") >= len(requirement_lines)


def test_all_github_actions_are_pinned_to_full_commit_shas():
    uses_pattern = re.compile(r"^\s*-?\s*uses:\s*[^\s@]+@([^\s#]+)", re.MULTILINE)
    violations = []
    for workflow in WORKFLOW_DIR.glob("*.yml"):
        for ref in uses_pattern.findall(workflow.read_text(encoding="utf-8")):
            if not re.fullmatch(r"[0-9a-f]{40}", ref):
                violations.append(f"{workflow.name}: {ref}")
    assert violations == []


def test_ci_uses_hash_locked_installs_and_lock_based_pip_cache():
    workflows = "\n".join(
        path.read_text(encoding="utf-8") for path in WORKFLOW_DIR.glob("*.yml")
    )
    assert "--require-hashes -r requirements.lock" in workflows
    assert "cache: pip" in workflows
    assert "cache-dependency-path: requirements.lock" in workflows


def test_ci_installs_browser_and_driver_as_a_compatible_v2_pair():
    workflows = "\n".join(
        path.read_text(encoding="utf-8") for path in WORKFLOW_DIR.glob("*.yml")
    )
    compatible_action = (
        "browser-actions/setup-chrome@"
        "2e1d749697dd1612b833dba4a722266286fbefcd # v2.1.2"
    )
    assert workflows.count(compatible_action) == 2
    assert workflows.count("install-chromedriver: true") == 2


def test_pages_is_built_uploaded_and_deployed_by_one_workflow():
    workflow_text = {
        path.name: path.read_text(encoding="utf-8")
        for path in WORKFLOW_DIR.glob("*.yml")
    }
    publishers = [
        name
        for name, text in workflow_text.items()
        if "actions/upload-pages-artifact@" in text or "actions/deploy-pages@" in text
    ]

    assert publishers == ["hourly-update.yml"]
    publisher = workflow_text["hourly-update.yml"]
    assert publisher.count("actions/upload-pages-artifact@") == 1
    assert publisher.count("actions/deploy-pages@") == 1
    assert "path: .site" in publisher
    assert "refs/heads/data-state" in publisher
    assert "python build_deploy_site.py" not in publisher


def test_refresh_publishes_only_validated_data_to_main():
    workflow = (WORKFLOW_DIR / "hourly-update.yml").read_text(encoding="utf-8")

    assert "Publish validated data to main" in workflow
    assert 'GIT_INDEX_FILE="$main_index" git read-tree "$main_parent"' in workflow
    assert 'GIT_INDEX_FILE="$main_index" git add -f -A -- data' in workflow
    assert 'git push origin "$main_commit:refs/heads/main"' in workflow
    assert 'if [ "$main_parent" != "$GITHUB_SHA" ]; then' in workflow
    assert "Remove legacy generated bundles from canonical data" in workflow
    assert workflow.index("Remove legacy generated bundles from canonical data") < workflow.index(
        "Snapshot validated quality baseline"
    )
    assert workflow.index("Require a promotable transaction") < workflow.index("Publish validated data to main")


def test_refresh_runs_complete_quality_gate_before_updating_or_deploying():
    workflow = (WORKFLOW_DIR / "hourly-update.yml").read_text(encoding="utf-8")
    overlay_index = workflow.index("Preserve pushed data files")
    refresh_index = workflow.index("Extract, transform, validate, and build once")
    upload_index = workflow.index("Upload immutable Pages artifact")
    required_commands = [
        "python -m pip check",
        "python data_quality.py validate",
        "--baseline-dir .quality-baseline/data",
        "python -m ruff check .",
        "python -m mypy",
        "python -m pytest",
        "python -m pre_commit run --all-files",
        "python -m pip_audit --require-hashes -r requirements.lock",
    ]

    for command in required_commands:
        command_index = workflow.index(command)
        assert command_index < refresh_index
        assert command_index < upload_index

    assert workflow.index("Snapshot validated quality baseline") < overlay_index


def test_push_overlay_recovers_data_changes_from_an_earlier_failed_run():
    workflow = (WORKFLOW_DIR / "hourly-update.yml").read_text(encoding="utf-8")

    assert "id: restore_data_state" in workflow
    assert "Source revision:" in workflow
    assert "DATA_STATE_SOURCE_SHA: ${{ steps.restore_data_state.outputs.source_sha }}" in workflow
    assert 'overlay_base="$DATA_STATE_SOURCE_SHA"' in workflow
    assert 'git diff --name-only -z "$overlay_base" "$GITHUB_SHA" -- data' in workflow


def test_refresh_restores_repository_managed_entry_list_config_after_data_state():
    workflow = (WORKFLOW_DIR / "hourly-update.yml").read_text(encoding="utf-8")

    restore_state = workflow.index("Restore latest validated data state")
    restore_config = workflow.index("Restore repository-managed data configuration")
    quality_baseline = workflow.index("Snapshot validated quality baseline")
    assert restore_state < restore_config < quality_baseline
    assert "data/gs_pdf_urls.json" in workflow
    assert 'git show "$GITHUB_SHA:$relative" > "$GITHUB_WORKSPACE/$relative"' in workflow


def test_update_notification_is_finalized_after_pages_deployment():
    workflow = (WORKFLOW_DIR / "hourly-update.yml").read_text(encoding="utf-8")

    assert workflow.index("actions/deploy-pages@") < workflow.index("Send final website update report")
    assert "needs:\n      - refresh-build-upload\n      - deploy" in workflow
    assert "needs.deploy.result" in workflow
    assert "execution_analysis.py finalize-email" in workflow
    assert workflow.count("action-send-mail@") == 1


def test_deployment_does_not_rebase_main_or_require_removed_secrets():
    workflows = "\n".join(
        path.read_text(encoding="utf-8") for path in WORKFLOW_DIR.glob("*.yml")
    )
    main_source = (PROJECT_DIR / "main.py").read_text(encoding="utf-8")

    assert "PAGES_PUSH_TOKEN" not in workflows
    assert "IMAGEKIT_PRIVATE_KEY" not in workflows
    assert "IMAGEKIT_API_URL" not in workflows
    assert "git rebase" not in workflows
    assert "HEAD:main" not in workflows
    assert "_build_deploy_site" not in main_source
