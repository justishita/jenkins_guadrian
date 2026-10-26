// Small shared pieces. Everything from the API is rendered as text; nothing here uses innerHTML.
import type { ReactNode } from "react";
import { ChevronLeftIcon, ChevronRightIcon } from "./icons";
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
      <Badge tone={report.actionable ? "info" : "neutral"}>{report.actionable ? "actionable" : "not actionable"}</Badge>
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

/** Title, one line of explanation and optional controls on the right: the head of a content panel. */
export function PanelHead({ title, subtitle, children }: { title: string; subtitle?: string; children?: ReactNode }) {
  return (
    <div className="panel-head">
      <div>
        <h2>{title}</h2>
        {subtitle && <p className="muted">{subtitle}</p>}
      </div>
      {children && <div className="panel-tools">{children}</div>}
    </div>
  );
}

export function Pager({ offset, page, total, onChange }: { offset: number; page: number; total: number; onChange: (offset: number) => void }) {
  return (
    <div className="pager">
      <span className="muted">
        {total === 0 ? 0 : offset + 1}–{Math.min(offset + page, total)} of {total}
      </span>
      <span className="row">
        <button className="ghost" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - page))}>
          <ChevronLeftIcon size={16} /> Previous
        </button>
        <button disabled={offset + page >= total} onClick={() => onChange(offset + page)}>
          Next <ChevronRightIcon size={16} />
        </button>
      </span>
    </div>
  );
}
