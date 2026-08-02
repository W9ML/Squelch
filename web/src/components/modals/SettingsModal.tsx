"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { THEME_LABELS } from "@/lib/format";
import { watchLabel } from "@/lib/watch";
import type { User, Watch } from "@/lib/types";
import { useApp } from "@/state/app-context";
import { SqModal } from "./SqModal";

const SWATCH: Record<string, string[]> = {
  night: ["#171923", "#4fd1c5", "#edf2f7"],
  day: ["#f7fafc", "#319795", "#1a202c"],
  crt: ["#050a06", "#3dff7c", "#8df5a8"],
  amber: ["#0f0a02", "#ffb000", "#ffc352"],
  cyan: ["#04090a", "#33ffee", "#8ff5ec"],
  borland: ["#0000aa", "#ffff55", "#f4f6ff"],
  c64: ["#3e31a2", "#8d89e4", "#b4adee"],
  paper: ["#0d0d0f", "#c21807", "#ececef"],
};
function swatchDots(theme: string) {
  return SWATCH[theme] || SWATCH.night;
}

function WatchlistSection() {
  const [watches, setWatches] = useState<Watch[]>([]);
  const [kind, setKind] = useState("callsign");
  const [value, setValue] = useState("");
  const [webhook, setWebhook] = useState("");
  const [err, setErr] = useState("");
  const [perm, setPerm] = useState(
    typeof window !== "undefined" && "Notification" in window ? Notification.permission : "granted",
  );

  const load = () => api.watchlist().then(({ watchlist }) => setWatches(watchlist)).catch(() => {});
  useEffect(() => {
    load();
  }, []);

  return (
    <>
      <h4>Watchlist alerts</h4>
      {perm === "default" && (
        <button
          className="text-btn"
          onClick={() => Notification.requestPermission().then((p) => setPerm(p))}
        >
          Enable browser notifications
        </button>
      )}
      <div>
        {watches.length === 0 ? (
          <p className="muted">No alerts yet.</p>
        ) : (
          watches.map((w) => (
            <div className="user-row" key={w.id}>
              <span className="watch-left">
                <input
                  type="checkbox"
                  className="watch-en"
                  checked={!!w.enabled}
                  title={w.enabled ? "Alert on — click to disable" : "Alert off — click to enable"}
                  onChange={async () => {
                    try {
                      await api.setWatchEnabled(w.id, !w.enabled);
                      load();
                    } catch (e) {
                      alert((e as Error).message);
                    }
                  }}
                />
                <span className={"uname" + (w.enabled ? "" : " watch-off")}>
                  {watchLabel(w)}
                  {w.webhook && <span className="badge-you">webhook</span>}
                </span>
              </span>
              <button
                className="link-danger"
                onClick={async () => {
                  try {
                    await api.deleteWatch(w.id);
                    load();
                  } catch (e) {
                    alert((e as Error).message);
                  }
                }}
              >
                remove
              </button>
            </div>
          ))
        )}
      </div>
      <select value={kind} onChange={(e) => setKind(e.target.value)}>
        <option value="callsign">Callsign heard</option>
        <option value="mdc_unit">MDC unit ID</option>
        <option value="emergency">MDC Emergency</option>
        <option value="speaker">Speaker ID</option>
      </select>
      {kind !== "emergency" && (
        <input
          type="text"
          placeholder="value (e.g. KD9NSC)"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
      )}
      <input
        type="text"
        placeholder="webhook URL (optional, e.g. ntfy)"
        value={webhook}
        onChange={(e) => setWebhook(e.target.value)}
      />
      <button
        className="text-btn"
        onClick={async () => {
          try {
            await api.addWatch(kind, value.trim(), webhook.trim());
            setValue("");
            setWebhook("");
            setErr("");
            load();
          } catch (e) {
            setErr((e as Error).message);
          }
        }}
      >
        Add alert
      </button>
      <div className="err">{err}</div>
    </>
  );
}

