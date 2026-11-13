"use client";

import { useRef, useState } from "react";
import { Loader2, Play, Scissors } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Slider } from "@/components/ui/slider";
import { fileUrl, genApi, type SceneTake } from "@/lib/api";
import { t } from "@/i18n";

const MIN_KEEP = 0.5;

/** Keeps a part of a take: two handles on the take's timeline, saved on the server as a new take of the scene. */
export function TakeTrimmer({ jobId, clip, take, initial, onSaved, onCancel }: {
  jobId: string; clip: string; take: SceneTake; initial?: [number, number]; onSaved: (file: string) => void; onCancel: () => void;
}) {
  const s = t().picker;
  const video = useRef<HTMLVideoElement>(null);
  const [length, setLength] = useState(0);
  const [range, setRange] = useState<[number, number]>([0, 0]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [start, end] = range;

  const move = (value: number | readonly number[]) => {
    if (!Array.isArray(value) || value.length < 2) return;
    const [a, b] = value as [number, number];
    if (b - a < MIN_KEEP) return;
    if (video.current) {
      video.current.pause();
      video.current.currentTime = a !== start ? a : b;  // show the frame at the handle being dragged
    }
    setRange([a, b]);
  };
  const preview = () => {
    if (!video.current) return;
    video.current.currentTime = start;
    void video.current.play().catch(() => undefined);
  };
  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const r = await genApi.trim(jobId, { clip, file: take.file, start, end });
      onSaved(r.take.file);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSaving(false);
    }
  };

  return (
    <div className="mt-2 rounded-xl bg-card p-3 ring-1 ring-foreground/10">
      <p className="text-sm font-semibold">{s.trimTitle(take.label)}</p>
      <p className="mb-3 text-xs text-muted-foreground">{s.trimHint}</p>
      <div className="flex flex-col gap-3 sm:flex-row">
        <video ref={video} src={fileUrl(take.url)} muted playsInline preload="metadata"
          className="aspect-9-16 w-32 shrink-0 self-center rounded-lg bg-surface object-cover sm:self-start"
          onLoadedMetadata={(e) => {
            const d = Math.round(e.currentTarget.duration * 10) / 10;
            setLength(d);
            setRange(initial ? [Math.min(initial[0], d), Math.min(initial[1], d)] : [0, d]);
          }}
          onTimeUpdate={(e) => { if (e.currentTarget.currentTime >= end) e.currentTarget.pause(); }} />
        <div className="min-w-0 flex-1 space-y-3">
          {length > 0 ? (
            <>
              <Slider min={0} max={length} step={0.1} value={range} onValueChange={move} aria-label={s.trimRange} />
              <p className="text-sm tabular-nums">{s.trimKeep(start.toFixed(1), end.toFixed(1), (end - start).toFixed(1))}</p>
            </>
          ) : <Loader2 className="size-4 animate-spin text-muted-foreground" aria-hidden />}
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="outline" className="rounded-full" onClick={preview} disabled={!length}><Play className="size-3" />{s.trimPreview}</Button>
            <Button size="sm" className="rounded-full" onClick={save} disabled={!length || saving}>
              {saving ? <Loader2 className="size-3 animate-spin" /> : <Scissors className="size-3" />}{saving ? s.trimSaving : s.trimSave}
            </Button>
            <Button size="sm" variant="ghost" className="rounded-full" onClick={onCancel} disabled={saving}>{s.trimCancel}</Button>
          </div>
          {error && <p className="text-sm text-destructive">{error}</p>}
        </div>
      </div>
    </div>
  );
}
