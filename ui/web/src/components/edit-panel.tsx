"use client";

import { ChevronDown, Loader2, RotateCcw } from "lucide-react";
import { useState } from "react";
import { usd } from "@/components/cost-breakdown";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { api, ApiError, type Job } from "@/lib/api";
import { t } from "@/i18n";
import { cn } from "@/lib/utils";

const REDO_STEPS = ["plan", "people", "pieces", "edit", "assemble", "check"] as const;
const fmtS = (v: number) => `${v.toFixed(1)} s`;

/** A sample edit (no agent): what the planner decided, the pieces sent to Seedance, the prompt, and Try again / Redo. */
export function EditPanel({ job, onChanged }: { job: Job; onChanged?: () => void }) {
  const s = t();
  const ed = job.edit;
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [redo, setRedo] = useState<(typeof REDO_STEPS)[number]>("edit");
  const [promptOpen, setPromptOpen] = useState(false);
  if (!ed) return null;
  const plan = ed.plan;
  const working = job.status === "running" || job.status === "queued" || !!ed.running;
  const run = async (from: string | null) => {
    setBusy(true);
    setErr(null);
    try { await api.retryEdit(job.id, from); onChanged?.(); } catch (e) { setErr(e instanceof ApiError ? e.message : String(e)); } finally { setBusy(false); }
  };
  return (
    <section aria-labelledby="edit-panel" className="space-y-4 rounded-3xl bg-card p-5 shadow-soft ring-1 ring-foreground/8 sm:p-6">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 id="edit-panel" className="font-display text-2xl font-medium">{s.edit.view.title}</h2>
        {ed.estimate?.total_usd != null && <span className="text-sm text-muted-foreground">{s.edit.view.estimate(usd(ed.estimate.total_usd))}</span>}
      </div>
      {plan ? (
        <dl className="grid gap-3 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-xs font-semibold text-muted-foreground">{s.edit.view.replace}</dt>
            <dd className="mt-1 space-y-1">
              {(plan.products ?? []).map((p, i) => <p key={`p${i}`}>{p.what} <span className="text-muted-foreground">{s.edit.view.withImage(p.image)}</span></p>)}
              {(plan.people ?? []).map((p, i) => (
                <p key={`h${i}`}>{p.what} <span className="text-muted-foreground">{p.new_person ? s.edit.view.newPerson : s.edit.view.withPerson(p.image ?? i + 1)}</span></p>
              ))}
              {ed.options.people === "keep" && <p className="text-muted-foreground">{s.edit.view.keptPeople}</p>}
              {plan.use_until && ed.sample_s && plan.use_until < ed.sample_s - 0.2 ? <p className="text-muted-foreground">{s.edit.view.usedUntil(fmtS(plan.use_until))}</p> : null}
            </dd>
          </div>
          <div>
            <dt className="text-xs font-semibold text-muted-foreground">{s.edit.view.captions}</dt>
            <dd className="mt-1 space-y-1">
              {ed.options.captions === "full" && (plan.captions ?? []).map((c, i) => <p key={i}>“{c.text}” <span className="text-xs text-muted-foreground tabular-nums">{fmtS(c.start)}-{fmtS(c.end)}</span></p>)}
              {ed.options.captions !== "none" && plan.cta && <p><span className="text-muted-foreground">{s.edit.view.cta}:</span> {[ed.product_name, ed.offer, plan.cta].filter(Boolean).join(" · ")}</p>}
            </dd>
          </div>
        </dl>
      ) : null}
      {ed.pieces.length ? (
        <div className="text-sm">
          <p className="text-xs font-semibold text-muted-foreground">{s.edit.view.pieces} · {ed.via_assets ? s.edit.view.viaAssets : s.edit.view.viaLink}</p>
          <ul className="mt-1 flex flex-wrap gap-2">
            {ed.pieces.map((p) => (
              <li key={p.n} className={cn("rounded-full px-2.5 py-1 text-xs ring-1", p.status === "succeeded" ? "ring-success/40" : p.status === "failed" ? "ring-danger/50" : "ring-foreground/12")}>
                {s.edit.view.piece(p.n, fmtS(p.start), fmtS(p.end))}{p.status && p.status !== "succeeded" ? ` · ${p.status}` : ""}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {ed.prompt ? (
        <Collapsible open={promptOpen} onOpenChange={setPromptOpen}>
          <CollapsibleTrigger className="flex items-center gap-1.5 text-sm font-semibold text-brand outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50">
            {s.edit.view.prompt}<ChevronDown className={cn("size-4 transition-transform", promptOpen && "rotate-180")} aria-hidden />
          </CollapsibleTrigger>
          <CollapsibleContent><p className="mt-2 whitespace-pre-wrap rounded-xl bg-muted p-3 text-sm leading-relaxed">{ed.prompt}</p></CollapsibleContent>
        </Collapsible>
      ) : null}
      <p className="text-xs text-muted-foreground">{s.edit.rights}</p>
      {!job.read_only && !working && (
        <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
          {job.status === "failed" && (
            <Button className="rounded-full bg-brand text-brand-foreground hover:bg-brand/90" onClick={() => void run(null)} disabled={busy}>
              {busy ? <Loader2 className="size-4 animate-spin" /> : <RotateCcw className="size-4" />}{s.edit.view.retry}
            </Button>
          )}
          <label className="flex items-center gap-2 text-sm">
            <span className="text-muted-foreground">{s.edit.view.redo}</span>
            <select value={redo} onChange={(e) => setRedo(e.target.value as (typeof REDO_STEPS)[number])} className="h-9 rounded-lg bg-muted px-2 text-sm">
              {REDO_STEPS.filter((k) => k !== "people" || ed.options.people === "generate").map((k) => {
                const n = { plan: 2, people: 3, pieces: 4, edit: 5, assemble: 6, check: 7 }[k];
                return <option key={k} value={k}>{s.edit.stepNames[n]}</option>;
              })}
            </select>
          </label>
          <Button variant="outline" className="rounded-full" onClick={() => void run(redo)} disabled={busy}>{s.edit.view.redo}</Button>
          <span className="basis-full text-xs text-muted-foreground">{s.edit.view.redoHint}</span>
        </div>
      )}
      {err && <p role="alert" className="text-sm text-danger">{err}</p>}
    </section>
  );
}
