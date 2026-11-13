"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Bot, Loader2, RotateCcw, Scissors, Send, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { OwnClips } from "@/components/own-clips";
import { TakeTrimmer } from "@/components/take-trimmer";
import { fileUrl, scenesApi, type ExportState, type Job, type OwnClip, type ScenePick, type SceneRow, type SceneTake } from "@/lib/api";
import { t } from "@/i18n";
import { cn } from "@/lib/utils";

type Status = { state: ExportState["state"] | "saved" | "unchanged" | "error"; error?: string | null };

/** One take per scene (agent's cut, its earlier takes, takes made on the site), a scene can be left out; one button
 *  rebuilds the ad with the choices, or hands them to the agent while it is still making the ad. */
export function ScenePicker({ job, refreshKey, onUpdated }: { job: Job; refreshKey: string; onUpdated?: () => void }) {
  const s = t().picker;
  const [rows, setRows] = useState<SceneRow[] | null>(null);
  const [assembled, setAssembled] = useState(false);
  const [picks, setPicks] = useState<Record<string, ScenePick>>({});
  const [status, setStatus] = useState<Status | null>(null);
  const [trimming, setTrimming] = useState<{ clip: string; take: SceneTake } | null>(null);
  const [trimmed, setTrimmed] = useState<string | null>(null);  // scene whose new trimmed take was just saved
  const [reload, setReload] = useState(0);
  // the user's own clips and the transition between scenes: local until the button below applies them
  const [extras, setExtras] = useState<OwnClip[]>([]);
  const [transitions, setTransitions] = useState("keep");
  const [transitionNames, setTransitionNames] = useState<string[]>(["keep", "cut", "mix"]);
  const [extraDirty, setExtraDirty] = useState(false);
  const extraDirtyRef = useRef(false);
  const touchExtras = (next: OwnClip[]) => { extraDirtyRef.current = true; setExtraDirty(true); setExtras(next); };
  const touchTransitions = (v: string) => { extraDirtyRef.current = true; setExtraDirty(true); setTransitions(v); };
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);
  // the page re-renders every few seconds while it polls the job: keep the callback in a ref so that does not reload
  const updated = useRef(onUpdated);
  useEffect(() => { updated.current = onUpdated; }, [onUpdated]);
  // scenes the user changed and has not applied yet: a reload (new take, finished rebuild) must not undo them
  const dirty = useRef<Set<string>>(new Set());
  const choose = (id: string, pick: ScenePick) => {
    dirty.current.add(id);
    setPicks((x) => ({ ...x, [id]: pick }));
  };

  const watch = useCallback(() => {
    if (poll.current) clearInterval(poll.current);
    setStatus({ state: "running" });
    let misses = 0;  // several server instances: one that has not heard of this update answers "idle" for a moment
    poll.current = setInterval(async () => {
      const st: ExportState | null = await fetch(fileUrl(`/api/jobs/${encodeURIComponent(job.id)}/captions/export`)).then((r) => r.json()).catch(() => null);
      if (!st || st.state === "running") return;
      if ((st.state as string) === "idle" && ++misses < 20) return;
      if (poll.current) clearInterval(poll.current);
      setStatus({ state: st.state, error: st.error });
      if (st.state === "done") updated.current?.();
    }, 3000);
  }, [job.id]);

  useEffect(() => {
    let live = true;
    scenesApi.get(job.id).then((r) => {
      if (!live) return;
      setRows(r.scenes);
      setAssembled(r.assembled);
      setPicks((cur) => Object.fromEntries(r.scenes.map((x) => [x.id,
        dirty.current.has(x.id) && cur[x.id] && r.scenes.find((y) => y.id === x.id)?.takes.some((tk) => tk.file === cur[x.id].file)
          ? cur[x.id] : { file: x.chosen, include: x.include }])));
      if (r.transition_names?.length) setTransitionNames(r.transition_names);
      if (!extraDirtyRef.current) {  // unsaved placement of own clips survives a reload of the list
        setExtras(r.extras ?? []);
        setTransitions(r.transitions ?? "keep");
      }
      if (r.export?.state === "running") watch();
    }).catch(() => { if (live) setRows([]); });
    return () => { live = false; };
  }, [job.id, refreshKey, reload, watch]);
  useEffect(() => () => { if (poll.current) clearInterval(poll.current); }, []);

  if (!rows || rows.length === 0 || job.demo || job.read_only) return null;

  const changed = rows.filter((r) => {
    const p = picks[r.id];
    return p && (p.include === false || p.file !== r.in_cut);
  }).length;
  const kept = rows.filter((r) => picks[r.id]?.include !== false).length;

  const apply = async () => {
    if (kept === 0) { setStatus({ state: "error", error: s.keepOne }); return; }
    setStatus({ state: "running" });
    try {
      const r = await scenesApi.set(job.id, picks, true, extras, transitions);
      dirty.current.clear();  // saved: the server's choices are now the user's
      extraDirtyRef.current = false;
      setExtraDirty(false);
      if (r.state === "running") watch();
      else setStatus({ state: r.state, error: r.error });
    } catch (e) {
      setStatus({ state: "error", error: e instanceof Error ? e.message : String(e) });
    }
  };

  return (
    <section aria-labelledby="scene-picker" className="mb-5 rounded-2xl bg-card p-4 shadow-soft ring-1 ring-foreground/8 sm:p-5">
      <h3 id="scene-picker" className="font-display text-lg font-medium">{s.title}</h3>
      <p className="mb-3 text-sm text-muted-foreground">{s.hint}</p>
      <ol className="space-y-3">
        {rows.map((r) => {
          const p = picks[r.id] ?? { file: r.chosen, include: true };
          return (
            <li key={r.id} className="rounded-xl bg-muted/40 p-3">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <span className="text-sm font-semibold">{s.scene(r.n)}</span>
                <span className="text-xs text-muted-foreground">{r.id}</span>
                <label className="ml-auto inline-flex items-center gap-2 text-xs">
                  <Switch checked={p.include} onCheckedChange={(v: boolean) => choose(r.id, { ...p, include: v })} aria-label={`${s.scene(r.n)}: ${s.inAd}`} />
                  {p.include ? s.inAd : s.left}
                </label>
              </div>
              <div role="radiogroup" aria-label={s.scene(r.n)} className={cn("flex gap-2 overflow-x-auto pb-1", !p.include && "opacity-40")}>
                {r.takes.map((tk) => (
                  <div key={tk.file} className="w-28 shrink-0 sm:w-32">
                    <TakeTile take={tk} selected={p.file === tk.file} disabled={!p.include}
                      onSelect={() => choose(r.id, { ...p, file: tk.file })} />
                    <button type="button" disabled={!p.include} onClick={() => { setTrimmed(null); setTrimming({ clip: r.id, take: tk }); }}
                      className="mt-1 inline-flex items-center gap-1 rounded px-1 text-xs text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
                      <Scissors className="size-3" aria-hidden />{s.trim}<span className="sr-only">: {tk.label}</span>
                    </button>
                  </div>
                ))}
              </div>
              {r.window && p.file === r.in_cut && (
                <p className={cn("mt-1 text-xs", r.cuts_payoff ? "font-medium text-destructive" : "text-muted-foreground")}>
                  {s.inAdPart(r.window[0].toFixed(1), r.window[1].toFixed(1), r.length ? r.length.toFixed(1) : null)}
                  {r.cuts_payoff ? `. ${s.cutsPayoff}` : ""}
                </p>
              )}
              {trimming?.clip === r.id && (
                <TakeTrimmer key={trimming.take.file} jobId={job.id} clip={r.id} take={trimming.take} onCancel={() => setTrimming(null)}
                  initial={trimming.take.file === r.in_cut ? r.window : undefined}
                  onSaved={(file) => { choose(r.id, { ...p, file, include: true }); setTrimming(null); setTrimmed(r.id); setReload((n) => n + 1); }} />
              )}
              {trimmed === r.id && <p className="mt-2 text-sm text-success">{s.trimSaved}</p>}
            </li>
          );
        })}
      </ol>
      <OwnClips jobId={job.id} rows={rows} extras={extras} onExtras={touchExtras}
        transitions={transitions} transitionNames={transitionNames} onTransitions={touchTransitions} />
      <div className="mt-4 flex flex-wrap items-center gap-3">
        <Button className="rounded-full" onClick={apply} disabled={status?.state === "running"}>
          {status?.state === "running" ? <Loader2 className="size-4 animate-spin" /> : assembled ? <RotateCcw className="size-4" /> : <Send className="size-4" />}
          {status?.state === "running" ? s.working : assembled ? s.rebuild : s.send}
        </Button>
        <span className="text-sm text-muted-foreground">{extraDirty && !changed ? s.own.changed : s.changed(changed)}</span>
        {!assembled && <span className="w-full text-xs text-muted-foreground">{s.sendHint}</span>}
        {status?.state === "done" && <span className="w-full text-sm text-success">{s.done}</span>}
        {status?.state === "sent_to_agent" && <span className="w-full text-sm text-success">{s.sent}</span>}
        {status?.state === "unchanged" && <span className="w-full text-sm text-muted-foreground">{s.unchanged}</span>}
        {(status?.state === "failed" || status?.state === "error") && <span className="w-full text-sm text-destructive">{status.error}</span>}
      </div>
    </section>
  );
}

