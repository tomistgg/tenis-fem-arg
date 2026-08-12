"""Plain-language analysis of refresh and website deployment outcomes."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

PROMOTABLE_STATUSES = {"success", "degraded"}
FINAL_STATUSES = {"success", "degraded", "partial", "failed"}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _sentence_subject(value: str) -> str:
    return value[:1].upper() + value[1:]


def effective_run_status(run_state: dict[str, Any] | None) -> str:
    """Return the trustworthy final status for a run-state payload."""
    state = run_state or {}
    status = _clean(state.get("status")).lower() or "unknown"
    if status in FINAL_STATUSES and not state.get("finished_at"):
        return "failed"
    return status


def transaction_was_promoted(run_state: dict[str, Any] | None) -> bool:
    state = run_state or {}
    promotion = state.get("promotion")
    return bool(
        effective_run_status(state) in PROMOTABLE_STATUSES
        and isinstance(promotion, dict)
        and promotion.get("deploy_site_promoted") is True
    )


def _subject_for_issue(issue: dict[str, Any]) -> str:
    component = _clean(issue.get("component")).lower()
    operation = _clean(issue.get("operation")).lower()
    combined = f"{component} {operation}"
    if "draw" in combined:
        return "tournament draws"
    if "ranking" in combined:
        return "WTA rankings"
    if "calendar" in combined:
        return "tournament calendar information"
    if "player profile" in combined or "player list" in combined:
        return "player information"
    if "bjk" in combined or "billie" in combined:
        return "Billie Jean King Cup information"
    if "olympic" in combined:
        return "Olympic tournament information"
    if "grand-slam" in combined or "grand slam" in combined:
        return "Grand Slam information"
    if "tstrength" in combined or "tournament strength" in combined:
        return "tournament strength information"
    if "deploy" in combined or "site" in combined or "html" in combined or "render" in combined:
        return "website files"
    if "data" in combined or "validation" in combined:
        return "refreshed data"
    if "browser" in combined:
        return "the source website connection"
    if component.startswith("wta") or component == "wta":
        return "WTA information"
    if "itf" in component:
        return "ITF information"
    return "part of the update"


def _friendly_issue(issue: dict[str, Any]) -> tuple[str, str]:
    subject = _subject_for_issue(issue)
    message = _clean(issue.get("message") or issue.get("type")).lower()
    operation = _clean(issue.get("operation")).lower()
    severity = _clean(issue.get("severity")).lower()

    if "stale" in message or "previously saved" in message:
        reason = f"Fresh {subject} could not be downloaded, so previously saved information was used."
        impact = "The affected information may be out of date."
    elif "no cached fallback" in message or "no saved" in message:
        reason = f"Fresh {subject} could not be downloaded and no saved copy was available."
        impact = "The affected information may be missing."
    elif "block" in message or "forbidden" in message or " 403" in f" {message}":
        reason = f"The data provider blocked a request for {subject}."
        impact = "The affected information could not be refreshed."
    elif "timed out" in message or "timeout" in message:
        reason = f"The provider took too long to return {subject}."
        impact = "The affected information could not be refreshed."
    elif "validation" in message or "validate" in operation or "safety" in message:
        reason = f"{_sentence_subject(subject)} did not pass the safety checks."
        impact = "The unsafe update was not accepted."
    elif "parse" in operation or "read" in operation or "invalid" in message:
        reason = f"Some information for {subject} was received but could not be read safely."
        impact = "The affected information was not accepted."
    elif "build" in operation or "render" in operation:
        reason = f"The update could not build {subject}."
        impact = "A safe website version could not be prepared."
    elif any(word in operation for word in ("fetch", "request", "load")) or "request failed" in message:
        reason = f"{_sentence_subject(subject)} could not be downloaded from the provider."
        impact = "The affected information could not be refreshed."
    elif issue.get("returncode") is not None or _clean(issue.get("type")) == "ChildProcessError":
        reason = "The update process stopped unexpectedly."
        impact = "The update could not be checked or published safely."
    else:
        reason = f"A problem affected {subject}."
        impact = "The affected information could not be refreshed normally."

    if severity == "partial":
        impact = "The update was rejected to prevent incomplete information from reaching the website."
    elif severity == "failed":
        impact = "The update stopped before a safe website version could be published."
    return reason, impact


def summarize_run_issues(run_state: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Group repeated technical failures into concise, explanatory messages."""
    state = run_state or {}
    issues = [item for item in state.get("issues") or [] if isinstance(item, dict)]
    terminal = state.get("error")
    if isinstance(terminal, dict):
        terminal_issue = dict(terminal)
        terminal_issue.setdefault("component", terminal_issue.get("component") or "update")
        terminal_issue.setdefault("operation", terminal_issue.get("operation") or "run update")
        terminal_issue["severity"] = "failed"
        issues.append(terminal_issue)

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for issue in issues:
        reason, impact = _friendly_issue(issue)
        key = (reason, impact)
        entry = grouped.setdefault(
            key,
            {
                "reason": reason,
                "impact": impact,
                "count": 0,
                "technical": Counter(),
            },
        )
        entry["count"] += 1
        component = _clean(issue.get("component")) or "update"
        operation = _clean(issue.get("operation")) or "unspecified operation"
        message = _clean(issue.get("message") or issue.get("type")) or "No error message was recorded"
        if issue.get("returncode") is not None:
            message = f"{message} (exit code {issue['returncode']})"
        entry["technical"][(component, operation, message)] += 1

    result = []
    for entry in grouped.values():
        technical = [
            {
                "component": component,
                "operation": operation,
                "message": message,
                "count": count,
            }
            for (component, operation, message), count in entry.pop("technical").items()
        ]
        entry["technical"] = technical
        result.append(entry)
    return result


