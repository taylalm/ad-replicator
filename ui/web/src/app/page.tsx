"use client";

import Link from "next/link";
import { ChevronRight, Folder, Plus, Sparkles } from "lucide-react";
import { useEffect, useState } from "react";
import { JobCard } from "@/components/job-card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { api, type JobRow } from "@/lib/api";
import { t } from "@/i18n";
import { SHOWCASE } from "@/lib/utils";

const DEMO_KEY = "ad-replicator-demo-mode";

export default function HistoryPage() {
  const s = t();
  const [jobs, setJobs] = useState<JobRow[] | null>(null);
  const [demo, setDemo] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const r = await api.jobs();
        if (!alive) return;
        setJobs(r.jobs);
        setError(null);
        setDemo((prev) => {
          if (prev != null) return prev;
          try {
            const stored = window.localStorage.getItem(DEMO_KEY);
            if (stored === "1" || stored === "0") return stored === "1";
          } catch { /* ignore */ }
          return !r.live_running;
        });
      } catch (e) {
        if (alive) setError(String(e));
      }
    };
    void load();
    const iv = window.setInterval(load, 8000);
    return () => { alive = false; window.clearInterval(iv); };
  }, []);

  const toggleDemo = (v: boolean) => {
    setDemo(v);
    try { window.localStorage.setItem(DEMO_KEY, v ? "1" : "0"); } catch { /* ignore */ }
  };

  const visible = (jobs ?? []).filter((j) => (demo ? true : !j.demo));
  const demoCount = (jobs ?? []).filter((j) => j.demo).length;
  const mainList = visible.filter((j) => !j.folder);
  const folderNames = Array.from(new Set((jobs ?? []).map((j) => j.folder).filter((f): f is string => !!f))).sort();
  const folderGroups = folderNames.map((name) => ({ name, jobs: visible.filter((j) => j.folder === name) })).filter((g) => g.jobs.length);
  const removeJob = (id: string) => setJobs((prev) => (prev || []).filter((x) => x.id !== id));
  const renameJob = (id: string, title: string) => setJobs((prev) => (prev || []).map((x) => (x.id === id ? { ...x, title } : x)));
  const moveJob = (id: string, folder: string | null) => setJobs((prev) => (prev || []).map((x) => (x.id === id ? { ...x, folder } : x)));
  const card = (j: JobRow, i: number) => (
    <JobCard key={j.id} job={j} index={i} onDeleted={removeJob} onRenamed={renameJob} folders={folderNames} onMoved={SHOWCASE ? undefined : moveJob} />
  );

  return (
    <div className="space-y-8">
      <header className="flex flex-col gap-6 sm:flex-row sm:items-end sm:justify-between">
        <div className="max-w-xl">
          <h1 className="font-display text-4xl font-medium tracking-tight sm:text-5xl">{s.history.title}</h1>
          <p className="mt-3 text-[15px] leading-relaxed text-muted-foreground">{s.history.subtitle}</p>
        </div>
        {!SHOWCASE && (
          <Button size="lg" className="h-11 rounded-full bg-brand px-5 text-base text-brand-foreground shadow-soft hover:bg-brand/90" nativeButton={false} render={<Link href="/new" />}>
            <Plus className="size-4" />
            {s.history.newVideo}
          </Button>
        )}
      </header>

      {demoCount > 0 && (
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl bg-card px-4 py-3 ring-1 ring-foreground/8">
        <label className="flex cursor-pointer items-center gap-3">
          <Switch checked={!!demo} onCheckedChange={(v) => toggleDemo(!!v)} aria-label={s.history.demoMode} />
          <span>
            <span className="block text-sm font-semibold">{s.history.demoMode}</span>
            <span className="block text-xs text-muted-foreground">{s.history.demoHint}</span>
          </span>
        </label>
        {demo && demoCount > 0 && (
          <Link href="/new?demo=1" className="inline-flex h-9 items-center gap-1.5 rounded-full bg-brand-soft px-4 text-sm font-semibold text-foreground outline-none hover:bg-brand/20 focus-visible:ring-3 focus-visible:ring-ring/50">
            <Sparkles className="size-4 text-brand" aria-hidden />
            {s.history.tryDemoFlow}
          </Link>
        )}
      </div>
      )}

      {error && <p role="alert" className="rounded-2xl bg-danger-soft p-4 text-sm">{s.app.backendDown}</p>}

      {jobs === null && !error ? (
        <ul className="grid gap-4 md:grid-cols-2" aria-busy="true" aria-label={s.app.loading}>
          {[0, 1, 2, 3].map((i) => (
            <li key={i} className="flex gap-4 rounded-2xl bg-card p-4 ring-1 ring-foreground/8">
              <Skeleton className="aspect-9-16 w-28 rounded-xl" />
              <div className="flex-1 space-y-3 py-1"><Skeleton className="h-5 w-32" /><Skeleton className="h-6 w-3/4" /><Skeleton className="h-4 w-1/2" /></div>
            </li>
          ))}
        </ul>
      ) : visible.length === 0 ? (
        <div className="rounded-3xl border border-dashed border-border bg-card/60 px-6 py-16 text-center">
          <div className="mx-auto grid size-14 place-items-center rounded-2xl bg-brand-soft text-brand"><Sparkles className="size-6" aria-hidden /></div>
          <h2 className="mt-4 font-display text-2xl font-medium">{s.history.emptyTitle}</h2>
          <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-muted-foreground">{demo && demoCount === 0 ? s.history.emptyDemo : s.history.emptyBody}</p>
          {!SHOWCASE && (
            <Button className="mt-6 rounded-full bg-brand text-brand-foreground hover:bg-brand/90" size="lg" nativeButton={false} render={<Link href="/new" />}>
              <Plus className="size-4" />{s.history.newVideo}
            </Button>
          )}
        </div>
      ) : (
        <div className="space-y-6">
          {mainList.length > 0 && <ul className="grid gap-4 md:grid-cols-2">{mainList.map(card)}</ul>}
          {folderGroups.map((g) => (
            <details key={g.name} className="group/folder rounded-2xl bg-card/60 ring-1 ring-foreground/8 open:pb-4">
              <summary className="flex cursor-pointer list-none items-center gap-2 rounded-2xl px-4 py-3 outline-none hover:bg-muted/60 focus-visible:ring-3 focus-visible:ring-ring/50 [&::-webkit-details-marker]:hidden">
                <ChevronRight className="size-4 text-muted-foreground transition-transform group-open/folder:rotate-90" aria-hidden />
                <Folder className="size-4 text-brand" aria-hidden />
                <span className="font-display text-lg font-medium">{g.name}</span>
                <span className="text-sm text-muted-foreground">· {s.history.folderCount(g.jobs.length)}</span>
              </summary>
              <ul className="grid gap-4 px-4 pt-2 md:grid-cols-2">{g.jobs.map(card)}</ul>
            </details>
          ))}
        </div>
      )}
    </div>
  );
}
