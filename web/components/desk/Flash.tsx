"use client";
import { m } from "motion/react";
import { useRef, type ReactNode } from "react";
import { cn } from "@/lib/utils";

/**
 * Brief highlight when `value` changes (PnL ticks, equity, KPIs). Skips the first value so the initial
 * snapshot load doesn't light up the whole page. Opacity-only overlay, so it stays on the compositor.
 */
export function Flash({ value, dir, children, className }: { value: unknown; dir?: number | null; children: ReactNode; className?: string }) {
  const seen = useRef<{ v: unknown; n: number } | null>(null);
  if (seen.current == null) seen.current = { v: value, n: 0 };
  else if (seen.current.v !== value) {
    // no flash for the first real value (null/undefined -> data), only for genuine ticks
    const tick = seen.current.v != null && value != null;
    seen.current = { v: value, n: seen.current.n + (tick ? 1 : 0) };
  }
  const n = seen.current.n;
  return (
    <span className={cn("relative isolate inline-block", className)}>
      {n > 0 && (
        <m.span
          key={n}
          aria-hidden
          className={cn("pointer-events-none absolute -inset-x-1 inset-y-0 -z-10 rounded-sm",
            dir == null || dir === 0 ? "bg-primary/25" : dir > 0 ? "bg-profit/30" : "bg-loss/30")}
          initial={{ opacity: 1 }}
          animate={{ opacity: 0 }}
          transition={{ duration: 1.2, ease: "easeOut" }}
        />
      )}
      {children}
    </span>
  );
}
