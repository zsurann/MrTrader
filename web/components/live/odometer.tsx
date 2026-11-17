"use client";
import { m } from "motion/react";

const DIGITS = ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"];
const spring = { type: "spring", stiffness: 120, damping: 18, mass: 0.9 } as const;

/** One slot-machine column: a 0-9 strip translated to the current digit (transform only -> GPU, no layout shift).
 *  An invisible "0" sizes the slot, so tabular digits keep a fixed width. */
function Digit({ d }: { d: number }) {
  return (
    <span className="relative inline-block overflow-hidden align-baseline" style={{ height: "1.08em", lineHeight: "1.08em" }}>
      <span className="invisible">0</span>
      <m.span className="absolute inset-x-0 top-0 flex flex-col items-center" initial={false} animate={{ y: `${-d * 1.08}em` }} transition={spring}>
        {DIGITS.map((x) => <span key={x} style={{ height: "1.08em" }}>{x}</span>)}
      </m.span>
    </span>
  );
}

/** Odometer for an already formatted string: every digit rolls, separators/symbols stay static. Keys are counted from
 *  the right so "$9.999" -> "$10.001" keeps units aligned. Reduced motion is handled by <MotionConfig reducedMotion="user">. */
export function Odometer({ text, className, label }: { text: string; className?: string; label?: string }) {
  const chars = text.split("");
  return (
    <span className={className} aria-label={label ?? text} role="text">
      <span aria-hidden className="inline-flex items-baseline">
        {chars.map((c, i) => {
          const k = chars.length - i;
          return /\d/.test(c) ? <Digit key={`d${k}`} d={+c} /> : <span key={`s${k}`}>{c}</span>;
        })}
      </span>
    </span>
  );
}
