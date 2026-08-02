"use client";

import { useEffect, useRef } from "react";
import { useApp } from "@/state/app-context";
import { subscribeBscope } from "@/lib/bandscope";

// internal buffer; CSS stretches it to the strip. 128 freq bins across 0-4 kHz,
// one px-row of time history per column (~30/sec from the backend FFT).
const W = 512;
const H = 128;
const BINS = 128;

function parseColor(s: string): [number, number, number] {
  s = s.trim();
  if (s[0] === "#") {
    if (s.length === 4)
      return [parseInt(s[1] + s[1], 16), parseInt(s[2] + s[2], 16), parseInt(s[3] + s[3], 16)];
    return [parseInt(s.slice(1, 3), 16), parseInt(s.slice(3, 5), 16), parseInt(s.slice(5, 7), 16)];
  }
  const m = s.match(/(\d+)[,\s]+(\d+)[,\s]+(\d+)/);
  if (m) return [+m[1], +m[2], +m[3]];
  return [80, 255, 130];
}

/** 256-entry intensity → RGB ramp: black → tube → white, like a phosphor. */
function buildLut(tube: [number, number, number]): Uint8ClampedArray {
  const lut = new Uint8ClampedArray(256 * 3);
  const [tr, tg, tb] = tube;
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    let r: number, g: number, b: number;
    if (t < 0.72) {
      const k = (t / 0.72) * 0.92;
      r = tr * k; g = tg * k; b = tb * k;
    } else {
      const k = (t - 0.72) / 0.28;
      r = tr + (255 - tr) * k; g = tg + (255 - tg) * k; b = tb + (255 - tb) * k;
    }
    lut[i * 3] = r; lut[i * 3 + 1] = g; lut[i * 3 + 2] = b;
  }
  return lut;
}

export function Bandscope() {
  const { theme } = useApp();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const peakRef = useRef<HTMLSpanElement>(null);
  const lutRef = useRef<Uint8ClampedArray>(buildLut([80, 255, 130]));

  // recolor the ramp when the theme's tube changes; clear so history recolors.
  // Paper = white P4 phosphor (red is reserved for actions), so use --text there.
  useEffect(() => {
    const varName = theme === "paper" ? "--text" : "--accent";
    const raw = getComputedStyle(document.documentElement).getPropertyValue(varName);
    lutRef.current = buildLut(parseColor(raw || "#3dff7c"));
    const cv = canvasRef.current;
    if (cv) {
      const ctx = cv.getContext("2d");
      if (ctx) { ctx.fillStyle = "#000"; ctx.fillRect(0, 0, W, H); }
    }
  }, [theme]);

  useEffect(() => {
    const cv = canvasRef.current;
    if (!cv) return;
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    ctx.imageSmoothingEnabled = false;
    ctx.fillStyle = "#000"; ctx.fillRect(0, 0, W, H);
    const row = ctx.createImageData(W, 1);
    const rd = row.data;
    let lastReal = 0;

    const drawCol = (col: Uint8Array) => {
      const lut = lutRef.current;
      for (let x = 0; x < W; x++) {
        const v = col[x >> 2] | 0;             // 4 px per bin (512 / 128)
        const o = x * 4, li = v * 3;
        rd[o] = lut[li]; rd[o + 1] = lut[li + 1]; rd[o + 2] = lut[li + 2]; rd[o + 3] = 255;
      }
      ctx.drawImage(cv, 0, 1);                  // scroll everything down a row
      ctx.putImageData(row, 0, 0);              // newest row at the top
    };

    const unsub = subscribeBscope((col) => {
      lastReal = performance.now();
      drawCol(col);
      // dominant-frequency readout (skip bin 0/1 DC); blank when it's just noise
      let mx = 0, mi = 0;
      for (let i = 2; i < BINS; i++) if (col[i] > mx) { mx = col[i]; mi = i; }
      if (peakRef.current)
        peakRef.current.textContent = mx > 105 ? `≈ ${Math.round((mi / BINS) * 4000)} Hz` : "";
    });

    // idle scroll: keep the noise floor flowing between overs so it reads live
    let raf = 0, acc = 0, last = 0;
    const idleCol = new Uint8Array(BINS);
    const tick = (ts: number) => {
      raf = requestAnimationFrame(tick);
      if (!last) last = ts;
      acc += ts - last; last = ts;
      while (acc > 34) {
        acc -= 34;
        if (performance.now() - lastReal > 80) {
          for (let i = 0; i < BINS; i++) idleCol[i] = 5 + ((Math.random() * 11) | 0);
          drawCol(idleCol);
          if (peakRef.current) peakRef.current.textContent = "";
        }
      }
    };
    raf = requestAnimationFrame(tick);

    return () => { unsub(); cancelAnimationFrame(raf); };
  }, []);

  return (
    <div className="bandscope" aria-hidden="true">
      <div className="bs-head">
        <span className="bs-title"><i className="bs-dot" />BANDSCOPE</span>
        <span className="bs-span">0–4 kHz</span>
        <span className="bs-peak" ref={peakRef} />
      </div>
      <div className="bs-tube"><canvas ref={canvasRef} width={W} height={H} /></div>
      <div className="bs-axis"><span>0</span><span>1k</span><span>2k</span><span>3k</span><span>4&nbsp;kHz</span></div>
    </div>
  );
}
