import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError, type IncidentDetail } from "../api";
import { ExternalIcon } from "../icons";
import { Badge, confidence, PanelHead, Problem, ReportBadges, shortId, StatusBadge, when } from "../ui";
import { NotFound } from "./NotFound";

function ReviewControls({ detail }: { detail: IncidentDetail }) {
  const client = useQueryClient();
  const [comment, setComment] = useState("");
  const decide = useMutation({
    mutationFn: (decision: "approved" | "rejected") => api.decide(detail.incident.id, decision, comment),
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: ["incident", detail.incident.id] }),
        client.invalidateQueries({ queryKey: ["reviews"] }),
        client.invalidateQueries({ queryKey: ["projects"] }),
        client.invalidateQueries({ queryKey: ["project"] }),
        client.invalidateQueries({ queryKey: ["stats"] }),
      ]);
    },
  });
  const blocked = detail.policy_decisions.some((p) => !p.allowed);
  return (
    <section className="panel" aria-label="Review">
      <PanelHead title="Your decision" subtitle="Recorded under your account in the audit trail." />
      {blocked && <p className="error">Policy refused this fix. Approving it will not publish a pull request; an admin can override.</p>}
      <label>
        Comment (required to reject)
        <textarea value={comment} onChange={(event) => setComment(event.target.value)} rows={3} maxLength={4000} />
      </label>
      {decide.isError && (
        <p className="error" role="alert">
          {decide.error.message}
        </p>
      )}
      <div className="row">
        <button disabled={decide.isPending} onClick={() => decide.mutate("approved")}>
          Approve fix
        </button>
        <button className="danger" disabled={decide.isPending || !comment.trim()} onClick={() => decide.mutate("rejected")}>
          Reject
        </button>
      </div>
    </section>
  );
}