function VoiceSection() {
  const [confirm, setConfirm] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const reset = async () => {
    setConfirm(false);
    try {
      const r = await api.resetAllVoiceprints();
      setMsg(
        `Done — ${r.samples_deleted} samples wiped, ` +
          `${r.attributions_cleared} attributions cleared, ` +
          `${r.clusters_dropped} auto-clusters removed, ` +
          `${r.reseeded} verified samples re-seeded.`,
      );
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  return (
    <>
      <h4>Voice identification</h4>
      <div className="user-row">
        <span className="uname">
          Reset all voiceprints
          <span className="muted-note">
            {" "}
            — wipes every learned voice so identification starts fresh.
            Named speakers keep their callsign/MDC-verified history, and
            their voiceprints re-seed from it.
          </span>
        </span>
        {confirm ? (
          <span className="inline-confirm">
            wipe every learned voice?
            <button className="link-danger" onClick={reset}>
              yes
            </button>
            <button className="link-muted" onClick={() => setConfirm(false)}>
              no
            </button>
          </span>
        ) : (
          <button className="link-danger" onClick={() => setConfirm(true)}>
            reset
          </button>
        )}
      </div>
      {msg && <div className="ok-msg">{msg}</div>}
      {err && <div className="err">{err}</div>}
    </>
  );
}

function UsersSection({ me }: { me: string | null }) {
  const [users, setUsers] = useState<User[]>([]);
  const [name, setName] = useState("");
  const [pw, setPw] = useState("");
  const [role, setRole] = useState("admin");
  const [err, setErr] = useState("");
  const [confirmDel, setConfirmDel] = useState<string | null>(null);

  const load = () => api.users().then(({ users }) => setUsers(users)).catch(() => {});
  useEffect(() => {
    load();
  }, []);

  return (
    <>
      <h4>Users</h4>
      <div>
        {users.map((u) => (
          <div className="user-row" key={u.username}>
            <span className="uname">
              {u.username}
              <span className={"role-badge role-" + (u.role || "admin")}>{u.role || "admin"}</span>
              {u.username === me && <span className="badge-you">you</span>}
            </span>
            {u.username !== me && (
              <span className="user-actions">
                <select
                  className="role-select"
                  value={u.role || "admin"}
                  onChange={async (e) => {
                    try {
                      await api.setUserRole(u.username, e.target.value);
                    } catch (err2) {
                      alert((err2 as Error).message);
                    }
                    load();
                  }}
                >
                  <option value="super">Super Admin</option>
                  <option value="admin">Admin</option>
                  <option value="user">User</option>
                </select>
                {confirmDel === u.username ? (
                  <span className="inline-confirm">
                    Delete?
                    <button
                      className="link-danger"
                      onClick={async () => {
                        setConfirmDel(null);
                        try {
                          await api.deleteUser(u.username);
                          load();
                        } catch (e) {
                          alert((e as Error).message);
                        }
                      }}
                    >
                      yes
                    </button>
                    <button className="link-muted" onClick={() => setConfirmDel(null)}>
                      no
                    </button>
                  </span>
                ) : (
                  <button className="link-danger" onClick={() => setConfirmDel(u.username)}>
                    remove
                  </button>
                )}
              </span>
            )}
          </div>
        ))}
      </div>
      <input type="text" placeholder="new username" value={name} onChange={(e) => setName(e.target.value)} />
      <input type="password" placeholder="password" value={pw} onChange={(e) => setPw(e.target.value)} />
      <select value={role} onChange={(e) => setRole(e.target.value)}>
        <option value="admin">Admin</option>
        <option value="super">Super Admin</option>
        <option value="user">User</option>
      </select>
      <button
        className="text-btn"
        onClick={async () => {
          try {
            await api.addUser(name.trim(), pw, role);
            setName("");
            setPw("");
            setErr("");
            load();
          } catch (e) {
            setErr((e as Error).message);
          }
        }}
      >
        Add user
      </button>
      <div className="err">{err}</div>
    </>
  );
}

export function SettingsModal() {
  const { status, canSettings, isAdmin, isSuper, username, refreshStatus, closeModal } = useApp();
  const s = status;
  const fileRef = useRef<HTMLInputElement>(null);
  const [logoErr, setLogoErr] = useState("");
  const [cs, setCs] = useState(s?.brand_callsign || "");
  const [node, setNode] = useState(s?.brand_node || "");
  const [brandMsg, setBrandMsg] = useState("");
  const [footer, setFooter] = useState(s?.footer_text || "");
  const [footerMsg, setFooterMsg] = useState("");
  const [curPw, setCurPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [pwMsg, setPwMsg] = useState<{ text: string; ok: boolean }>({ text: "", ok: true });
  const [qrzUser, setQrzUser] = useState(s?.qrz_username || "");
  const [qrzPw, setQrzPw] = useState("");
  const [qrzMsg, setQrzMsg] = useState<{ text: string; ok: boolean }>({ text: "", ok: true });
  const [idleOn, setIdleOn] = useState(!!s?.voter_idle_timeout);
  const [idleMins, setIdleMins] = useState(String(s?.voter_idle_minutes ?? 10));
  const [voterOff, setVoterOff] = useState(!!s?.voter_polling_disabled);
  const avatarRef = useRef<HTMLInputElement>(null);
  const [avatarErr, setAvatarErr] = useState("");

  const applyVoterIdle = async (enabled: boolean, minutes: number) => {
    try {
      await api.setVoterIdle(enabled, minutes);
      await refreshStatus();
    } catch (e) {
      alert((e as Error).message);
    }
  };

  const applyVoterDisabled = async (disabled: boolean) => {
    try {
      await api.setVoterDisabled(disabled);
      await refreshStatus();
    } catch (e) {
      alert((e as Error).message);
    }
  };

  return (
    <SqModal
      title={canSettings ? "Admin settings" : "Account"}
      onClose={closeModal}
      footer={
        <button className="text-btn primary" onClick={closeModal}>
          Close
        </button>
      }
    >
      {canSettings && s && (
        <>
          <h4>Default theme (new visitors)</h4>
          <div className="theme-grid">
            {s.themes.map((t) => (
              <button
                key={t}
                className={"theme-swatch" + (t === s.theme ? " active" : "")}
                onClick={async () => {
                  try {
                    await api.setTheme(t);
                    await refreshStatus();
                  } catch (e) {
                    alert((e as Error).message);
                  }
                }}
              >
                <span className="dots">
                  {swatchDots(t).map((c, i) => (
                    <i key={i} style={{ background: c, border: "1px solid #7777" }} />
                  ))}
                </span>
                {THEME_LABELS[t] || t}
              </button>
            ))}
          </div>
          <p className="muted">Your own view follows the theme picker in the header (saved per browser).</p>

          <h4>Logo (top-left)</h4>
          <input
            ref={fileRef}
            type="file"
            accept="image/png,image/jpeg,image/svg+xml,image/webp"
            onChange={async () => {
              const file = fileRef.current?.files?.[0];
              if (!file) return;
              try {
                await api.uploadLogo(file);
                setLogoErr("");
                await refreshStatus();
              } catch (e) {
                setLogoErr((e as Error).message);
              }
            }}
          />
          <div className="err">{logoErr}</div>
          <p className="muted">PNG, JPEG, SVG, or WebP · 512 KB max</p>
          <button
            className="link-danger"
            onClick={async () => {
              try {
                await api.deleteLogo();
                await refreshStatus();
              } catch (e) {
                alert((e as Error).message);
              }
            }}
          >
            remove custom logo
          </button>

          <h4>Subtitle (top-left)</h4>
          <div className="filter-row">
            <div>
              <label>Callsign</label>
              <input
                type="text"
                value={cs}
                placeholder={s.callsign || "callsign"}
                onChange={(e) => setCs(e.target.value)}
              />
            </div>
            <div>
              <label>Node number</label>
              <input
                type="text"
                value={node}
                placeholder={s.node_number || "number"}
                onChange={(e) => setNode(e.target.value)}
              />
            </div>
          </div>
          <button
            className="text-btn"
            onClick={async () => {
              try {
                await api.setBranding(cs, node);
                setBrandMsg("saved");
                await refreshStatus();
                setTimeout(() => setBrandMsg(""), 1500);
              } catch (e) {
                setBrandMsg((e as Error).message);
              }
            }}
          >
            Save subtitle
          </button>
          <p className="muted">
            Shown under “Squelch” as “callsign · node &lt;number&gt;”. Blank uses the node&apos;s configured
            values.
          </p>
          <div className="ok-msg">{brandMsg}</div>

          <h4>QRZ lookups</h4>
          <input
            type="text"
            placeholder="QRZ username (callsign)"
            value={qrzUser}
            onChange={(e) => setQrzUser(e.target.value)}
          />
          <input
            type="password"
            placeholder={s.qrz_username ? "QRZ password (unchanged if blank)" : "QRZ password"}
            value={qrzPw}
            onChange={(e) => setQrzPw(e.target.value)}
          />
          <button
            className="text-btn"
            onClick={async () => {
              try {
                const r = await api.setQrz(qrzUser.trim(), qrzPw);
                setQrzPw("");
                setQrzMsg({
                  text: r.requeued
                    ? `saved — re-enriching ${r.requeued} callsigns in the background`
                    : "saved",
                  ok: true,
                });
                await refreshStatus();
                setTimeout(() => setQrzMsg({ text: "", ok: true }), 4000);
              } catch (e) {
                setQrzMsg({ text: (e as Error).message, ok: false });
              }
            }}
          >
            Save QRZ credentials
          </button>
          <div className={qrzMsg.ok ? "ok-msg" : "err"}>{qrzMsg.text}</div>
          <p className="muted">
            A QRZ <b>XML Data</b> (or premium) subscription on this account is required for
            email and photo lookups; without credentials the free FCC feed is used.
          </p>

          <h4>Footer text</h4>
          <input
            type="text"
            value={footer}
            placeholder="System maintained by: …"
            onChange={(e) => setFooter(e.target.value)}
          />
          <button
            className="text-btn"
            onClick={async () => {
              try {
                await api.setFooter(footer);
                await refreshStatus();
                setFooterMsg("saved");
                setTimeout(() => setFooterMsg(""), 1500);
              } catch (e) {
                setFooterMsg((e as Error).message);
              }
            }}
          >
            Save footer
          </button>
          <div className="ok-msg">{footerMsg}</div>

          {s.has_voter && (
            <>
              <h4>Voter polling</h4>
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={voterOff}
                  onChange={(e) => {
                    setVoterOff(e.target.checked);
                    applyVoterDisabled(e.target.checked);
                  }}
                />
                Disable voter polling entirely
              </label>
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={idleOn}
                  disabled={voterOff}
                  onChange={(e) => {
                    setIdleOn(e.target.checked);
                    applyVoterIdle(e.target.checked, Number(idleMins) || 10);
                  }}
                />
                Pause polling when the channel is idle
              </label>
              <div className="filter-row">
                <div>
                  <label>Idle timeout (minutes)</label>
                  <input
                    type="number"
                    min={1}
                    max={240}
                    value={idleMins}
                    disabled={voterOff || !idleOn}
                    onChange={(e) => setIdleMins(e.target.value)}
                    onBlur={() => {
                      if (!voterOff && idleOn)
                        applyVoterIdle(true, Number(idleMins) || 10);
                    }}
                  />
                </div>
              </div>
              <p className="muted">
                <b>Disable entirely</b> fully stops polling the voter feed. Otherwise,{" "}
                <b>pause when idle</b> stops after this many minutes of no traffic and resumes on
                the next transmission (its first ~2s of signal bars may be missing); off = poll
                continuously while linked.
              </p>
            </>
          )}
        </>
      )}

      <h4>Profile picture</h4>
      {s?.avatar_ts && (
        // eslint-disable-next-line @next/next/no-img-element
        <img className="avatar-preview" src={api.avatarUrl(s.avatar_ts)} alt="your profile picture" />
      )}
      <input
        ref={avatarRef}
        type="file"
        accept="image/png,image/jpeg,image/webp"
        onChange={async () => {
          const file = avatarRef.current?.files?.[0];
          if (!file) return;
          try {
            await api.uploadAvatar(file);
            setAvatarErr("");
            await refreshStatus();
          } catch (e) {
            setAvatarErr((e as Error).message);
          }
        }}
      />
      <div className="err">{avatarErr}</div>
      <p className="muted">PNG, JPEG, or WebP · 1 MB max. Shown on your account button.</p>
      {s?.avatar_ts && (
        <button
          className="link-danger"
          onClick={async () => {
            try {
              await api.deleteAvatar();
              await refreshStatus();
            } catch (e) {
              setAvatarErr((e as Error).message);
            }
          }}
        >
          remove picture
        </button>
      )}

      <h4>{`Change password (${username})`}</h4>
      <input
        type="password"
        placeholder="current password"
        value={curPw}
        onChange={(e) => setCurPw(e.target.value)}
      />
      <input
        type="password"
        placeholder="new password"
        value={newPw}
        onChange={(e) => setNewPw(e.target.value)}
      />
      <button
        className="text-btn"
        onClick={async () => {
          try {
            await api.changePassword(curPw, newPw);
            setCurPw("");
            setNewPw("");
            setPwMsg({ text: "password updated", ok: true });
            setTimeout(() => setPwMsg({ text: "", ok: true }), 2000);
          } catch (e) {
            setPwMsg({ text: (e as Error).message, ok: false });
          }
        }}
      >
        Update password
      </button>
      <div className={pwMsg.ok ? "ok-msg" : "err"}>{pwMsg.text}</div>

      {isAdmin && <WatchlistSection />}
      {isAdmin && <VoiceSection />}
      {isSuper && <UsersSection me={username} />}
    </SqModal>
  );
}
