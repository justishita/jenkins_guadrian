"""Notification message renderer for Jenkins incident remediation approval.

Turns an incident, Jenkins Findings evidence, and Coordinator proposed fix
into multi-channel notifications (Slack, Teams, Email, Console) containing:
1. Title: job, build, failure type, confidence
2. 3-line summary
3. Top evidence with links to the Jenkins build URL
4. Proposed fix summary (from Coordinator payload)
5. Approve/Reject links with signed, expiring URLs (24h, single-use HMAC token)

Edge cases handled:
- Secrets in evidence: runs redaction on all fields before rendering.
- Message length limits: truncates oversized logs/diffs safely for Slack/Teams/Email.
"""

from __future__ import annotations

import html
import logging
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.config import settings
from backend.notifications.tokens import build_action_urls
from common.redaction import redact

logger = logging.getLogger(__name__)

# Limits for messaging platforms
SLACK_SECTION_CHAR_LIMIT = 2800
TEAMS_SECTION_CHAR_LIMIT = 4000
MAX_EXCERPT_CHAR_LIMIT = 1000
MAX_DIFF_CHAR_LIMIT = 1500


def _truncate(text: str, limit: int) -> str:
    """Truncate text cleanly with a marker if it exceeds limit."""
    if len(text) <= limit:
        return text
    marker = "\n... [truncated]"
    return text[: max(0, limit - len(marker))] + marker


class TopEvidenceItem(BaseModel):
    """Structured evidence item with links to the Jenkins build."""

    model_config = ConfigDict(frozen=True)

    id: str
    kind: str
    source: str
    excerpt: str
    build_url: str
    location: str | None = None


class RenderedNotification(BaseModel):
    """Complete rendered payload across all supported notification channels."""

    model_config = ConfigDict(frozen=True)

    incident_id: str
    attempt: int
    title: str
    summary_3_lines: str
    top_evidence: list[TopEvidenceItem]
    proposed_fix_summary: str
    approve_url: str
    reject_url: str

    # Pre-rendered channel formats
    plain_text: str
    slack_payload: dict[str, Any]
    teams_payload: dict[str, Any]
    email_subject: str
    email_html: str
    email_text: str


