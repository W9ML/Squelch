"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api } from "@/lib/api";
import { THEME_KEY } from "@/lib/format";
import {
  DEFAULT_VOLUME,
  playEnableChirp,
  playVolumePreview,
  setUserVolume,
  unlockAudio,
} from "@/lib/sound";
import { notifyPermission, requestNotify } from "@/lib/notify";
import { registerServiceWorker, subscribeToPush } from "@/lib/push";
import type { Status, Transmission, Watch } from "@/lib/types";

/** The non-standard install-prompt event (Chromium only). */
interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: string }>;
}

const SOUND_KEY = "squelch-sound";
const VOLUME_KEY = "squelch-volume";
const BANDSCOPE_KEY = "squelch-bandscope";

/** Every modal the app can open, with its payload. Mirrors the imperative
 *  openModal(...) calls in the original app.js. */
export type Modal =
  | { kind: "login" }
  | { kind: "logout" }
  | { kind: "filters" }
  | { kind: "settings" }
  | { kind: "stats" }
  | { kind: "logbook" }
  | { kind: "speaker"; tx: Transmission }
  | { kind: "renameSpeaker"; speakerId: number; label: string }
  | { kind: "mdcLink"; unit: string; currentOp: string | null }
  | { kind: "similar"; tx: Transmission }
  | { kind: "callsign"; cs: string }
  | { kind: "connlog" }
  | { kind: "network" }
  | { kind: "timemachine" }
  | {
      kind: "confirm";
      title: string;
      message: string;
      confirmLabel?: string;
      danger?: boolean;
      onConfirm: () => void | Promise<void>;
    };

interface AppContextValue {
  status: Status | null;
  isAdmin: boolean;
  isSuper: boolean;
  canSettings: boolean;
  username: string | null;
  rxActive: boolean;
  /** stuck-repeater gate is up — transcription paused */
  stormActive: boolean;
  /** live count of open connections to the site (logged-in users only) */
  connections: number;
  setConnections: (n: number) => void;
  wsOpen: boolean;
  /** true only while the live-feed WebSocket is actually open (distinct from
   *  wsOpen, which tracks the HTTP status poll) */
  wsConnected: boolean;
  theme: string;
  speakers: Map<number, string>;
  mdcUnits: Record<string, string>;
  /** callsign -> QRZ photo URL (from the enrichment cache) */
  avatars: Record<string, string>;
  /** monotonic counter bumped when speaker labels change, so cards re-render */
  speakerRev: number;

  filters: Record<string, string | number | boolean>;
  searching: boolean;
  searchText: string;
  isFiltered: boolean;
  setFilter: (key: string, value: string | number | boolean) => void;
  clearFilter: (key: string) => void;
  clearAllFilters: () => void;
  applyFilters: (obj: Record<string, string | number | boolean | null | undefined>) => void;
  setSearch: (text: string) => void;
  /** clicking the brand/logo: clear filters + search, drop any permalink, jump to latest */
  homeNonce: number;
  goHome: () => void;

  refreshStatus: () => Promise<void>;
  reloadMdcUnits: () => Promise<void>;
  setSpeakerLabel: (id: number, label: string) => void;
  setLocalTheme: (t: string) => void;
  effectiveTheme: () => string;
  setWsOpen: (b: boolean) => void;
  setWsConnected: (b: boolean) => void;
  setRxActive: (b: boolean) => void;
  setStormActive: (b: boolean) => void;

  paused: boolean;
  togglePaused: () => void;

  soundOn: boolean;
  toggleSound: () => void;
  /** per-browser: show the live audio waterfall bandscope */
  bandscopeOn: boolean;
  toggleBandscope: () => void;
  volume: number; // 0..1
  setVolumePref: (v: number) => void;

  /** watchlist rules (loaded only for settings-capable accounts) */
  watches: Watch[];
  reloadWatches: () => Promise<void>;
  watchedSpeakerId: (id: number) => Watch | undefined;
  watchedCallsign: (cs: string) => Watch | undefined;
  toggleSpeakerWatch: (id: number, label: string) => Promise<void>;
  toggleCallsignWatch: (cs: string, label?: string) => Promise<void>;
  notifyPerm: NotificationPermission;
  enableNotifications: () => Promise<void>;
  /** a Chromium "install app" prompt is available to trigger */
  installAvailable: boolean;
  promptInstall: () => Promise<void>;

  modal: Modal | null;
  openModal: (m: Modal) => void;
  closeModal: () => void;
}

const AppContext = createContext<AppContextValue | null>(null);

export function useApp(): AppContextValue {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("useApp must be used within <AppProvider>");
  return ctx;
}