function TakeTile({ take, selected, disabled, onSelect }: { take: SceneTake; selected: boolean; disabled: boolean; onSelect: () => void }) {
  const s = t().picker;
  const video = useRef<HTMLVideoElement>(null);
  const v = take.verdict;
  return (
    <button type="button" role="radio" aria-checked={selected} disabled={disabled} onClick={onSelect}
      onMouseEnter={() => { void video.current?.play().catch(() => undefined); }}
      onMouseLeave={() => { if (video.current) { video.current.pause(); video.current.currentTime = 1; } }}
      title={take.summary ?? undefined}
      className={cn("group w-full rounded-xl p-1 text-left outline-none transition-shadow focus-visible:ring-3 focus-visible:ring-ring/50",
        selected ? "bg-brand-soft ring-2 ring-brand" : "ring-1 ring-foreground/10 hover:ring-foreground/30")}>
      <span className="relative block aspect-9-16 overflow-hidden rounded-lg bg-surface">
        <video ref={video} src={`${fileUrl(take.url)}#t=1`} muted playsInline loop preload="metadata" className="size-full object-cover" />
        <span className="absolute left-1 top-1 inline-flex items-center gap-1 rounded-md bg-background/85 px-1 py-0.5 text-[10px] font-semibold">
          {take.source === "site" ? <Sparkles className="size-3" aria-hidden /> : <Bot className="size-3" aria-hidden />}
          {take.model ?? ""}
        </span>
      </span>
      <span className="mt-1 block truncate text-xs font-medium">{take.label}</span>
      <span className={cn("mt-0.5 inline-flex rounded px-1 text-[10px] font-semibold",
        v === "PASS" ? "bg-success-soft text-success" : v ? "bg-danger/10 text-danger" : "bg-muted text-muted-foreground")}>
        {v ? s.check(v) : s.noCheck}
      </span>
    </button>
  );
}
