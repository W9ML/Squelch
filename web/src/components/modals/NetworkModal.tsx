"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { NetworkStatus } from "@/lib/types";
import { useApp } from "@/state/app-context";
import { SqModal } from "./SqModal";

function ago(ts: number): string {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

/** Public live node roster — which hubs are up and what's linked to each,
 *  the same connection data AllStarLink publishes. Polls on the hubs' ~30s
 *  report cadence. */
export function NetworkModal() {
  const { closeModal, setFilter } = useApp();
  const [data, setData] = useState<NetworkStatus | null>(null);
  const [msg, setMsg] = useState("checking the network…");

  useEffect(() => {
    let live = true;
    const load = () =>
      api
        .network()
        .then((d) => live && setData(d))
        .catch((e) => live && setMsg((e as Error).message));
    load();
    const iv = setInterval(load, 20000);
    return () => {
      live = false;
      clearInterval(iv);
    };
  }, []);

  return (
    <SqModal
      title="Network"
      wide
      onClose={closeModal}
      footer={
        <button className="text-btn primary" onClick={closeModal}>
          Close
        </button>
      }
    >
      {!data ? (
        <div className="dash-loading">{msg}</div>
      ) : data.hubs.length === 0 ? (
        <div className="dash-empty">
          No hub is reporting yet — node status appears once the hubs&apos; monitors check in.
        </div>
      ) : (
        <div className="netview">
          {data.hubs.map((h) => {
            const leaves = h.connected.filter((c) => !c.is_hub);
            return (
              <section className="net-hub" key={h.hub}>
                <h4>
                  <span className={"net-dot " + (h.online ? "ok" : "off")} />
                  {h.name || `Hub ${h.hub}`}
                  <span className="net-num">· {h.hub}</span>
                  <span className="net-fresh">
                    {h.online
                      ? `checked in ${ago(h.reported_at)}`
                      : `offline · last seen ${ago(h.reported_at)}`}
                  </span>
                </h4>
                {leaves.length === 0 ? (
                  <div className="dash-empty">nothing connected</div>
                ) : (
                  <div className="net-nodes">
                    {leaves.map((c) => (
                      <button
                        className="net-node"
                        key={c.node}
                        title="Filter the feed to this node"
                        onClick={() => {
                          closeModal();
                          setFilter("origin", c.node);
                        }}
                      >
                        {c.name ? `${c.name} · ${c.node}` : `node ${c.node}`}
                      </button>
                    ))}
                  </div>
                )}
              </section>
            );
          })}
        </div>
      )}
    </SqModal>
  );
}
