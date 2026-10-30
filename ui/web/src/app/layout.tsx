import type { Metadata, Viewport } from "next";
import { Suspense } from "react";
import "./globals.css";
import { ThemeProvider, themeInitScript } from "@/lib/theme";
import { AppShell } from "@/components/app-shell";
import { t } from "@/i18n";

export const metadata: Metadata = {
  title: { default: t().app.name, template: `%s · ${t().app.name}` },
  description: t().app.tagline,
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#faf7f2" },
    { media: "(prefers-color-scheme: dark)", color: "#15120f" },
  ],
};

// Optional accent from the build profile: "primary,text on primary,soft tint" for light and for dark mode.
function accentRule(selector: string, value?: string): string {
  const [main, fg, soft] = (value || "").split(",").map((v) => v.trim());
  if (!main || !/^#[0-9a-fA-F]{3,8}$/.test(main)) return "";
  const safe = (v?: string) => (v && /^#[0-9a-fA-F]{3,8}$/.test(v) ? v : undefined);
  const f = safe(fg) || "#ffffff";
  const s = safe(soft);
  return `${selector}{--primary:${main};--primary-foreground:${f};--brand:${main};--brand-foreground:${f};--ring:${main};${s ? `--brand-soft:${s};` : ""}}`;
}
const accentCss =
  accentRule("html:root", process.env.NEXT_PUBLIC_ACCENT_LIGHT) + accentRule("html:root.dark", process.env.NEXT_PUBLIC_ACCENT_DARK);

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning className="h-full">
      <head>
        {accentCss ? <style dangerouslySetInnerHTML={{ __html: accentCss }} /> : null}
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        {/* Fonts via <link> on purpose: next/font/google downloads at build time, which this offline-friendly build avoids. */}
        {/* eslint-disable-next-line @next/next/no-page-custom-font */}
        <link
          href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght,SOFT@9..144,400..700,0..100&family=Manrope:wght@400;500;600;700&display=swap"
          rel="stylesheet"
        />
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
      </head>
      <body className="min-h-full flex flex-col">
        <ThemeProvider>
          <Suspense fallback={null}>
            <AppShell>{children}</AppShell>
          </Suspense>
        </ThemeProvider>
      </body>
    </html>
  );
}
