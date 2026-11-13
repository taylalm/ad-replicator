"use client";

import { useRef, useState } from "react";
import { Film, Loader2, Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { api, fileUrl, scenesApi, type OwnClip, type SceneRow } from "@/lib/api";
import { t } from "@/i18n";
import { cn } from "@/lib/utils";

const selectClass = "h-9 rounded-lg bg-background px-2 text-sm ring-1 ring-foreground/12 outline-none focus-visible:ring-3 focus-visible:ring-ring/50";

/** The user's own footage in the ad (an intro, an outro, a scene they filmed) and the transition between scenes.
 *  Changes are local until the scene list's button rebuilds the ad. */
export function OwnClips({ jobId, rows, extras, onExtras, transitions, transitionNames, onTransitions }: {
  jobId: string; rows: SceneRow[]; extras: OwnClip[]; onExtras: (next: OwnClip[]) => void;
  transitions: string; transitionNames: string[]; onTransitions: (v: string) => void;
}) {
  const s = t().picker;
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const change = (id: string, patch: Partial<OwnClip>) => onExtras(extras.map((x) => (x.id === id ? { ...x, ...patch } : x)));

  const add = async (files: FileList | null) => {
    if (!files?.length) return;
    setError(null);
    let list = extras;
    for (const f of Array.from(files)) {
      setBusy(f.name);
      try {
        const up = await api.uploadToJob(jobId, f, "own clip for the ad");
        const r = await scenesApi.addOwn(jobId, up.file_id, f.name);
        list = [...list, r.extra];
        onExtras(list);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    }
    setBusy(null);
    if (input.current) input.current.value = "";
  };
  const remove = async (id: string) => {
    onExtras(extras.filter((x) => x.id !== id));
    try { await scenesApi.removeOwn(jobId, id); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  return (
    <div className="mt-4 space-y-4 border-t border-border pt-4">
      <div>
        <label className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-semibold">{s.transitions.label}</span>
          <select className={selectClass} value={transitions} onChange={(e) => onTransitions(e.target.value)}>
            {transitionNames.map((n) => <option key={n} value={n}>{s.transitions.names[n] ?? n}</option>)}
          </select>
        </label>
        <p className="mt-1 text-xs text-muted-foreground">{s.transitions.hint}</p>
      </div>
      <div>
        <h4 className="text-sm font-semibold">{s.own.title}</h4>
        <p className="mb-2 text-xs text-muted-foreground">{s.own.hint}</p>
        <ul className="space-y-2">
          {extras.map((x) => {
            const length = x.length ?? 0;
            return (
              <li key={x.id} className={cn("flex flex-col gap-3 rounded-xl bg-muted/40 p-3 sm:flex-row", !x.include && "opacity-60")}>
                {x.kind === "video"
                  ? <video src={`${fileUrl(x.url)}#t=0.5`} muted playsInline controls preload="metadata" className="aspect-9-16 w-24 shrink-0 self-center rounded-lg bg-surface object-cover sm:self-start" />
                  // eslint-disable-next-line @next/next/no-img-element
                  : <img src={fileUrl(x.url)} alt="" className="aspect-9-16 w-24 shrink-0 self-center rounded-lg bg-surface object-cover sm:self-start" />}
                <div className="min-w-0 flex-1 space-y-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <Film className="size-4 text-muted-foreground" aria-hidden />
                    <span className="min-w-0 truncate text-sm font-medium">{x.label}</span>
                    <label className="ml-auto inline-flex items-center gap-2 text-xs">
                      <Switch checked={x.include} onCheckedChange={(v: boolean) => change(x.id, { include: v })} aria-label={`${x.label}: ${s.own.inAd}`} />
                      {x.include ? s.own.inAd : s.own.left}
                    </label>
                  </div>
                  <label className="flex flex-wrap items-center gap-2 text-sm">
                    <span className="text-muted-foreground">{s.own.where}</span>
                    <select className={selectClass} value={x.after} onChange={(e) => change(x.id, { after: e.target.value })}>
                      <option value="start">{s.own.start}</option>
                      {rows.map((r) => <option key={r.id} value={r.id}>{s.own.afterScene(r.n)}</option>)}
                      <option value="end">{s.own.end}</option>
                      {x.kind === "video" && <option value="ending">{s.own.ending}</option>}
                    </select>
                  </label>
                  {x.kind === "video" && length > 0.6 ? (
                    <div>
                      <p className="mb-1 text-xs text-muted-foreground">{s.own.part}: <span className="tabular-nums">{s.own.keep(x.start.toFixed(1), x.end.toFixed(1), (x.end - x.start).toFixed(1))}</span></p>
                      <Slider min={0} max={length} step={0.1} value={[x.start, x.end]} aria-label={s.own.part}
                        onValueChange={(v: number | readonly number[]) => {
                          if (Array.isArray(v) && v.length === 2 && v[1] - v[0] >= 0.5) change(x.id, { start: v[0], end: v[1] });
                        }} />
                    </div>
                  ) : x.kind === "image" ? (
                    <label className="flex items-center gap-2 text-sm">
                      <span className="text-muted-foreground">{s.own.seconds}</span>
                      <input type="number" min={0.5} max={8} step={0.5} value={x.end - x.start} className={cn(selectClass, "w-20")}
                        onChange={(e) => change(x.id, { start: 0, end: Math.min(8, Math.max(0.5, Number(e.target.value) || 2.5)) })} />
                    </label>
                  ) : null}
                  <div className="flex flex-wrap items-center gap-3">
                    {x.kind === "video" && (
                      <label className="inline-flex items-center gap-2 text-xs">
                        <Switch checked={x.mute} onCheckedChange={(v: boolean) => change(x.id, { mute: v })} aria-label={`${x.label}: ${s.own.mute}`} />
                        {s.own.mute}
                      </label>
                    )}
                    <button type="button" onClick={() => void remove(x.id)}
                      className="ml-auto inline-flex items-center gap-1 rounded px-1 text-xs text-muted-foreground outline-none hover:text-destructive focus-visible:ring-3 focus-visible:ring-ring/50">
                      <Trash2 className="size-3" aria-hidden />{s.own.remove}
                    </button>
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
        <input ref={input} type="file" accept="video/mp4,video/quicktime,video/webm,image/png,image/jpeg,image/webp" multiple hidden
          onChange={(e) => void add(e.target.files)} />
        <Button variant="outline" size="sm" className="mt-2 rounded-full" disabled={!!busy} onClick={() => input.current?.click()}>
          {busy ? <Loader2 className="size-3 animate-spin" /> : <Plus className="size-3" />}{busy ? s.own.uploading(busy) : s.own.add}
        </Button>
        {error && <p role="alert" className="mt-2 text-sm text-destructive">{error}</p>}
      </div>
    </div>
  );
}
