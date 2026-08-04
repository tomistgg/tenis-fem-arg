from execution_analysis import (
    analyze_execution,
    effective_run_status,
    finalize_email_markdown,
    render_status_markdown,
    summarize_run_issues,
    transaction_was_promoted,
)


def completed_state(status, *, issues=None, error=None):
    state = {
        "run_id": "run-123",
        "status": status,
        "finished_at": "2026-08-04T12:00:00Z",
        "issues": issues or [],
    }
    if status in {"success", "degraded"}:
        state["promotion"] = {"deploy_site_promoted": True}
    elif status == "partial":
        state["promotion"] = "blocked"
    if error:
        state["error"] = error
    return state


def test_each_transaction_status_explains_whether_publication_is_allowed():
    success = analyze_execution(completed_state("success"))
    degraded = analyze_execution(completed_state("degraded"))
    partial = analyze_execution(completed_state("partial"))
    failed = analyze_execution(completed_state("failed"))

    assert success["outcome"] == "Update completed successfully"
    assert success["website"].startswith("Ready to publish")
    assert degraded["outcome"] == "Update completed with warnings"
    assert "older or missing" in degraded["publishing"]
    assert partial["outcome"] == "Website not updated — required information was incomplete"
    assert "intentionally blocked" in partial["publishing"]
    assert failed["outcome"] == "Website not updated — the update process failed"
    assert "previous version remains online" in failed["website"]


def test_deployment_result_is_separate_from_refresh_result():
    state = completed_state("success")

    deployed = analyze_execution(state, deployment_status="success")
    publishing_failed = analyze_execution(state, deployment_status="failure")

    assert deployed["outcome"] == "Website updated successfully"
    assert deployed["website"] == "Updated. The new version is now live."
    assert publishing_failed["outcome"] == "Website not updated — publishing failed"
    assert "previous version remains online" in publishing_failed["website"]
    assert "published again" in publishing_failed["next_step"]


def test_repeated_technical_errors_are_grouped_and_explained():
    issue = {
        "component": "itf-loader",
        "operation": "fetch drawsheet",
        "message": "using stale draw after HTTP block",
        "severity": "degraded",
    }

    summaries = summarize_run_issues(completed_state("degraded", issues=[issue, issue, issue]))

    assert len(summaries) == 1
    assert summaries[0]["count"] == 3
    assert summaries[0]["reason"] == (
        "Fresh tournament draws could not be downloaded, so previously saved information was used."
    )
    assert summaries[0]["impact"] == "The affected information may be out of date."
    assert summaries[0]["technical"][0]["count"] == 3


def test_unfinished_final_status_is_not_trusted_or_promoted():
    state = {
        "status": "degraded",
        "finished_at": None,
        "promotion": {"deploy_site_promoted": True},
        "issues": [],
    }

    assert effective_run_status(state) == "failed"
    assert transaction_was_promoted(state) is False
    assert analyze_execution(state)["outcome"] == "Website not updated — the update did not finish"


def test_terminal_error_without_a_message_gets_a_plain_explanation():
    state = completed_state(
        "failed",
        error={"type": "ChildProcessError", "returncode": 120},
    )

    markdown = render_status_markdown(state)

    assert "The update process stopped unexpectedly" in markdown
    assert "## Technical details" in markdown
    assert "exit code 120" in markdown


def test_finalizer_replaces_pending_website_result_after_deployment():
    pending = render_status_markdown(completed_state("degraded"))

    final = finalize_email_markdown(pending, "degraded", "success")

    assert "Website updated with warnings" in final
    assert "Updated. The new version is now live." in final
    assert "Publishing completed successfully. Some information may be older or missing." in final
    assert "Ready to publish" not in final


def test_finalizer_preserves_blocked_partial_result_when_deployment_is_skipped():
    pending = render_status_markdown(completed_state("partial"))

    final = finalize_email_markdown(pending, "partial", "skipped")

    assert "Website not updated — required information was incomplete" in final
    assert "Publishing was intentionally blocked" in final
