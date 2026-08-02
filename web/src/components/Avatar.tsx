"use client";

import { useState } from "react";
import { avatarColors, initials } from "@/lib/format";
import { useApp } from "@/state/app-context";

const CALL_RE = /^[A-Z]{1,2}[0-9][A-Z]{1,3}$/i;

/** The callsign inside a speaker label ("W9ML Michael" -> "W9ML"). */
export function callsignOf(label: string): string | null {
  const hit = label.split(/\s+/).find((w) => CALL_RE.test(w));
  return hit ? hit.toUpperCase() : null;
}

/** Speaker avatar: the operator's QRZ photo when the enrichment cache has
 *  one, otherwise the classic hue-colored initials circle. Falls back to
 *  initials automatically if the photo fails to load. */
export function Avatar({
  label,
  className = "",
  title,
  onClick,
}: {
  label: string;
  className?: string;
  title?: string;
  onClick?: () => void;
}) {
  const { avatars } = useApp();
  const [broken, setBroken] = useState(false);
  const cs = callsignOf(label);
  const photo = cs ? avatars[cs] : undefined;

  if (photo && !broken) {
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        className={"avatar avatar-img " + className}
        src={photo}
        alt={label}
        title={title}
        onClick={onClick}
        onError={() => setBroken(true)}
      />
    );
  }
  const av = avatarColors(label);
  return (
    <div
      className={"avatar " + className}
      style={{ background: av.bg, color: av.color }}
      title={title}
      onClick={onClick}
    >
      {initials(label)}
    </div>
  );
}
