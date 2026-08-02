"use client";

import { useEffect, useRef, useState, type RefObject } from "react";

/** Interaction logic for the header volume popovers (bell / headphones).
 *
 *  Mouse: hovering the button opens the popover, leaving closes it (with a
 *  short grace period so the pointer can travel into it); clicking toggles.
 *
 *  Touch: there is no hover, and a tap fires enter→down→up→leave in one burst
 *  (which used to flash the popover open and shut). So for coarse pointers a
 *  plain tap only toggles, and a long-press (~450 ms) opens the popover
 *  instead — suppressing the toggle that the same gesture's click would fire.
 */
export function useVolumePopover(toggle: () => void, touchHold = true): {
  open: boolean;
  close: () => void;
  ref: RefObject<HTMLDivElement>;
  popStyle: React.CSSProperties | undefined;
  wrapProps: {
    onPointerEnter: (e: React.PointerEvent) => void;
    onPointerLeave: (e: React.PointerEvent) => void;
    onContextMenu: (e: React.MouseEvent) => void;
  };
  buttonProps: {
    onClick: () => void;
    onPointerDown: (e: React.PointerEvent) => void;
    onPointerUp: () => void;
    onPointerCancel: () => void;
  };
} {
  const [open, setOpen] = useState(false);
  const [popStyle, setPopStyle] = useState<React.CSSProperties | undefined>();
  const ref = useRef<HTMLDivElement>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const holdTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const held = useRef(false);

  const openPop = () => {
    // narrow screens: the header wraps and a right-anchored popover can hang
    // off-screen — pin it full-width just below the button row instead
    if (window.innerWidth <= 600 && ref.current) {
      const r = ref.current.getBoundingClientRect();
      setPopStyle({ position: "fixed", left: 12, right: 12, top: Math.round(r.bottom + 8) });
    } else {
      setPopStyle(undefined);
    }
    setOpen(true);
  };

  const onPointerEnter = (e: React.PointerEvent) => {
    if (e.pointerType !== "mouse") return;
    if (closeTimer.current) clearTimeout(closeTimer.current);
    openPop();
  };
  const onPointerLeave = (e: React.PointerEvent) => {
    if (e.pointerType !== "mouse") return;
    if (closeTimer.current) clearTimeout(closeTimer.current);
    closeTimer.current = setTimeout(() => setOpen(false), 150);
  };

  const onPointerDown = (e: React.PointerEvent) => {
    if (e.pointerType === "mouse") return;
    held.current = false;
    if (holdTimer.current) clearTimeout(holdTimer.current);
    holdTimer.current = setTimeout(() => {
      held.current = true;
      // touchHold=false means this control has no popover on touch — treat a
      // long-press as a slow tap and toggle, instead of doing nothing
      if (touchHold) openPop();
      else toggle();
    }, 450);
  };
  const cancelHold = () => {
    if (holdTimer.current) clearTimeout(holdTimer.current);
  };
  const onClick = () => {
    // a long-press already opened the popover; swallow that gesture's click
    if (held.current) {
      held.current = false;
      return;
    }
    toggle();
  };
  const onContextMenu = (e: React.MouseEvent) => {
    // Android fires a context menu on long-press — ours wins
    if (held.current || open) e.preventDefault();
  };

  // close on a tap/click anywhere outside (the touch escape hatch)
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onDoc);
    return () => document.removeEventListener("pointerdown", onDoc);
  }, [open]);

  return {
    open,
    close: () => setOpen(false),
    ref,
    popStyle,
    wrapProps: { onPointerEnter, onPointerLeave, onContextMenu },
    buttonProps: { onClick, onPointerDown, onPointerUp: cancelHold, onPointerCancel: cancelHold },
  };
}
