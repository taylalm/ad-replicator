"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { CheckCircle2, CircleAlert, FileVideo, ImagePlus, Loader2, Scissors, Upload, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { usd } from "@/components/cost-breakdown";
import { Field, Section, Segmented } from "@/components/form-bits";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { api, ApiError, type EditEstimate, type EditOptions, type Reference } from "@/lib/api";
import { fmtBytes } from "@/lib/format";
import { t } from "@/i18n";
import { cn, SHOWCASE } from "@/lib/utils";

const MODELS = ["seedance-2.0-mini", "seedance-2.0-fast", "seedance-2.5"] as const;
const RESOLUTIONS = ["480p", "720p"] as const;
const CAPTION_MODES = ["full", "cta_only", "none"] as const;
const PEOPLE = ["generate", "upload", "keep"] as const;

type Pic = { key: string; name: string; preview: string; ref?: Reference; error?: string };

export default function EditSamplePage() {
  if (SHOWCASE) return <p className="mx-auto max-w-xl rounded-2xl bg-muted p-6 text-sm">{t().app.showcaseOnly}</p>;
  return <EditForm />;
}

function usePictures() {
  const [pics, setPics] = useState<Pic[]>([]);
  const add = async (files: File[], note: string) => {
    for (const f of files) {
      const key = `${f.name}-${f.size}-${Math.random().toString(36).slice(2, 7)}`;
      setPics((xs) => [...xs, { key, name: f.name, preview: URL.createObjectURL(f) }]);
      try {
        const ref = await api.uploadReference(f, note);
        setPics((xs) => xs.map((x) => (x.key === key ? { ...x, ref } : x)));
      } catch (e) {
        setPics((xs) => xs.map((x) => (x.key === key ? { ...x, error: e instanceof Error ? e.message : String(e) } : x)));
      }
    }
  };
  const remove = (key: string) => setPics((xs) => xs.filter((x) => x.key !== key));
  return { pics, add, remove, busy: pics.some((p) => !p.ref && !p.error), ready: pics.filter((p) => p.ref) };
}

