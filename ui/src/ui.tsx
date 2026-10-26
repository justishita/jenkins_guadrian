// Small shared pieces. Everything from the API is rendered as text; nothing here uses innerHTML.
import type { ReactNode } from "react";
import type { ReportSummary } from "./api";

export function Badge({ tone = "neutral", children }: { tone?: "ok" | "warn" | "bad" | "neutral" | "info"; children: ReactNode }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

export function when(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

export function seconds(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value < 90) return `${value.toFixed(0)} s`;
  return `${(value / 60).toFixed(1)} min`;
}

export function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${value.toFixed(0)}%`;
}

export function confidence(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toFixed(2);
}

export function ReportBadges({ report }: { report: ReportSummary | null }) {
  if (!report) return <Badge>no report yet</Badge>;
  return (
    <>
      <Badge tone={report.complete ? "ok" : "warn"}>{report.complete ? "complete" : "incomplete"}</Badge>{" "}
      <Badge tone={report.actionable ? "ok" : "neutral"}>{report.actionable ? "actionable" : "not actionable"}</Badge>
      {report.conflicted && (
        <>
          {" "}
          <Badge tone="bad">conflict</Badge>
        </>
      )}
    </>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const tone = status === "OPEN" ? "warn" : status === "RESOLVED" ? "ok" : "neutral";
  return <Badge tone={tone}>{status.toLowerCase()}</Badge>;
}

export function Kpi({ label, value, hint, icon }: { label: string; value: ReactNode; hint?: string; icon?: ReactNode }) {
  return (
    <div className="kpi" title={hint}>
      <div className="kpi-top">
        <div className="kpi-value">{value}</div>
        {icon && <span className="kpi-icon">{icon}</span>}
      </div>
      <div className="kpi-label">{label}</div>
    </div>
  );
}

export function Problem({ error, retry }: { error: unknown; retry?: () => void }) {
  return (
    <div className="panel error" role="alert">
      {error instanceof Error ? error.message : "Something went wrong"}
      {retry && (
        <>
          {" "}
          <button className="ghost" onClick={retry}>
            Retry
          </button>
        </>
      )}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="panel muted empty">{children}</div>;
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}