def website_outcome(status: str, deployment_status: str | None = None) -> tuple[str, str]:
    """Describe the public-site result, including the separate Pages deployment."""
    status = _clean(status).lower() or "unknown"
    deployment = _clean(deployment_status).lower() if deployment_status is not None else "pending"

    if status == "running":
        return "Not updated yet.", "The update is still being prepared and checked."
    if status not in PROMOTABLE_STATUSES:
        if status == "partial":
            return (
                "Not updated. The previous version remains online.",
                "Publishing was intentionally blocked because required information was incomplete.",
            )
        if status == "failed":
            return (
                "Not updated. The previous version remains online.",
                "The update stopped before a safe version could be published.",
            )
        return (
            "Not updated. The previous version remains online.",
            "A trustworthy update result was not available, so publishing was not allowed.",
        )

    warning_suffix = " Some information may be older or missing." if status == "degraded" else ""
    if deployment == "success":
        return "Updated. The new version is now live.", f"Publishing completed successfully.{warning_suffix}"
    if deployment in {"failure", "failed"}:
        return (
            "Not updated. The previous version remains online.",
            "The update passed its checks, but publishing to the website failed.",
        )
    if deployment == "cancelled":
        return (
            "Not updated. The previous version remains online.",
            "The update passed its checks, but publishing was cancelled.",
        )
    if deployment == "skipped":
        return (
            "Not updated. The previous version remains online.",
            "Publishing did not run because an earlier workflow step did not complete successfully.",
        )
    return (
        "Ready to publish; the public website is not confirmed yet.",
        f"The new version passed its checks and is waiting for the publishing step.{warning_suffix}",
    )


def _final_outcome(outcome: str, status: str, deployment_status: str | None) -> str:
    deployment = _clean(deployment_status).lower() if deployment_status is not None else "pending"
    if status not in PROMOTABLE_STATUSES or deployment == "pending":
        return outcome
    if deployment == "success":
        return "Website updated with warnings" if status == "degraded" else "Website updated successfully"
    if deployment in {"failure", "failed"}:
        return "Website not updated — publishing failed"
    if deployment == "cancelled":
        return "Website not updated — publishing was cancelled"
    if deployment == "skipped":
        return "Website not updated — publishing did not run"
    return outcome


def _next_step(status: str, deployment_status: str | None) -> str:
    deployment = _clean(deployment_status).lower() if deployment_status is not None else "pending"
    if status in PROMOTABLE_STATUSES and deployment in {"failure", "failed", "cancelled", "skipped"}:
        if deployment == "skipped":
            return "Review the failed workflow step; the update can run again after that problem is resolved."
        return "The prepared version can be published again after the deployment problem is resolved."
    if status in {"degraded", "partial"}:
        return "The next scheduled update will automatically try the affected sources again."
    if status == "failed":
        return "Review the technical details; the next scheduled update will try again automatically."
    return "No action is required."


