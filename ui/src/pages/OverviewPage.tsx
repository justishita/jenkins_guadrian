import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, Cell, LabelList, Legend, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api";
import { ArrowRightIcon, CalendarIcon, ChevronIcon, ClockIcon, DocIcon, EyeIcon, PercentIcon, TrendIcon } from "../icons";
import { Kpi, Problem, percent, seconds } from "../ui";
import { useProject } from "./ProjectLayout";

const BLUE = "#3b7ddd";
const PIE = ["#3b8fe8", "#6b6fa8", "#35b3a3", "#f0a04b", "#e0607e", "#8b6fe0"];
const AXIS = { fontSize: 12, fill: "#5b6b86" };

export function OverviewPage() {
  const project = useProject();
  const [days, setDays] = useState(14);
  const stats = useQuery({ queryKey: ["stats", project.id, days], queryFn: () => api.stats(project.id, days) });

  if (stats.isLoading) return <p className="muted">Loading…</p>;
  if (stats.isError) return <Problem error={stats.error} retry={() => void stats.refetch()} />;
  const data = stats.data!;
  const totalVerdicts = data.verdicts.reduce((sum, row) => sum + row.count, 0);

  return (
    <>
      <div className="row between toolbar">
        <label className="range">
          <span className="muted">Range</span>
          <span className="select-pill">
            <CalendarIcon size={18} />
            <select value={days} onChange={(event) => setDays(Number(event.target.value))} aria-label="Range">
              <option value={7}>7 days</option>
              <option value={14}>14 days</option>
              <option value={30}>30 days</option>
              <option value={90}>90 days</option>
            </select>
            <ChevronIcon size={16} />
          </span>
        </label>
        <Link to="dashboards" className="link-strong">
          View Grafana dashboards <ArrowRightIcon size={16} />
        </Link>
      </div>

      <div className="kpis">
        <Kpi label="Incidents" value={data.incidents_total} icon={<DocIcon size={18} />} />
        <Kpi label="Awaiting review" value={data.approvals.pending} icon={<EyeIcon size={18} />} />
        <Kpi label="Approval rate" value={percent(data.approvals.approval_rate_pct)} hint={`${data.approvals.decided} decided`} icon={<PercentIcon size={18} />} />
        <Kpi label="Time to report (median)" value={seconds(data.reports.time_to_report_p50_s)} hint={`p95 ${seconds(data.reports.time_to_report_p95_s)}`} icon={<ClockIcon size={18} />} />
        <Kpi label="Fix success (accuracy proxy)" value={percent(data.fix_outcomes.fix_success_pct)} hint={data.fix_outcomes.note} icon={<TrendIcon size={18} />} />
        <Kpi label="Incomplete reports" value={`${data.reports.incomplete} / ${data.reports.total}`} icon={<DocIcon size={18} />} />
      </div>

      <div className="charts">
        <section className="panel">
          <h2>Incidents per day</h2>
          <ResponsiveContainer width="100%" height={230}>
            <BarChart data={data.incidents_per_day} margin={{ top: 18, right: 8, left: -18, bottom: 0 }}>
              <CartesianGrid strokeDasharray="4 4" vertical={false} stroke="#d9e0ec" />
              <XAxis dataKey="date" tickFormatter={(d: string) => d.slice(5)} tick={AXIS} axisLine={{ stroke: "#c7d0df" }} tickLine={false} interval="preserveStartEnd" />
              <YAxis allowDecimals={false} tick={AXIS} axisLine={false} tickLine={false} />
              <Tooltip cursor={{ fill: "rgba(59,125,221,0.08)" }} />
              <Bar dataKey="incidents" fill={BLUE} radius={[4, 4, 0, 0]} maxBarSize={40}>
                <LabelList dataKey="incidents" position="top" fill="#1f3a68" fontSize={12} formatter={(value: number) => (value > 0 ? value : "")} />
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </section>

        <section className="panel">
          <h2>Root-cause verdicts</h2>
          {data.verdicts.length === 0 ? (
            <p className="muted">No reports in this range.</p>
          ) : (
            <ResponsiveContainer width="100%" height={230}>
              <PieChart>
                <Pie
                  data={data.verdicts}
                  dataKey="count"
                  nameKey="verdict"
                  cx="38%"
                  outerRadius={88}
                  stroke="#fff"
                  strokeWidth={2}
                  labelLine={false}
                  label={({ cx, cy, midAngle, innerRadius, outerRadius, value }: { cx: number; cy: number; midAngle: number; innerRadius: number; outerRadius: number; value: number }) => {
                    const radius = innerRadius + (outerRadius - innerRadius) * 0.55;
                    const x = cx + radius * Math.cos((-midAngle * Math.PI) / 180);
                    const y = cy + radius * Math.sin((-midAngle * Math.PI) / 180);
                    return (
                      <text x={x} y={y} fill="#fff" fontSize={13} fontWeight={600} textAnchor="middle" dominantBaseline="central">
                        {Math.round((100 * value) / totalVerdicts)}%
                      </text>
                    );
                  }}
                >
                  {data.verdicts.map((entry, index) => (
                    <Cell key={entry.verdict} fill={PIE[index % PIE.length]} />
                  ))}
                </Pie>
                <Legend layout="vertical" align="right" verticalAlign="middle" iconType="circle" />
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
            <ResponsiveContainer width="100%" height={230}>
              <BarChart data={data.agent_time.map((row) => ({ ...row, p50: row.p50_s ?? 0 }))} layout="vertical" margin={{ top: 4, right: 28, left: 8, bottom: 0 }}>
                <CartesianGrid strokeDasharray="4 4" horizontal={false} stroke="#d9e0ec" />
                <XAxis type="number" tick={AXIS} axisLine={{ stroke: "#c7d0df" }} tickLine={false} />
                <YAxis type="category" dataKey="agent" tick={AXIS} width={105} axisLine={false} tickLine={false} />
                <Tooltip cursor={{ fill: "rgba(59,125,221,0.08)" }} />
                <Bar dataKey="p50" name="median (s)" fill={BLUE} radius={[0, 4, 4, 0]} maxBarSize={34}>
                  <LabelList dataKey="p50" position="right" fill="#1f3a68" fontSize={12} />
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          )}
          {data.agent_time.some((row) => row.problems > 0) && (
            <p className="muted small">
              Failures or timeouts: {data.agent_time.filter((r) => r.problems).map((r) => `${r.agent} ${r.problems}`).join(", ")}
            </p>
          )}
        </section>
      </div>

      <section className="panel strip-panel">
        <h2>Approvals and fixes</h2>
        <div className="strip">
          <div>
            <span className="strip-number">{data.fix_outcomes.pull_requests_opened}</span>
            <span className="muted">Draft PRs opened</span>
          </div>
          <div>
            <span className="strip-number">{data.fix_outcomes.policy_denied}</span>
            <span className="muted">Blocked by policy</span>
          </div>
          <div>
            <span className="strip-number">
              {data.fix_outcomes.revalidation_passed} <small>passed,</small> {data.fix_outcomes.revalidation_failed} <small>failed</small>
            </span>
            <span className="muted">Re-validation</span>
          </div>
        </div>
        {data.approvals.by_decision.length > 0 && (
          <p className="muted small">Decisions: {data.approvals.by_decision.map((row) => `${row.decision} ${row.count}`).join(" · ")}</p>
        )}
        <p className="note">{data.fix_outcomes.note}</p>
      </section>
    </>
  );
}