function EditForm() {
  const s = t();
  const router = useRouter();
  const [title, setTitle] = useState("");
  const [sampleMode, setSampleMode] = useState<"upload" | "link">("upload");
  const [sampleUrl, setSampleUrl] = useState("");
  const [sampleFile, setSampleFile] = useState<{ state: "idle" | "uploading" | "done" | "error"; name?: string; size?: number; ref?: Reference; error?: string }>({ state: "idle" });
  const [duration, setDuration] = useState<number | null>(null);
  const products = usePictures();
  const people = usePictures();
  const [productName, setProductName] = useState("");
  const [productText, setProductText] = useState("");
  const [sizeNote, setSizeNote] = useState("");
  const [offer, setOffer] = useState("");
  const [notes, setNotes] = useState("");
  const [opts, setOpts] = useState<EditOptions>({ video_model: "seedance-2.0-mini", resolution: "480p", captions: "full", people: "generate", image_model: "seedream-5.0-pro", check: true });
  const [info, setInfo] = useState<Awaited<ReturnType<typeof api.editMode>> | null>(null);
  const [est, setEst] = useState<EditEstimate | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => { api.editMode().then(setInfo).catch(() => setInfo(null)); }, []);

  // the sample's length, read by the browser, drives the estimate (the real cost comes from the job)
  const readDuration = (src: string) => {
    const v = document.createElement("video");
    v.preload = "metadata";
    v.onloadedmetadata = () => { if (Number.isFinite(v.duration)) setDuration(v.duration); };
    v.src = src;
  };
  const uploadSample = async (f: File) => {
    setSampleFile({ state: "uploading", name: f.name, size: f.size });
    readDuration(URL.createObjectURL(f));
    try {
      const up = await api.uploadReference(f, "sample video");
      setSampleFile({ state: "done", name: f.name, size: f.size, ref: up });
    } catch (err) {
      setSampleFile({ state: "error", name: f.name, size: f.size, error: err instanceof Error ? err.message : String(err) });
    }
  };
  const nPeople = opts.people === "generate" ? 2 : 0;
  useEffect(() => {
    if (!duration) return;
    const id = window.setTimeout(() => {
      api.editEstimate({ seconds: opts.use_until ? Math.min(duration, opts.use_until) : duration, video_model: opts.video_model, resolution: opts.resolution, people_new: nPeople, image_model: opts.image_model })
        .then(setEst).catch(() => setEst(null));
    }, 250);
    return () => window.clearTimeout(id);
  }, [duration, opts.video_model, opts.resolution, opts.image_model, opts.use_until, nPeople]);

  const warnings = useMemo(() => {
    if (!info) return [];
    const w: string[] = [];
    if (!info.bucket) w.push(s.edit.notReady.bucket);
    if (!info.assets.ready) w.push(s.edit.notReady.assets);
    return w;
  }, [info, s.edit.notReady]);

  const validate = () => {
    const e: Record<string, string> = {};
    if (sampleMode === "upload" ? sampleFile.state !== "done" : !/^https:\/\//i.test(sampleUrl.trim())) e.sample = sampleFile.state === "uploading" ? s.edit.errors.uploading : s.edit.errors.sample;
    if (products.busy || people.busy) e.products = s.edit.errors.uploading;
    else if (!products.ready.length) e.products = s.edit.errors.products;
    if (opts.people === "upload" && !people.ready.length) e.people = s.edit.errors.people;
    setErrors(e);
    return !Object.keys(e).length;
  };

  const submit = async (ev: React.FormEvent) => {
    ev.preventDefault();
    if (!validate()) return;
    setSubmitting(true);
    setSubmitError(null);
    const ref = (r: Reference) => ({ file_id: r.file_id, filename: r.filename, local_name: r.local_name });
    try {
      const job = await api.createEditJob({
        title: title.trim() || undefined, sample_url: sampleMode === "link" ? sampleUrl.trim() : "",
        sample_upload: sampleMode === "upload" && sampleFile.ref ? ref(sampleFile.ref) : null,
        product_images: products.ready.map((p) => ref(p.ref!)), person_images: opts.people === "upload" ? people.ready.map((p) => ref(p.ref!)) : [],
        product_name: productName.trim(), product_text: productText.trim(), size_note: sizeNote.trim(), offer: offer.trim(), notes: notes.trim(), options: opts,
      });
      router.push(`/job?id=${job.id}`);
    } catch (e) {
      setSubmitError(e instanceof ApiError && e.status !== 0 ? e.message : s.newJob.errors.generic);
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={submit} className="mx-auto max-w-3xl space-y-8" noValidate>
      <header>
        <h1 className="flex items-center gap-3 font-display text-4xl font-medium tracking-tight sm:text-5xl"><Scissors className="size-8 text-brand" aria-hidden />{s.edit.title}</h1>
        <p className="mt-3 max-w-xl text-[15px] leading-relaxed text-muted-foreground">{s.edit.subtitle}</p>
        <Link href="/new" className="mt-2 inline-block text-sm font-semibold text-brand hover:underline">{s.edit.backToAgent}</Link>
        {warnings.map((w) => (
          <p key={w} className="mt-3 flex items-start gap-2 rounded-2xl bg-warning-soft px-4 py-3 text-sm" role="status"><CircleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />{w}</p>
        ))}
      </header>

      <Section title={s.edit.sections.inputs}>
        <Field label={s.newJob.sample.label} error={errors.sample} id="sample">
          <div role="tablist" aria-label={s.newJob.sample.label} className="mb-2 inline-flex rounded-full bg-muted p-1 text-sm">
            {(["upload", "link"] as const).map((m) => (
              <button key={m} type="button" role="tab" aria-selected={sampleMode === m} onClick={() => setSampleMode(m)}
                className={cn("rounded-full px-3 py-1 font-semibold transition-colors", sampleMode === m ? "bg-card shadow-soft" : "text-muted-foreground hover:text-foreground")}>
                {m === "upload" ? s.newJob.sample.modeUpload : s.newJob.sample.modeLink}
              </button>
            ))}
          </div>
          {sampleMode === "upload" ? (
            <label className={cn("flex cursor-pointer items-center gap-3 rounded-xl border border-dashed px-4 py-4 transition-colors hover:bg-foreground/5", sampleFile.state === "error" ? "border-danger" : "border-foreground/20")}
              onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); const f = e.dataTransfer.files?.[0]; if (f) void uploadSample(f); }}>
              {sampleFile.state === "uploading" ? <Loader2 className="size-5 animate-spin text-muted-foreground" aria-hidden />
                : sampleFile.state === "done" ? <CheckCircle2 className="size-5 text-success" aria-hidden />
                : sampleFile.state === "error" ? <CircleAlert className="size-5 text-danger" aria-hidden />
                : <Upload className="size-5 text-muted-foreground" aria-hidden />}
              <span className="min-w-0 flex-1 text-sm">
                {sampleFile.state === "idle" && (<><span className="block font-semibold">{s.newJob.sample.drop}</span><span className="block text-xs text-muted-foreground">{s.newJob.sample.dropHint}</span></>)}
                {sampleFile.state === "uploading" && <span className="block">{s.newJob.sample.uploading} {sampleFile.name}…</span>}
                {sampleFile.state === "done" && <span className="flex items-center gap-1.5 font-semibold"><FileVideo className="size-4" aria-hidden />{s.newJob.sample.uploaded(sampleFile.name ?? "", fmtBytes(sampleFile.size ?? null))}{duration ? ` · ${duration.toFixed(1)} s` : ""}</span>}
                {sampleFile.state === "error" && <span className="block text-danger">{sampleFile.error}</span>}
              </span>
              <input id="sample" type="file" accept="video/mp4,video/quicktime,video/webm,.mp4,.mov,.m4v,.webm" className="sr-only"
                onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) void uploadSample(f); }} />
            </label>
          ) : (
            <Input id="sample" type="url" inputMode="url" placeholder="https://…/sample.mp4" value={sampleUrl} className="h-11 rounded-xl text-base"
              onChange={(e) => setSampleUrl(e.target.value)} onBlur={() => { if (/^https:\/\//i.test(sampleUrl.trim())) readDuration(sampleUrl.trim()); }} />
          )}
        </Field>

        <Field label={s.edit.products.label} help={s.edit.products.help} error={errors.products} id="products">
          <PictureRow pics={products.pics} onRemove={products.remove} addLabel={s.edit.products.add} max={4} onAdd={(fs) => void products.add(fs, "product picture")} />
        </Field>
        <div className="grid gap-5 sm:grid-cols-2">
          <Field label={s.edit.productName.label} id="pname">
            <Input id="pname" value={productName} onChange={(e) => setProductName(e.target.value)} placeholder={s.edit.productName.placeholder} maxLength={80} className="h-11 rounded-xl" />
          </Field>
          <Field label={s.edit.productText.label} help={s.edit.productText.help} id="ptext">
            <Input id="ptext" value={productText} onChange={(e) => setProductText(e.target.value)} placeholder={s.edit.productText.placeholder} maxLength={160} className="h-11 rounded-xl" />
          </Field>
        </div>
        <Field label={s.edit.sizeNote.label} id="size">
          <Input id="size" value={sizeNote} onChange={(e) => setSizeNote(e.target.value)} placeholder={s.edit.sizeNote.placeholder} maxLength={200} className="h-11 rounded-xl" />
        </Field>
        <Field label={s.newJob.titleField.label} id="title">
          <Input id="title" placeholder={s.newJob.titleField.placeholder} value={title} onChange={(e) => setTitle(e.target.value)} className="h-11 rounded-xl" maxLength={80} />
        </Field>
      </Section>

      <Section title={s.edit.sections.people}>
        <Segmented label={s.edit.people.label} options={PEOPLE.map((p) => ({ v: p, l: s.edit.people[p] }))} value={opts.people} onChange={(v) => setOpts((o) => ({ ...o, people: v as EditOptions["people"] }))} />
        <p className="text-sm text-muted-foreground">{s.edit.people[`${opts.people}Hint` as "keepHint" | "generateHint" | "uploadHint"]}</p>
        {opts.people === "upload" && (
          <Field label={s.edit.people.add} error={errors.people} id="people">
            <PictureRow pics={people.pics} onRemove={people.remove} addLabel={s.edit.people.add} max={3} onAdd={(fs) => void people.add(fs, "person picture")} />
          </Field>
        )}
      </Section>

      <Section title={s.edit.sections.output}>
        <div>
          <Segmented label={s.edit.model} options={MODELS.map((m) => ({ v: m, l: s.newJob.models.names[m] }))} value={opts.video_model} onChange={(v) => setOpts((o) => ({ ...o, video_model: v }))} />
          <p className="mt-2 text-xs text-muted-foreground">{s.edit.modelHint}</p>
        </div>
        <div className="grid gap-6 sm:grid-cols-2">
          <Segmented label={s.newJob.resolution.label} options={RESOLUTIONS.map((r) => ({ v: r, l: r }))} value={opts.resolution} onChange={(v) => setOpts((o) => ({ ...o, resolution: v as EditOptions["resolution"] }))} />
          <Segmented label={s.newJob.captions.label} options={CAPTION_MODES.map((m) => ({ v: m, l: s.newJob.captions[m] }))} value={opts.captions} onChange={(v) => setOpts((o) => ({ ...o, captions: v as EditOptions["captions"] }))} />
        </div>
        <Field label={s.edit.useUntil.label} help={s.edit.useUntil.help} id="until">
          <Input id="until" type="number" inputMode="decimal" min={4} step={0.1} value={opts.use_until ?? ""} placeholder={s.edit.useUntil.placeholder} className="h-11 w-40 rounded-xl"
            onChange={(e) => setOpts((o) => ({ ...o, use_until: e.target.value ? Number(e.target.value) : null }))} />
        </Field>
        <Field label={s.edit.offer.label} help={s.edit.offer.help} id="offer">
          <Input id="offer" value={offer} onChange={(e) => setOffer(e.target.value)} placeholder={s.edit.offer.placeholder} maxLength={80} className="h-11 rounded-xl" />
        </Field>
      </Section>

      <Section title={s.edit.sections.notes}>
        <Field label={s.edit.notes.label} id="notes">
          <Textarea id="notes" rows={3} value={notes} onChange={(e) => setNotes(e.target.value)} placeholder={s.edit.notes.placeholder} maxLength={1000} className="rounded-xl" />
        </Field>
        <p className="text-xs text-muted-foreground">{s.edit.rights}</p>
      </Section>

      {est && (
        <p className="rounded-2xl bg-brand-soft px-4 py-3 text-sm" role="status">
          {est.total_usd != null ? s.edit.estimate(`${est.seconds} s`, usd(est.total_usd)) : s.edit.estimateNoPrice(`${est.seconds} s`)}
          <span className="block text-xs text-muted-foreground">{s.edit.estimateHint}</span>
        </p>
      )}
      {submitError && <p role="alert" className="rounded-2xl bg-danger-soft p-4 text-sm">{submitError}</p>}

      <div className="flex flex-col-reverse items-stretch gap-3 sm:flex-row sm:items-center sm:justify-end">
        <Button variant="ghost" className="h-12 rounded-full px-5 text-base" nativeButton={false} render={<Link href="/" />}>{s.newJob.confirm.cancel}</Button>
        <Button type="submit" size="lg" className="h-12 rounded-full bg-brand px-6 text-base text-brand-foreground shadow-soft hover:bg-brand/90" disabled={submitting}>
          {submitting ? <Loader2 className="size-4 animate-spin" /> : <Scissors className="size-4" />}
          {submitting ? s.edit.submitting : s.edit.submit}
        </Button>
      </div>
    </form>
  );
}