def analyze_execution(
    run_state: dict[str, Any] | None,
    *,
    deployment_status: str | None = None,
) -> dict[str, Any]:
    state = run_state or {}
    raw_status = _clean(state.get("status")).lower() or "unknown"
    status = effective_run_status(state)
    if raw_status in FINAL_STATUSES and raw_status != status:
        outcome = "Website not updated — the update did not finish"
        summary = "The process stopped before its checks and publishing preparation were completed."
    elif status == "success":
        outcome = "Update completed successfully"
        summary = "The refreshed information passed the safety checks and a new website version was prepared."
    elif status == "degraded":
        outcome = "Update completed with warnings"
        summary = "The main update was safe, but some information could not be refreshed normally."
    elif status == "partial":
        outcome = "Website not updated — required information was incomplete"
        summary = "At least one required part of the update was incomplete, so the entire update was rejected."
    elif status == "failed":
        outcome = "Website not updated — the update process failed"
        summary = "The process stopped before it could prepare and publish a safe website version."
    elif status == "running":
        outcome = "Update in progress"
        summary = "The information is still being refreshed and checked."
    else:
        outcome = "Website not updated — update status unavailable"
        summary = "The process did not provide a trustworthy final result."

    outcome = _final_outcome(outcome, status, deployment_status)
    website, publishing = website_outcome(status, deployment_status)
    next_step = _next_step(status, deployment_status)

    return {
        "status": status,
        "raw_status": raw_status,
        "outcome": outcome,
        "summary": summary,
        "website": website,
        "publishing": publishing,
        "next_step": next_step,
        "issues": summarize_run_issues(state),
        "promoted": transaction_was_promoted(state),
    }


def render_status_markdown(
    run_state: dict[str, Any] | None,
    *,
    deployment_status: str | None = None,
    title: str = "Website Update Report",
) -> str:
    analysis = analyze_execution(run_state, deployment_status=deployment_status)
    lines = [
        f"# {title}",
        "",
        "## Update result",
        f"- **Result:** {analysis['outcome']}",
        f"- **Website:** {analysis['website']}",
        f"- **Publishing:** {analysis['publishing']}",
        f"- **What happened:** {analysis['summary']}",
    ]
    if analysis["issues"]:
        lines.extend(["", "## What went wrong"])
        for issue in analysis["issues"]:
            count = f" This happened {issue['count']} times." if issue["count"] > 1 else ""
            lines.append(f"- {issue['reason']}{count} {issue['impact']}")
    lines.extend(["", "## What happens next", f"- **Next step:** {analysis['next_step']}", ""])
    technical = [detail for issue in analysis["issues"] for detail in issue["technical"]]
    if run_state:
        lines.extend(["## Technical details"])
        lines.append(f"- Internal status: {analysis['raw_status']}")
        if (run_state or {}).get("run_id"):
            lines.append(f"- Run reference: {(run_state or {})['run_id']}")
        for detail in technical:
            count = f" ({detail['count']} occurrences)" if detail["count"] > 1 else ""
            lines.append(f"- {detail['component']} / {detail['operation']}: {detail['message']}{count}")
        lines.append("")
    return "\n".join(lines)


def finalize_email_markdown(markdown: str, status: str, deployment_status: str) -> str:
    effective_status = _clean(status).lower() or "unknown"
    website, publishing = website_outcome(effective_status, deployment_status)
    replacements = {
        "Website": website,
        "Publishing": publishing,
        "Next step": _next_step(effective_status, deployment_status),
    }
    if effective_status in PROMOTABLE_STATUSES:
        replacements["Result"] = _final_outcome(
            "Update completed with warnings" if effective_status == "degraded" else "Update completed successfully",
            effective_status,
            deployment_status,
        )
    result = markdown
    for label, value in replacements.items():
        pattern = rf"(?m)^- \*\*{re.escape(label)}:\*\*.*$"
        replacement = f"- **{label}:** {value}"
        result, count = re.subn(pattern, replacement, result, count=1)
        if count != 1:
            raise ValueError(f"email report has no {label!r} result line")
    return result


def _load_state(path: str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("run status must be a JSON object")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    render_parser = subparsers.add_parser("render-status-email")
    render_parser.add_argument("--run-status", required=True)
    render_parser.add_argument("--output", required=True)

    finalize_parser = subparsers.add_parser("finalize-email")
    finalize_parser.add_argument("--input", required=True)
    finalize_parser.add_argument("--output", required=True)
    finalize_parser.add_argument("--status", required=True)
    finalize_parser.add_argument("--deployment-status", required=True)

    args = parser.parse_args()
    if args.command == "render-status-email":
        content = render_status_markdown(_load_state(args.run_status))
    else:
        content = finalize_email_markdown(
            Path(args.input).read_text(encoding="utf-8"),
            args.status,
            args.deployment_status,
        )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