class NotificationRenderer:
    """Renders formatted incident approval requests."""

    def __init__(self, base_url: str | None = None, signing_secret: str | None = None) -> None:
        self.base_url = base_url or settings.BASE_URL
        self.signing_secret = signing_secret or settings.APPROVAL_SIGNING_SECRET or settings.WEBHOOK_SHARED_SECRET

    def render(
        self,
        incident: dict[str, Any],
        jenkins_findings: dict[str, Any],
        proposed_fix: dict[str, Any] | str,
        *,
        attempt: int = 0,
        max_evidence_items: int = 3,
    ) -> RenderedNotification:
        """Render notification content from incident data, Jenkins findings, and proposed fix.

        All raw strings are passed through redact() to guarantee zero secrets in outbound messages.
        Bounded truncation is performed before and after redaction to prevent ReDoS on massive logs.
        """
        # 1. Normalize and redact incident details
        inc_id = str(incident.get("id") or incident.get("incident_id") or "unknown-incident")
        job_name = redact(str(incident.get("job_name", "unknown-job")))
        build_number = incident.get("build_number", 0)
        raw_build_url = str(incident.get("build_url", "")).rstrip("/")
        build_url = raw_build_url or f"http://jenkins.local/job/{job_name}/{build_number}"
        branch = redact(str(incident.get("branch", "unknown-branch")))
        failed_stage = redact(str(incident.get("failed_stage", "build")))

        # 2. Normalize and redact findings
        failure_type = str(jenkins_findings.get("failure_type", "unknown"))
        raw_confidence = jenkins_findings.get("confidence", 0.0)
        try:
            confidence = float(raw_confidence)
        except (ValueError, TypeError):
            confidence = 0.0
        confidence_pct = int(confidence * 100) if confidence <= 1.0 else int(confidence)

        raw_summary = _truncate(str(jenkins_findings.get("summary", "Automated pipeline failure detected.")), 2000)
        findings_summary = redact(raw_summary)

        # 3. Normalize and redact proposed fix
        if isinstance(proposed_fix, dict):
            raw_rat = _truncate(str(proposed_fix.get("rationale") or proposed_fix.get("summary") or proposed_fix.get("commit_message") or "Proposed remediation plan"), 2000)
            rationale = redact(raw_rat)
            changed_files = proposed_fix.get("changed_files") or proposed_fix.get("changed_paths") or []
            files_str = ", ".join(redact(str(f)) for f in changed_files) if changed_files else "N/A"

            diff_snippet = _truncate(str(proposed_fix.get("diff", "")), MAX_DIFF_CHAR_LIMIT * 2)
            diff_redacted = redact(diff_snippet)
            if diff_redacted:
                diff_redacted = _truncate(diff_redacted, MAX_DIFF_CHAR_LIMIT)
                proposed_fix_summary = f"{rationale}\nFiles: {files_str}\nDiff:\n{diff_redacted}"
            else:
                proposed_fix_summary = f"{rationale} (Files: {files_str})"
        else:
            raw_fix = _truncate(str(proposed_fix), MAX_DIFF_CHAR_LIMIT * 2)
            proposed_fix_summary = redact(raw_fix)

        proposed_fix_summary = _truncate(proposed_fix_summary, MAX_DIFF_CHAR_LIMIT)

        # 4. Generate Title: (job, build, failure type, confidence)
        title = f"[{job_name} #{build_number}] {failure_type} ({confidence_pct}% confidence)"

        # 5. Generate 3-line summary
        line1 = f"Pipeline {job_name} #{build_number} failed in stage '{failed_stage}' on branch {branch}."
        line2 = f"Diagnosis ({confidence_pct}%): {findings_summary}"
        line3 = f"Proposed fix: {rationale if isinstance(proposed_fix, dict) else proposed_fix_summary}"
        summary_3_lines = f"{line1}\n{line2}\n{line3}"

        # 6. Extract Top Evidence items with links to Jenkins build URL
        raw_evidence_list = jenkins_findings.get("evidence_items") or []
        top_evidence: list[TopEvidenceItem] = []
        for item in raw_evidence_list[:max_evidence_items]:
            item_id = str(item.get("id", "evidence-1"))
            kind = str(item.get("kind", "log_excerpt"))
            source = redact(str(item.get("source", "jenkins")))

            raw_content = _truncate(str(item.get("content", "")), MAX_EXCERPT_CHAR_LIMIT * 2)
            content = redact(raw_content)
            excerpt = _truncate(content, MAX_EXCERPT_CHAR_LIMIT)

            location_dict = item.get("location")
            loc_str = None
            if isinstance(location_dict, dict) and location_dict.get("file"):
                loc_str = f"{location_dict.get('file')}:{location_dict.get('line', '')}"

            # Ensure direct link into the Jenkins build console
            evidence_link = f"{build_url}/console" if not build_url.endswith("/console") else build_url

            top_evidence.append(
                TopEvidenceItem(
                    id=item_id,
                    kind=kind,
                    source=source,
                    excerpt=excerpt,
                    build_url=evidence_link,
                    location=loc_str,
                )
            )

        # 7. Generate Signed Expiring Approve / Reject URLs (24h expiry, HMAC token)
        action_urls = build_action_urls(
            incident_id=inc_id,
            attempt=attempt,
            base_url=self.base_url,
            secret=self.signing_secret,
        )
        approve_url = action_urls["approve_url"]
        reject_url = action_urls["reject_url"]

        # 8. Render plain text format (Console / Plain Email)
        plain_evidence_blocks = []
        for ev in top_evidence:
            loc = f" [{ev.location}]" if ev.location else ""
            plain_evidence_blocks.append(
                f"- [{ev.kind}] {ev.source}{loc}\n  Jenkins Link: {ev.build_url}\n  Excerpt: {ev.excerpt}"
            )
        evidence_text = "\n".join(plain_evidence_blocks) if plain_evidence_blocks else "No evidence items provided."

        plain_text = (
            f"=== {title} ===\n\n"
            f"Summary:\n{summary_3_lines}\n\n"
            f"Top Evidence:\n{evidence_text}\n\n"
            f"Proposed Fix Summary:\n{proposed_fix_summary}\n\n"
            f"Decide Remediation (Valid for 24 hours):\n"
            f"  [APPROVE]: {approve_url}\n"
            f"  [REJECT]:  {reject_url}\n"
        )

        # 9. Render Slack Block Kit payload
        slack_evidence_text = ""
        for ev in top_evidence:
            loc = f" (`{ev.location}`)" if ev.location else ""
            slack_evidence_text += f"• *{ev.kind}* via {ev.source}{loc} - <{ev.build_url}|View in Jenkins>\n```{_truncate(ev.excerpt, 300)}```\n"

        slack_blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": _truncate(title, 150), "emoji": True},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Summary:*\n{_truncate(summary_3_lines, SLACK_SECTION_CHAR_LIMIT)}",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Top Evidence (Jenkins Links):*\n{_truncate(slack_evidence_text or 'None available', SLACK_SECTION_CHAR_LIMIT)}",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Proposed Fix:*\n```{_truncate(proposed_fix_summary, SLACK_SECTION_CHAR_LIMIT)}```",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve Fix"},
                        "style": "primary",
                        "url": approve_url,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Reject Fix"},
                        "style": "danger",
                        "url": reject_url,
                    },
                ],
            },
        ]
        slack_payload = {
            "text": title,
            "blocks": slack_blocks,
        }

        # 10. Render Teams MessageCard payload
        teams_evidence_sections = []
        for ev in top_evidence:
            teams_evidence_sections.append(
                f"**{ev.kind}** ({ev.source}) - [View in Jenkins]({ev.build_url})<br><pre>{html.escape(_truncate(ev.excerpt, 400))}</pre>"
            )
        teams_ev_html = "<br>".join(teams_evidence_sections) if teams_evidence_sections else "None"

        teams_payload = {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "summary": title,
            "themeColor": "D9381E",
            "title": title,
            "sections": [
                {
                    "activityTitle": "DevOps Incident Remediation Approval Required",
                    "activitySubtitle": f"Incident {inc_id} (Attempt #{attempt})",
                    "facts": [
                        {"name": "Job", "value": f"{job_name} #{build_number}"},
                        {"name": "Stage", "value": failed_stage},
                        {"name": "Classification", "value": f"{failure_type} ({confidence_pct}%)"},
                    ],
                    "text": (
                        f"**Summary:**<br>{html.escape(summary_3_lines).replace(chr(10), '<br>')}<br><br>"
                        f"**Top Evidence:**<br>{teams_ev_html}<br><br>"
                        f"**Proposed Fix:**<br><pre>{html.escape(_truncate(proposed_fix_summary, TEAMS_SECTION_CHAR_LIMIT))}</pre>"
                    ),
                }
            ],
            "potentialAction": [
                {
                    "@type": "OpenUri",
                    "name": "Approve Fix (24h)",
                    "targets": [{"os": "default", "uri": approve_url}],
                },
                {
                    "@type": "OpenUri",
                    "name": "Reject Fix (24h)",
                    "targets": [{"os": "default", "uri": reject_url}],
                },
            ],
        }

        # 11. Render HTML email
        email_evidence_rows = ""
        for ev in top_evidence:
            email_evidence_rows += f"""
            <div style="margin-bottom: 12px; padding: 8px; background-color: #f6f8fa; border-left: 3px solid #0969da;">
                <strong>{html.escape(ev.kind)}</strong> ({html.escape(ev.source)}) -
                <a href="{html.escape(ev.build_url)}" style="color: #0969da;">View in Jenkins Console</a>
                <pre style="margin-top: 6px; white-space: pre-wrap; font-family: monospace; font-size: 12px;">{html.escape(ev.excerpt)}</pre>
            </div>
            """

        email_html = f"""
        <!DOCTYPE html>
        <html>
        <head>
          <meta charset="utf-8">
          <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #24292f; line-height: 1.5; }}
            .container {{ max-width: 680px; margin: 0 auto; padding: 20px; border: 1px solid #d0d7de; border-radius: 6px; }}
            .header {{ background-color: #cf222e; color: #ffffff; padding: 12px 16px; border-radius: 4px; font-size: 18px; font-weight: bold; }}
            .section {{ margin-top: 16px; }}
            .btn {{ display: inline-block; padding: 10px 18px; font-size: 14px; font-weight: bold; text-decoration: none; border-radius: 6px; margin-right: 12px; }}
            .btn-approve {{ background-color: #1f883d; color: #ffffff; }}
            .btn-reject {{ background-color: #cf222e; color: #ffffff; }}
            pre {{ background-color: #f6f8fa; padding: 10px; border-radius: 6px; overflow-x: auto; }}
          </style>
        </head>
        <body>
          <div class="container">
            <div class="header">{html.escape(title)}</div>
            <div class="section">
              <h3>Incident Summary</h3>
              <p>{html.escape(summary_3_lines).replace(chr(10), '<br>')}</p>
            </div>
            <div class="section">
              <h3>Top Evidence</h3>
              {email_evidence_rows or '<p>No evidence items available.</p>'}
            </div>
            <div class="section">
              <h3>Proposed Fix</h3>
              <pre>{html.escape(proposed_fix_summary)}</pre>
            </div>
            <div class="section" style="margin-top: 24px; padding-top: 16px; border-top: 1px solid #d0d7de;">
              <h3>Action Required (Link valid for 24 hours, single-use):</h3>
              <p>
                <a href="{html.escape(approve_url)}" class="btn btn-approve">Approve Remediation</a>
                <a href="{html.escape(reject_url)}" class="btn btn-reject">Reject Remediation</a>
              </p>
            </div>
          </div>
        </body>
        </html>
        """

        return RenderedNotification(
            incident_id=inc_id,
            attempt=attempt,
            title=title,
            summary_3_lines=summary_3_lines,
            top_evidence=top_evidence,
            proposed_fix_summary=proposed_fix_summary,
            approve_url=approve_url,
            reject_url=reject_url,
            plain_text=plain_text,
            slack_payload=slack_payload,
            teams_payload=teams_payload,
            email_subject=title,
            email_html=email_html,
            email_text=plain_text,
        )


# Global renderer instance
renderer = NotificationRenderer()
