import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Bar, BarChart, CartesianGrid, Cell, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api";
import { Kpi, Problem, percent, seconds } from "../ui";
import { useProject } from "./ProjectLayout";

const PALETTE = ["#4f46e5", "#f59e0b", "#10b981", "#ec4899", "#0ea5e9", "#8b5cf6"];

export function OverviewPage() {
  const project = useProject();
  const [days, setDays] = useState(14);
  const stats = useQuery({ queryKey: ["stats", project.id, days], queryFn: () => api.stats(project.id, days) });
  const config = useQuery({ queryKey: ["config"], queryFn: api.config, retry: false });

  if (stats.isLoading) return <p className="muted">Loading…</p>;
  if (stats.isError) return <Problem error={stats.error} retry={() => void stats.refetch()} />;
  const data = stats.data!;
  const grafana = config.data
    ? `${config.data.grafana_url.replace(/\/$/, "")}/d/agent-ops?var-job=${encodeURIComponent(project.jenkins_job)}`
    : null;

  return (
    <>
      <div className="row between">
        <label className="inline">
          Range{" "}
          <select value={days} onChange={(event) => setDays(Number(event.target.value))} aria-label="Range">
            <option value={7}>7 days</option>
            <option value={14}>14 days</option>
            <option value={30}>30 days</option>
            <option value={90}>90 days</option>
          </select>
        </label>
        {grafana && (
          <a href={grafana} target="_blank" rel="noopener noreferrer">
            Open Grafana dashboards ↗
          </a>
        )}
      </div>

      <div className="kpis">
        <Kpi label="Incidents" value={data.incidents_total} />
        <Kpi label="Awaiting review" value={data.approvals.pending} />
        <Kpi label="Approval rate" value={percent(data.approvals.approval_rate_pct)} hint={`${data.approvals.decided} decided`} />
        <Kpi label="Time to report (median)" value={seconds(data.reports.time_to_report_p50_s)} hint={`p95 ${seconds(data.reports.time_to_report_p95_s)}`} />
        <Kpi label="Fix success (accuracy proxy)" value={percent(data.fix_outcomes.fix_success_pct)} hint={data.fix_outcomes.note} />
        <Kpi label="Incomplete reports" value={`${data.reports.incomplete} / ${data.reports.total}`} />
      </div>

      <div className="charts">
        <section className="panel">
          <h2>Incidents per day</h2>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={data.incidents_per_day}>
              <CartesianGrid strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="date" tickFormatter={(d: string) => d.slice(5)} fontSize={12} />
              <YAxis allowDecimals={false} fontSize={12} width={30} />
              <Tooltip />
              <Bar dataKey="incidents" fill={PALETTE[0]} radius={[3, 3, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </section>

        <section className="panel">
          <h2>Root-cause verdicts</h2>
          {data.verdicts.length === 0 ? (
            <p className="muted">No reports in this range.</p>
          ) : (
            <ResponsiveContainer width="100%" height={220}>
              <PieChart>
                <Pie data={data.verdicts} dataKey="count" nameKey="verdict" outerRadius={80} label={(entry: { verdict: string }) => entry.verdict}>
                  {data.verdicts.map((entry, index) => (
                    <Cell key={entry.verdict} fill={PALETTE[index % PALETTE.length]} />
                  ))}
                </Pie>
                <Tooltip />
              </PieChart>
            </ResponsiveContainer>
          )}
        </section>

        <section className="panel">
          <h2>Investigation time by agent (median, s)</h2>
          {data.agent_time.length === 0 ? (
            <p className="muted">No agent runs in this range.</p>
          ) : (
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={data.agent_time.map((row) => ({ ...row, p50: row.p50_s ?? 0 }))} layout="vertical">
                <CartesianGrid strokeDasharray="3 3" horizontal={false} />
                <XAxis type="number" fontSize={12} />
                <YAxis type="category" dataKey="agent" fontSize={12} width={110} />
                <Tooltip />
                <Bar dataKey="p50" name="median (s)" fill={PALETTE[2]} radius={[0, 3, 3, 0]} />
              </BarChart>
            </ResponsiveContainer>
          )}
          {data.agent_time.some((row) => row.problems > 0) && (
            <p className="muted small">
              Failures or timeouts: {data.agent_time.filter((r) => r.problems).map((r) => `${r.agent} ${r.problems}`).join(", ")}
            </p>
          )}
        </section>

        <section className="panel">
          <h2>Approvals and fixes</h2>
          <ul className="plain">
            {data.approvals.by_decision.map((row) => (
              <li key={row.decision}>
                {row.decision}: {row.count}
              </li>
            ))}
            <li>Draft PRs opened: {data.fix_outcomes.pull_requests_opened}</li>
            <li>Blocked by policy: {data.fix_outcomes.policy_denied}</li>
            <li>
              Re-validation: {data.fix_outcomes.revalidation_passed} passed, {data.fix_outcomes.revalidation_failed} failed
            </li>
          </ul>
          <p className="muted small">{data.fix_outcomes.note}</p>
        </section>
      </div>
    </>
  );
}
