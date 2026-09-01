"use client";

import "leaflet/dist/leaflet.css";
import { useEffect, useRef, useState } from "react";
import type * as Leaflet from "leaflet";
import { api } from "@/lib/api";
import { ApiError } from "@/lib/api";

/**
 * Fuzzy free-space-loss "location" map, admin-only. Leaflet is imported
 * dynamically inside the effect so the static export's prerender never touches
 * `window`. Ported from the original buildGeoMap.
 */
export function GeoMap({ txId, bus }: { txId: number; bus: EventTarget }) {
  const elRef = useRef<HTMLDivElement>(null);
  const [note, setNote] = useState("loading map…");
  const [summary, setSummary] = useState("");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let map: Leaflet.Map | null = null;
    let cancelled = false;
    let cleanup: (() => void) | null = null;

    (async () => {
      let data;
      try {
        data = await api.geo(txId);
      } catch (e) {
        setFailed(true);
        setNote(e instanceof ApiError ? e.message : "geolocation unavailable");
        return;
      }
      let L: typeof Leaflet;
      try {
        L = (await import("leaflet")).default as unknown as typeof Leaflet;
      } catch {
        setFailed(true);
        setNote("map library failed to load");
        return;
      }
      if (cancelled || !elRef.current) return;
      setNote("");

      map = L.map(elRef.current, { attributionControl: false, zoomControl: true });
      // Esri "Dark Gray Canvas" — keyless dark basemap (CARTO's free CDN now
      // watermarks anonymous use). Base = land, Reference = labels overlay.
      // Native tiles top out at z16; upscale past that instead of blanking.
      const esri = (layer: string) =>
        `https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/${layer}/MapServer/tile/{z}/{y}/{x}`;
      L.tileLayer(esri("World_Dark_Gray_Base"), { maxZoom: 19, maxNativeZoom: 16 }).addTo(map);
      L.tileLayer(esri("World_Dark_Gray_Reference"), { maxZoom: 19, maxNativeZoom: 16 }).addTo(map);

      const pts = data.receivers.map((r) => [r.lat, r.lon] as [number, number]);
      const rxMarkers: Record<string, Leaflet.Circle> = {};
      for (const r of data.receivers) {
        const dkm = data.distances?.[r.name];
        const label = dkm != null ? `${r.name} · ~${dkm} km` : r.name;
        L.circleMarker([r.lat, r.lon], {
          radius: 5,
          color: "#38bdf8",
          weight: 2,
          fillOpacity: 0.4,
        })
          .bindTooltip(label, { permanent: false })
          .addTo(map);
        rxMarkers[r.name] = L.circle([r.lat, r.lon], {
          radius: 200,
          color: "#34d399",
          weight: 1,
          fillOpacity: 0.06,
          opacity: 0,
        }).addTo(map);
      }
      // honest credible region (~68% area) around the estimate, coloured by
      // confidence — never a precise-looking pin
      const CONF = { low: "#f59e0b", medium: "#38bdf8", high: "#34d399" } as const;
      const conf = (data.confidence ?? "low") as keyof typeof CONF;
      const ring = CONF[conf];
      const credM = (data.credible_km ?? 0) * 1000;
      let credCircle: Leaflet.Circle | null = null;
      if (credM > 0) {
        credCircle = L.circle([data.best_est[0], data.best_est[1]], {
          radius: credM,
          color: ring,
          weight: 1.5,
          fillColor: ring,
          fillOpacity: 0.12,
        }).addTo(map);
      }
      const est = L.circleMarker([data.best_est[0], data.best_est[1]], {
        radius: 7,
        color: ring,
        weight: 3,
        fillColor: ring,
        fillOpacity: 0.6,
      })
        .bindTooltip(
          `most likely area · ${conf} confidence` +
            (data.credible_km ? ` · ~${data.credible_km} km` : ""),
          { permanent: false },
        )
        .addTo(map);
      setSummary(
        (data.credible_km ? `~${data.credible_km} km credible radius` : "coarse estimate") +
          ` · ${conf} confidence`,
      );
      // extend the view to include the credible ring using the radius in
      // DEGREES, not credCircle.getBounds(): getBounds() projects through the
      // map, which has no view yet here, and throws — blanking the whole map.
      const km = data.credible_km ?? 0;
      let bounds = L.latLngBounds([...pts, data.best_est]);
      if (km > 0) {
        const dLat = km / 111;
        const dLon = km / (111 * Math.cos((data.best_est[0] * Math.PI) / 180));
        bounds = bounds
          .extend([data.best_est[0] + dLat, data.best_est[1] + dLon])
          .extend([data.best_est[0] - dLat, data.best_est[1] - dLon]);
      }
      if (pts.length) {
        map.fitBounds(bounds.pad(0.1));
      } else {
        map.setView(data.best_est, 11);
      }
      setTimeout(() => map?.invalidateSize(), 100);

      const renderAt = (t: number) => {
        let s = data.track[0];
        for (const cand of data.track) {
          if (cand.t <= t) s = cand;
          else break;
        }
        if (s.est) est.setLatLng(s.est);
        for (const r of data.receivers) {
          const v = s.rssi[r.name] || 0;
          const halo = rxMarkers[r.name];
          halo.setStyle({ opacity: v > 0 ? 0.8 : 0 });
          halo.setRadius(150 + (v / 255) * 1800);
        }
      };
      const onTime = (e: Event) => renderAt((e as CustomEvent<number>).detail);
      bus.addEventListener("ptime", onTime);
      renderAt(data.track[0]?.t ?? 0);

      // store cleanup on the closure
      cleanup = () => bus.removeEventListener("ptime", onTime);
    })();

    return () => {
      cancelled = true;
      cleanup?.();
      map?.remove();
    };
  }, [txId, bus]);

  // the map div is always mounted (stable node for leaflet); only hidden on a
  // hard failure. The note line doubles as the loading/error message.
  return (
    <>
      <div ref={elRef} className="geo-map" style={failed ? { display: "none" } : undefined} />
      <div className="geo-note">
        {note || (
          <>
            {summary ? <strong>{summary}</strong> : null}
            {summary ? " — " : null}
            Censored-grid estimate from voter RSSI; saturation and terrain make it
            coarse (kilometres), not direction finding. Shaded ring ≈ 68% credible
            area.
          </>
        )}
      </div>
    </>
  );
}
