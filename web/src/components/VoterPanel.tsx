"use client";

import { useEffect, useRef, useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import type { Transmission } from "@/lib/types";
import { useApp } from "@/state/app-context";
import { ICONS } from "./icons";
import { GeoMap } from "./GeoMap";

/** Collapsible per-receiver RSSI bars that replay during playback (voter
 *  status), plus the admin-only approximate-location map. Ported from the
 *  original buildVoterPanel. */
export function VoterPanel({ tx, bus }: { tx: Transmission; bus: EventTarget }) {
  const { status, isAdmin } = useApp();
  const [open, setOpen] = useState(false);
  const [geoOpen, setGeoOpen] = useState(false);
  const nodes = tx.voter?.nodes ?? [];
  const fillRefs = useRef<HTMLDivElement[][]>([]);
  const valRefs = useRef<HTMLSpanElement[][]>([]);
  const openRef = useRef(open);
  openRef.current = open;

  useEffect(() => {
    const renderAt = (t: number) => {
      nodes.forEach((nd, ni) => {
        let s = nd.samples[0];
        for (const cand of nd.samples) {
          if (cand[0] <= t) s = cand;
          else break;
        }
        if (!s) return;
        const [, rssi, mask] = s;
        nd.clients.forEach((_, ci) => {
          const v = rssi[ci] ?? 0;
          const fill = fillRefs.current[ni]?.[ci];
          const val = valRefs.current[ni]?.[ci];
          if (fill) {
            fill.style.width = `${Math.max(1, (v / 255) * 100)}%`;
            fill.classList.toggle("voted", !!((mask >> ci) & 1));
          }
          if (val) val.textContent = String(v);
        });
      });
    };
    const renderEmpty = () => {
      nodes.forEach((nd, ni) =>
        nd.clients.forEach((_, ci) => {
          const fill = fillRefs.current[ni]?.[ci];
          const val = valRefs.current[ni]?.[ci];
          if (fill) {
            fill.style.width = "1%";
            fill.classList.remove("voted");
          }
          if (val) val.textContent = "0";
        }),
      );
    };
    const onTime = (e: Event) => {
      if (openRef.current) renderAt((e as CustomEvent<number>).detail);
    };
    const onEnded = () => {
      if (openRef.current) renderEmpty();
    };
    bus.addEventListener("ptime", onTime);
    bus.addEventListener("pended", onEnded);
    return () => {
      bus.removeEventListener("ptime", onTime);
      bus.removeEventListener("pended", onEnded);
    };
  }, [bus, nodes]);

  // reset the bars to empty whenever the panel is opened
  useEffect(() => {
    if (!open) return;
    nodes.forEach((nd, ni) =>
      nd.clients.forEach((_, ci) => {
        const fill = fillRefs.current[ni]?.[ci];
        const val = valRefs.current[ni]?.[ci];
        if (fill) {
          fill.style.width = "1%";
          fill.classList.remove("voted");
        }
        if (val) val.textContent = "0";
      }),
    );
  }, [open, nodes]);

  return (
    <>
      <button className="voter-toggle" onClick={() => setOpen((o) => !o)}>
        <span className="chev">{open ? "▾" : "▸"}</span> Voter status
      </button>
      {open && (
        <div className="voter-panel">
          {nodes.map((nd, ni) => (
            <div className="voter-node" key={ni}>
              <div className="voter-node-head">{nd.info || `Node ${nd.node}`}</div>
              {nd.clients.map((name, ci) => (
                <div className="voter-row" key={ci}>
                  <span className="voter-name">{name}</span>
                  <div className="voter-bar">
                    <div
                      className="voter-fill"
                      ref={(el) => {
                        if (el) (fillRefs.current[ni] ??= [])[ci] = el;
                      }}
                    />
                  </div>
                  <span
                    className="voter-rssi"
                    ref={(el) => {
                      if (el) (valRefs.current[ni] ??= [])[ci] = el;
                    }}
                  >
                    0
                  </span>
                </div>
              ))}
            </div>
          ))}
          <div className="voter-note">
            receiver signal strength (0–255), recorded during this transmission — press
            play to replay it; green = voted site
          </div>
        </div>
      )}

      {status?.has_geo && isAdmin && (
        <>
          <button className="voter-toggle geo-toggle" onClick={() => setGeoOpen((o) => !o)}>
            <FontAwesomeIcon icon={ICONS.map} /> Approx. location
          </button>
          {geoOpen && (
            <div className="geo-panel">
              <GeoMap txId={tx.id} bus={bus} />
            </div>
          )}
        </>
      )}
    </>
  );
}
