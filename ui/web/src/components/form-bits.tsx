"use client";

import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

export function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-3xl bg-card p-5 shadow-soft ring-1 ring-foreground/8 sm:p-7">
      <h2 className="mb-5 font-display text-xl font-medium">{title}</h2>
      <div className="space-y-5">{children}</div>
    </section>
  );
}

export function Field({ label, help, error, id, children }: { label: string; help?: string; error?: string; id: string; children: React.ReactNode }) {
  return (
    <div>
      <Label htmlFor={id} className="mb-1.5 text-sm font-semibold">{label}</Label>
      {children}
      {help && !error && <p id={`${id}-help`} className="mt-1.5 text-xs leading-relaxed text-muted-foreground">{help}</p>}
      {error && <p className="mt-1.5 text-sm text-danger" role="alert">{error}</p>}
    </div>
  );
}

export function Segmented({ label, options, value, onChange }: { label: string; options: { v: string; l: string }[]; value: string; onChange: (v: string) => void }) {
  return (
    <div>
      <span className="mb-1.5 block text-sm font-semibold">{label}</span>
      <div role="radiogroup" aria-label={label} className="inline-flex rounded-xl bg-muted p-1">
        {options.map((o) => (
          <button
            key={o.v}
            type="button"
            role="radio"
            aria-checked={value === o.v}
            onClick={() => onChange(o.v)}
            className={cn("h-9 rounded-lg px-4 text-sm font-semibold outline-none transition-colors focus-visible:ring-3 focus-visible:ring-ring/50", value === o.v ? "bg-card text-foreground shadow-soft" : "text-muted-foreground hover:text-foreground")}
          >
            {o.l}
          </button>
        ))}
      </div>
    </div>
  );
}
