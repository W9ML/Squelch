"use client";

import { useEffect, useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { getLiveVolume, setLiveVolume, useLiveAudio } from "@/lib/liveAudio";
import { isCoarsePointer } from "@/lib/pointer";
import { useVolumePopover } from "@/lib/popover";
import { ICONS } from "./icons";

/** Header live-audio control. Clicking the headphones toggles listening on and
 *  off (the click doubles as the AudioContext user gesture); hovering reveals
 *  a popover with the volume slider. Live audio is off by default. */
export function LiveControl() {
  const { listening, streaming, toggleListen } = useLiveAudio();
  const [vol, setVol] = useState(1);
  // phones: no volume popover at all — tap toggles, the hardware rocker is
  // the volume control (state so SSR and first client render agree)
  const [coarse, setCoarse] = useState(false);
  useEffect(() => {
    setVol(getLiveVolume()); // read the saved pref client-side only
    setCoarse(isCoarsePointer());
  }, []);
  const { open, ref, popStyle, wrapProps, buttonProps } = useVolumePopover(toggleListen, !coarse);

  const pct = Math.round(vol * 100);

  return (
    <div className="sound-wrap" ref={ref} {...wrapProps}>
      <button
        className={
          "icon-btn live-btn" + (listening ? (streaming ? " live-streaming" : " live-waiting") : "")
        }
        title={listening ? "Live audio on — click to stop" : "Click to listen live"}
        aria-label="Live audio"
        aria-pressed={listening}
        {...buttonProps}
      >
        <FontAwesomeIcon icon={ICONS.headphones} />
        {streaming && <span className="live-badge">LIVE</span>}
      </button>

      {open && !coarse && (
        <div className="sound-pop" style={popStyle} role="dialog" aria-label="Live audio volume">
          <div className="sound-slider">
            <FontAwesomeIcon className="sv-ico" icon={ICONS.volumeLow} />
            <input
              type="range"
              min={0}
              max={100}
              value={pct}
              aria-label="Live audio volume"
              onChange={(e) => {
                const v = Number(e.target.value) / 100;
                setVol(v);
                setLiveVolume(v); // applies immediately while streaming
              }}
            />
            <FontAwesomeIcon className="sv-ico" icon={ICONS.volumeHigh} />
          </div>
          <div className="sound-pct">{pct}%</div>
          <div className={"alert-note" + (listening ? " ok" : "")}>
            {listening
              ? streaming
                ? "Receiving — audio is playing"
                : "Listening for transmissions"
              : "Live audio off — click the headphones to listen"}
          </div>
        </div>
      )}
    </div>
  );
}