export function IncidentPage() {
  const id = useParams().incidentId ?? "";
  const detail = useQuery({ queryKey: ["incident", id], queryFn: () => api.incident(id) });

  if (detail.isLoading) return <p className="muted">Loading…</p>;
  if (detail.error instanceof ApiError && (detail.error.status === 404 || detail.error.status === 422)) return <NotFound />;
  if (detail.isError) return <Problem error={detail.error} retry={() => void detail.refetch()} />;
  const d = detail.data!;
  const latest = d.revisions.filter(Boolean).at(-1) ?? null;
  const report = d.report?.report ?? null;

  return (
    <>
      {d.project && (
        <p>
          <Link to={`/projects/${d.project.id}/incidents`}>← {d.project.name}</Link>
        </p>
      )}
      <div className="row between page-head">
        <div>
          <h1>
            {d.incident.job_name}
            {d.incident.build_number ? ` #${d.incident.build_number}` : ""} <StatusBadge status={d.incident.status} />
          </h1>
          <p className="muted">
            Incident <code>{shortId(d.incident.id)}</code> · {when(d.incident.created_at)} · branch {d.incident.branch ?? "—"} · stage {d.incident.failed_stage ?? "—"}
          </p>
        </div>
        <div className="row">
          {d.links.jenkins_build && (
            <a href={d.links.jenkins_build} target="_blank" rel="noopener noreferrer">
              Jenkins build <ExternalIcon size={15} />
            </a>
          )}
          {d.links.pull_request && (
            <a href={d.links.pull_request} target="_blank" rel="noopener noreferrer">
              Pull request <ExternalIcon size={15} />
            </a>
          )}
          <a href={d.links.grafana} target="_blank" rel="noopener noreferrer">
            Grafana <ExternalIcon size={15} />
          </a>
        </div>
      </div>

      <section className="panel">
        <PanelHead title="Report" subtitle="What the Coordinator concluded from all the agents." />
        {!latest || !report ? (
          <p className="muted">No report has been written yet.</p>
        ) : (
          <>
            <p>
              <strong>{latest.verdict}</strong> · confidence {confidence(latest.confidence)} · {latest.status} · revision {latest.revision ?? 0} <ReportBadges report={latest} />
            </p>
            <p>{latest.summary}</p>
            {latest.missing_agents.length > 0 && <p className="error">Missing agents: {latest.missing_agents.join(", ")}</p>}
            <h3>Agents</h3>
            <ul className="plain">
              {Object.entries(latest.agent_outcomes).map(([agent, outcome]) => (
                <li key={agent}>
                  {agent}: <Badge tone={outcome === "completed" ? "ok" : "bad"}>{outcome}</Badge>
                </li>
              ))}
            </ul>
            <p>
              <a href={`/api/v1/incidents/${d.incident.id}/report?format=md`} target="_blank" rel="noopener noreferrer">
                Download as Markdown
              </a>
            </p>
            <details>
              <summary>Full report (JSON)</summary>
              <pre>{JSON.stringify(report, null, 2)}</pre>
            </details>
          </>
        )}
      </section>

      <section className="panel">
        <PanelHead title="Evidence by agent" subtitle="What each agent found, as text." />
        {Object.keys(d.evidence).length === 0 && <p className="muted">No agent evidence recorded.</p>}
        {Object.entries(d.evidence).map(([agent, doc]) => (
          <details key={agent}>
            <summary>
              {agent} — {doc.failure_type ?? "no verdict"} ({confidence(doc.confidence)})
            </summary>
            <p>{doc.summary}</p>
            {(doc.evidence_items ?? []).map((item, index) => (
              <div key={item.id ?? index} className="evidence">
                <div className="muted small">
                  {item.kind} · {item.source}
                </div>
                <pre>{item.content}</pre>
              </div>
            ))}
          </details>
        ))}
      </section>

      {d.fix && (
        <section className="panel">
          <h2>
            Proposed fix · attempt {d.fix.attempt} <Badge tone="info">{d.fix.status.replace(/_/g, " ").toLowerCase()}</Badge>
          </h2>
          <p>{d.fix.rationale}</p>
          <p className="muted small">
            Branch <code>{d.fix.branch}</code> · files {d.fix.changed_files.join(", ") || "—"} · confidence {confidence(d.fix.confidence)}
          </p>
          <pre className="diff">{d.fix.diff}</pre>
          <h3>Policy</h3>
          {d.policy_decisions.length === 0 && <p className="muted">Not evaluated yet.</p>}
          {d.policy_decisions.map((policy) => (
            <div key={policy.id}>
              <Badge tone={policy.allowed ? "ok" : "bad"}>{policy.allowed ? "allowed" : "blocked"}</Badge> {policy.policy_package}
              {policy.violations && policy.violations.length > 0 && <pre>{JSON.stringify(policy.violations, null, 2)}</pre>}
            </div>
          ))}
          {d.approval && (
            <p>
              <Badge tone={d.approval.decision === "approved" ? "ok" : "bad"}>{d.approval.decision}</Badge> by {d.approval.reviewer} on {when(d.approval.decided_at)}
              {d.approval.comment ? ` — “${d.approval.comment}”` : ""}
            </p>
          )}
        </section>
      )}

      {d.can_decide && <ReviewControls detail={d} />}
      {d.awaiting_decision && !d.can_decide && <p className="muted">This fix is waiting for a reviewer. Your role can view it but not decide.</p>}

      <section className="panel">
        <PanelHead title="Timeline" subtitle="Every audited step for this incident, in order." />
        <ol className="timeline">
          {d.timeline.map((event) => (
            <li key={event.id}>
              <span className="muted small">{when(event.created_at)}</span> <Badge tone={event.ok === false ? "bad" : "neutral"}>{event.event_type}</Badge> {event.actor} — {event.summary}
              {event.duration_ms !== null && <span className="muted small"> ({(event.duration_ms / 1000).toFixed(1)} s)</span>}
            </li>
          ))}
        </ol>
      </section>
    </>
  );
}
