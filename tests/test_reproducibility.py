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
    assert workflow.index("Require a promotable transaction") < workflow.index("Publish validated data to main")


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
    assert "IMAGEKIT_GALLERY_ROOT" not in workflows
    assert "IMAGEKIT_API_URL" not in workflows
    assert "git rebase" not in workflows
    assert "HEAD:main" not in workflows
    assert "_build_deploy_site" not in main_source