function PictureRow({ pics, onAdd, onRemove, addLabel, max }: { pics: Pic[]; onAdd: (files: File[]) => void; onRemove: (key: string) => void; addLabel: string; max: number }) {
  const s = t();
  return (
    <div className="flex flex-wrap gap-3">
      {pics.map((p) => (
        <figure key={p.key} className="relative w-24">
          <div className={cn("aspect-square overflow-hidden rounded-xl bg-surface ring-1", p.error ? "ring-danger" : "ring-foreground/10")}>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={p.preview} alt={p.name} className={cn("size-full object-cover", !p.ref && !p.error && "opacity-50")} />
          </div>
          {!p.ref && !p.error && <Loader2 className="absolute left-9 top-9 size-6 animate-spin text-foreground/70" aria-label={s.edit.products.uploading} />}
          <figcaption className={cn("mt-1 truncate text-[11px]", p.error ? "text-danger" : "text-muted-foreground")}>{p.error ?? p.name}</figcaption>
          <button type="button" onClick={() => onRemove(p.key)} aria-label={s.edit.products.remove}
            className="absolute -right-2 -top-2 rounded-full bg-card p-1 shadow-soft ring-1 ring-foreground/10 hover:bg-muted"><X className="size-3.5" /></button>
        </figure>
      ))}
      {pics.length < max && (
        <label className="grid aspect-square w-24 cursor-pointer place-items-center rounded-xl border border-dashed border-foreground/20 text-center text-xs font-semibold text-muted-foreground hover:bg-foreground/5">
          <span className="flex flex-col items-center gap-1 px-2"><ImagePlus className="size-5" aria-hidden />{addLabel}</span>
          <input type="file" accept="image/png,image/jpeg,image/webp" multiple className="sr-only"
            onChange={(e) => { const fs = Array.from(e.target.files ?? []).slice(0, max - pics.length); e.target.value = ""; if (fs.length) onAdd(fs); }} />
        </label>
      )}
    </div>
  );
}
