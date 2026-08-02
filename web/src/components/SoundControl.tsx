"use client";

import { useEffect, useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { api } from "@/lib/api";
import { notifySupported } from "@/lib/notify";
import { isCoarsePointer } from "@/lib/pointer";
import { useVolumePopover } from "@/lib/popover";
import { watchLabel } from "@/lib/watch";
import { useApp } from "@/state/app-context";
import { ICONS } from "./icons";

/** Header alert control. Clicking the bell toggles alert sounds on and off;
 *  hovering reveals a popover with the volume slider plus notification
 *  settings — desktop-notification enablement, a one-tap "mention my
 *  callsign" toggle, and the current watch list. Live audio has its own twin
 *  control (LiveControl). */
export function SoundControl() {
  const {
    soundOn,
    toggleSound,
    volume,
    setVolumePref,
    status,
    isAdmin,
    watches,
    reloadWatches,
    notifyPerm,
    enableNotifications,
    watchedCallsign,
    toggleCallsignWatch,
    installAvailable,
    promptInstall,
    openModal,
  } = useApp();
  // phones: no popover at all — tap or long-press toggles, the hardware
  // rocker controls loudness, and notification/watch settings live in the
  // Settings modal (which has its own enable-notifications button)
  const [coarse, setCoarse] = useState(false);
  useEffect(() => {
    setCoarse(isCoarsePointer());
  }, []);
  const { open, close, ref, popStyle, wrapProps, buttonProps } = useVolumePopover(
    toggleSound,
    !coarse,
  );

  const pct = Math.round(volume * 100);
  const myCall = (status?.callsign || status?.brand_callsign || "").toUpperCase();
  const supported = notifySupported();

  const removeWatch = async (id: number) => {
    try {
      await api.deleteWatch(id);
      await reloadWatches();
    } catch (e) {
      alert((e as Error).message);
    }
  };

  return (
    <div className="sound-wrap" ref={ref} {...wrapProps}>
      <button
        className={"icon-btn" + (soundOn ? " active" : "")}
        title={soundOn ? "Alert sounds on — click to mute" : "Alert sounds muted — click to unmute"}
        aria-label="Alerts"
        aria-pressed={soundOn}
        {...buttonProps}
      >
        <FontAwesomeIcon icon={soundOn ? ICONS.bell : ICONS.bellSlash} />
      </button>

      {open && !coarse && (
        <div className="sound-pop" style={popStyle} role="dialog" aria-label="Alert settings">
          <div className="sound-slider">
            <FontAwesomeIcon className="sv-ico" icon={ICONS.bellSlash} />
            <input
              type="range"
              min={0}
              max={100}
              value={pct}
              aria-label="Alert volume"
              onChange={(e) => setVolumePref(Number(e.target.value) / 100)}
            />
            <FontAwesomeIcon className="sv-ico" icon={ICONS.bell} />
          </div>
          <div className="sound-pct">{pct}%</div>

          {supported && (
            <>
              <div className="alert-sep" />
              {notifyPerm === "granted" ? (
                <div className="alert-note ok">Notifications on (this device &amp; phone)</div>
              ) : notifyPerm === "denied" ? (
                <div className="alert-note">Notifications blocked in browser settings</div>
              ) : (
                <button className="text-btn" onClick={enableNotifications}>
                  Enable notifications
                </button>
              )}
            </>
          )}

          {installAvailable && (
            <button className="text-btn" onClick={promptInstall}>
              Install Squelch app
            </button>
          )}

          {isAdmin && myCall && (
            <label className="sound-mute" title={`Notify me whenever “${myCall}” is spoken on the air`}>
              <input
                type="checkbox"
                checked={!!watchedCallsign(myCall)}
                onChange={() => toggleCallsignWatch(myCall)}
              />
              Alert when “{myCall}” is mentioned
            </label>
          )}

          {isAdmin && (
            <div className="alert-watches">
              {watches.length === 0 ? (
                <div className="alert-note">
                  No alerts yet — tap the <FontAwesomeIcon icon={ICONS.star} /> on an operator.
                </div>
              ) : (
                watches.map((w) => (
                  <div className="alert-watch-row" key={w.id}>
                    <span className="awr-label">{watchLabel(w)}</span>
                    <button className="alert-x" title="Remove alert" onClick={() => removeWatch(w.id)}>
                      ×
                    </button>
                  </div>
                ))
              )}
              <button
                className="text-btn small"
                onClick={() => {
                  close();
                  openModal({ kind: "settings" });
                }}
              >
                Manage alerts…
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