function applyTheme(theme: string | null) {
  if (theme) document.documentElement.setAttribute("data-theme", theme);
}
function localTheme(): string | null {
  try {
    return localStorage.getItem(THEME_KEY);
  } catch {
    return null;
  }
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [wsOpen, setWsOpen] = useState(false);
  const [wsConnected, setWsConnected] = useState(false);
  const [rxActive, setRxActive] = useState(false);
  const [stormActive, setStormActive] = useState(false);
  const [connections, setConnections] = useState(0);
  const [theme, setTheme] = useState<string>("paper");
  const [mdcUnits, setMdcUnits] = useState<Record<string, string>>({});
  const [avatars, setAvatars] = useState<Record<string, string>>({});
  const [watches, setWatches] = useState<Watch[]>([]);
  const [notifyPerm, setNotifyPerm] = useState<NotificationPermission>("default");
  const [installAvailable, setInstallAvailable] = useState(false);
  const installEvent = useRef<BeforeInstallPromptEvent | null>(null);
  const [modal, setModal] = useState<Modal | null>(null);
  const [speakerRev, setSpeakerRev] = useState(0);
  const speakers = useRef(new Map<number, string>()).current;
  const [filters, setFilters] = useState<Record<string, string | number | boolean>>({});
  const [searching, setSearching] = useState(false);
  const [searchText, setSearchText] = useState("");
  const [paused, setPaused] = useState(false);
  const togglePaused = useCallback(() => setPaused((p) => !p), []);
  const [soundOn, setSoundOn] = useState(true); // on by default; overridden by a saved choice
  const toggleSound = useCallback(() => {
    setSoundOn((on) => {
      const next = !on;
      try {
        localStorage.setItem(SOUND_KEY, next ? "1" : "0");
      } catch {
        /* ignore */
      }
      if (next) playEnableChirp(); // this click is a user gesture -> unlocks + previews
      return next;
    });
  }, []);
  const [bandscopeOn, setBandscopeOn] = useState(false); // opt-in; a saved choice overrides
  const toggleBandscope = useCallback(() => {
    setBandscopeOn((on) => {
      const next = !on;
      try {
        localStorage.setItem(BANDSCOPE_KEY, next ? "1" : "0");
      } catch {
        /* ignore */
      }
      return next;
    });
  }, []);
  const [volume, setVolume] = useState(DEFAULT_VOLUME);
  const setVolumePref = useCallback(
    (v: number) => {
      const clamped = Math.max(0, Math.min(1, v));
      setVolume(clamped);
      try {
        localStorage.setItem(VOLUME_KEY, String(clamped));
      } catch {
        /* ignore */
      }
      if (soundOn) playVolumePreview(clamped); // apply + Windows-style test tone
      else setUserVolume(clamped); // muted: apply silently, no preview chirp
    },
    [soundOn],
  );

  const setFilter = useCallback(
    (key: string, value: string | number | boolean) =>
      setFilters((f) => ({ ...f, [key]: value })),
    [],
  );
  const clearFilter = useCallback(
    (key: string) =>
      setFilters((f) => {
        const n = { ...f };
        delete n[key];
        return n;
      }),
    [],
  );
  const clearAllFilters = useCallback(() => {
    setFilters({});
    setSearchText("");
    setSearching(false);
  }, []);
  const applyFilters = useCallback(
    (obj: Record<string, string | number | boolean | null | undefined>) =>
      setFilters((f) => {
        const n = { ...f };
        for (const [k, v] of Object.entries(obj)) {
          if (v === null || v === undefined || v === "" || v === false) delete n[k];
          else n[k] = v;
        }
        return n;
      }),
    [],
  );
  const setSearch = useCallback((text: string) => {
    setSearchText(text);
    setSearching(!!text);
  }, []);
  const isFiltered = searching || Object.keys(filters).length > 0;

  const [homeNonce, setHomeNonce] = useState(0);
  const goHome = useCallback(() => {
    // drop any ?tx= permalink + #hash so a later refresh won't reopen the deep link
    try {
      if (window.location.search || window.location.hash) {
        window.history.replaceState(null, "", window.location.pathname);
      }
    } catch {
      /* ignore */
    }
    clearAllFilters();          // filters + transcript search
    setHomeNonce((n) => n + 1); // signal the Feed to drop any permalink + jump to latest
  }, [clearAllFilters]);

  const effectiveTheme = useCallback(() => {
    const themes = status?.themes || ["paper"];
    const t = localTheme() || status?.theme;
    return t && themes.includes(t) ? t : themes[0];
  }, [status]);

  const refreshStatus = useCallback(async () => {
    try {
      const s = await api.status();
      setStatus(s);
      setRxActive(!!s.rx_active);
      setStormActive(!!s.storm_active);
      // count is only present for logged-in users; leave it untouched
      // otherwise so an anon status refresh doesn't zero a live value
      if (typeof s.connections === "number") setConnections(s.connections);
      setWsOpen(true);
      const themes = s.themes || ["paper"];
      const t = localTheme() || s.theme;
      const eff = t && themes.includes(t) ? t : themes[0];
      setTheme(eff);
      applyTheme(eff);
    } catch {
      setWsOpen(false);
    }
  }, []);

  const reloadMdcUnits = useCallback(async () => {
    try {
      const { units } = await api.mdcUnits();
      const map: Record<string, string> = {};
      for (const u of units || []) map[u.unit_raw] = u.label;
      setMdcUnits(map);
    } catch {
      /* non-fatal: badges just show the raw unit */
    }
  }, []);

  const reloadAvatars = useCallback(async () => {
    try {
      const { images } = await api.avatars();
      setAvatars(images || {});
    } catch {
      /* non-fatal: avatars fall back to initials */
    }
  }, []);

  const reloadWatches = useCallback(async () => {
    try {
      const { watchlist } = await api.watchlist();
      setWatches(watchlist || []);
    } catch {
      setWatches([]); // not permitted / not logged in — no stars shown
    }
  }, []);

  const enableNotifications = useCallback(async () => {
    const p = await requestNotify(); // must run from a user gesture
    setNotifyPerm(p);
    if (p === "granted") await subscribeToPush(); // register for phone/desktop push
  }, []);

  const promptInstall = useCallback(async () => {
    const e = installEvent.current;
    if (!e) return;
    try {
      await e.prompt();
      await e.userChoice;
    } catch {
      /* ignore */
    }
    installEvent.current = null;
    setInstallAvailable(false);
  }, []);

  const watchedSpeakerId = useCallback(
    (id: number) => watches.find((w) => w.kind === "speaker" && w.value === String(id)),
    [watches],
  );
  const watchedCallsign = useCallback(
    (cs: string) =>
      watches.find(
        (w) => w.kind === "callsign" && (w.value || "").toUpperCase() === cs.toUpperCase(),
      ),
    [watches],
  );

  // starring your first operator/callsign is the natural moment to ask for
  // notification permission — it's a user gesture and the intent is explicit
  const maybePromptNotify = useCallback(async () => {
    let perm = notifyPermission();
    if (perm === "default") {
      perm = await requestNotify();
      setNotifyPerm(perm);
    }
    if (perm === "granted") await subscribeToPush();
  }, []);

  const toggleSpeakerWatch = useCallback(
    async (id: number, label: string) => {
      const existing = watches.find((w) => w.kind === "speaker" && w.value === String(id));
      try {
        if (existing) await api.deleteWatch(existing.id);
        else {
          await maybePromptNotify(); // request while the click gesture is still live
          await api.addWatch("speaker", String(id), "", label);
        }
        await reloadWatches();
      } catch (e) {
        alert((e as Error).message);
      }
    },
    [watches, reloadWatches, maybePromptNotify],
  );

  const toggleCallsignWatch = useCallback(
    async (cs: string, label = "") => {
      const existing = watches.find(
        (w) => w.kind === "callsign" && (w.value || "").toUpperCase() === cs.toUpperCase(),
      );
      try {
        if (existing) await api.deleteWatch(existing.id);
        else {
          await maybePromptNotify(); // request while the click gesture is still live
          await api.addWatch("callsign", cs, "", label || `${cs} mentioned`);
        }
        await reloadWatches();
      } catch (e) {
        alert((e as Error).message);
      }
    },
    [watches, reloadWatches, maybePromptNotify],
  );

  const setSpeakerLabel = useCallback(
    (id: number, label: string) => {
      speakers.set(id, label);
      setSpeakerRev((r) => r + 1);
    },
    [speakers],
  );

  const setLocalTheme = useCallback((t: string) => {
    try {
      localStorage.setItem(THEME_KEY, t);
    } catch {
      /* ignore */
    }
    applyTheme(t);
    setTheme(t);
  }, []);

  // instant theme before first paint settles, then the real status
  useEffect(() => {
    applyTheme(localTheme());
    setNotifyPerm(notifyPermission());
    try {
      const saved = localStorage.getItem(SOUND_KEY);
      if (saved !== null) setSoundOn(saved === "1"); // respect an explicit on/off choice
      const bs = localStorage.getItem(BANDSCOPE_KEY);
      if (bs !== null) setBandscopeOn(bs === "1");
      const vol = localStorage.getItem(VOLUME_KEY);
      if (vol !== null) {
        const v = Math.max(0, Math.min(1, Number(vol) || 0));
        setVolume(v);
        setUserVolume(v); // keep the engine in sync for live alerts
      }
    } catch {
      /* ignore */
    }
    // browsers gate audio until a user gesture — resume the context on the
    // first interaction so an already-enabled preference actually sounds
    const unlock = () => unlockAudio();
    window.addEventListener("pointerdown", unlock, { once: true, passive: true });
    window.addEventListener("keydown", unlock, { once: true });

    // live-sync sound prefs across open tabs/windows: localStorage alone only
    // affects future loads, so muting here must also silence the Squelch tab
    // you forgot about (storage events fire in every tab except the writer)
    const onStorage = (e: StorageEvent) => {
      if (e.key === SOUND_KEY && e.newValue !== null) {
        setSoundOn(e.newValue === "1");
      } else if (e.key === BANDSCOPE_KEY && e.newValue !== null) {
        setBandscopeOn(e.newValue === "1");
      } else if (e.key === VOLUME_KEY && e.newValue !== null) {
        const v = Math.max(0, Math.min(1, Number(e.newValue) || 0));
        setVolume(v);
        setUserVolume(v);
      }
    };
    window.addEventListener("storage", onStorage);

    // register the PWA service worker so the app is installable, and capture
    // the Chromium install prompt so we can offer an "Install app" button
    registerServiceWorker();
    const onBip = (e: Event) => {
      e.preventDefault();
      installEvent.current = e as BeforeInstallPromptEvent;
      setInstallAvailable(true);
    };
    const onInstalled = () => {
      installEvent.current = null;
      setInstallAvailable(false);
    };
    window.addEventListener("beforeinstallprompt", onBip);
    window.addEventListener("appinstalled", onInstalled);

    refreshStatus();
    reloadMdcUnits();
    reloadAvatars();
    const id = setInterval(() => {
      refreshStatus();
      reloadAvatars();     // picks up freshly enriched QRZ photos
    }, 30000);
    return () => {
      clearInterval(id);
      window.removeEventListener("pointerdown", unlock);
      window.removeEventListener("keydown", unlock);
      window.removeEventListener("storage", onStorage);
      window.removeEventListener("beforeinstallprompt", onBip);
      window.removeEventListener("appinstalled", onInstalled);
    };
  }, [refreshStatus, reloadMdcUnits, reloadAvatars]);

  // once we know the account is a logged-in admin and notifications are already
  // granted (e.g. from Phase 1), make sure this device is subscribed to push
  useEffect(() => {
    if (status?.is_admin && notifyPermission() === "granted") subscribeToPush();
  }, [status?.is_admin]);

  // watch rules are settings-gated; load them once the account qualifies so the
  // star toggles can reflect current state (anonymous users just get no stars)
  useEffect(() => {
    if (status?.is_admin) reloadWatches(); // alerts are per logged-in user
    else setWatches([]);
  }, [status?.is_admin, reloadWatches]);

  const value: AppContextValue = useMemo(
    () => ({
      status,
      isAdmin: !!status?.is_admin,
      isSuper: !!status?.is_super,
      canSettings: !!status?.can_settings,
      username: status?.username ?? null,
      rxActive,
      stormActive,
      connections,
      setConnections,
      wsOpen,
      wsConnected,
      theme,
      speakers,
      mdcUnits,
      avatars,
      speakerRev,
      filters,
      searching,
      searchText,
      isFiltered,
      setFilter,
      clearFilter,
      clearAllFilters,
      applyFilters,
      setSearch,
      homeNonce,
      goHome,
      refreshStatus,
      reloadMdcUnits,
      setSpeakerLabel,
      setLocalTheme,
      effectiveTheme,
      setWsOpen,
      setWsConnected,
      setRxActive,
      setStormActive,
      paused,
      togglePaused,
      soundOn,
      toggleSound,
      bandscopeOn,
      toggleBandscope,
      volume,
      setVolumePref,
      watches,
      reloadWatches,
      watchedSpeakerId,
      watchedCallsign,
      toggleSpeakerWatch,
      toggleCallsignWatch,
      notifyPerm,
      enableNotifications,
      installAvailable,
      promptInstall,
      modal,
      openModal: setModal,
      closeModal: () => setModal(null),
    }),
    [
      status,
      rxActive,
      stormActive,
      connections,
      wsOpen,
      wsConnected,
      theme,
      paused,
      togglePaused,
      soundOn,
      toggleSound,
      bandscopeOn,
      toggleBandscope,
      volume,
      setVolumePref,
      watches,
      reloadWatches,
      watchedSpeakerId,
      watchedCallsign,
      toggleSpeakerWatch,
      toggleCallsignWatch,
      notifyPerm,
      enableNotifications,
      installAvailable,
      promptInstall,
      speakers,
      mdcUnits,
      avatars,
      speakerRev,
      filters,
      searching,
      searchText,
      isFiltered,
      setFilter,
      clearFilter,
      clearAllFilters,
      applyFilters,
      setSearch,
      homeNonce,
      goHome,
      refreshStatus,
      reloadMdcUnits,
      setSpeakerLabel,
      setLocalTheme,
      effectiveTheme,
      modal,
    ],
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
