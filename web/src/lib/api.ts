/** Typed client for the FastAPI backend. Same-origin in production (the export
 *  is served by FastAPI); in dev Next proxies /api to the backend, so the empty
 *  base works in both. Errors surface the server's `detail` message, matching
 *  the original app.js behavior. */

import type {
  CallsignInfo,
  Connection,
  ConnStats,
  Facets,
  GeoData,
  LogbookRow,
  MdcUnit,
  SimilarRow,
  SpeakerDetail,
  SpeakerFacet,
  Stats,
  Status,
  Transmission,
  User,
  Watch,
} from "./types";

const BASE = process.env.NEXT_PUBLIC_API_BASE || "";

export class ApiError extends Error {}

async function request<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, { credentials: "same-origin", ...opts });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch {
      /* not json */
    }
    throw new ApiError(detail);
  }
  // some endpoints (logout) return empty
  const text = await res.text();
  return (text ? JSON.parse(text) : {}) as T;
}

function jsonBody(method: string, path: string, body?: unknown) {
  return request(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
}

const post = <T = unknown>(p: string, b?: unknown) => jsonBody("POST", p, b) as Promise<T>;
const del = <T = unknown>(p: string) => jsonBody("DELETE", p) as Promise<T>;
const patch = <T = unknown>(p: string, b?: unknown) => jsonBody("PATCH", p, b) as Promise<T>;

async function uploadFile(path: string, file: File): Promise<void> {
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch(BASE + path, {
    method: "POST",
    body: fd,
    credentials: "same-origin",
  });
  if (!res.ok) {
    let d = res.statusText;
    try {
      d = (await res.json()).detail || d;
    } catch {}
    throw new ApiError(d);
  }
}

export interface FeedParams {
  limit?: number;
  before_id?: number;
  q?: string;
  speaker_id?: number | string;
  origin?: string;
  mdc_unit?: string;
  since?: number;
  until?: number;
  has_mdc?: boolean;
  unnamed?: boolean;
  no_speech?: boolean;
}

export const api = {
  // ---- status / auth ----
  status: () => request<Status>("/api/status"),
  connections: (limit = 500) =>
    request<{ connections: Connection[]; live: number }>(
      `/api/connections?limit=${limit}`),
  connectionStats: (days: number, tz: number) =>
    request<ConnStats>(`/api/connections/stats?days=${days}&tz=${tz}`),
  connectionsExportUrl: (fmt: "csv" | "xlsx", q: string, status: string) =>
    `${BASE}/api/connections/export?fmt=${fmt}&status=${status}` +
    (q ? `&q=${encodeURIComponent(q)}` : ""),
  setVoterIdle: (enabled: boolean, minutes: number) =>
    post("/api/settings/voter_idle", { enabled, minutes }),
  setVoterDisabled: (disabled: boolean) =>
    post("/api/settings/voter_disabled", { disabled }),
  voterStatusUrl: () => `${BASE}/api/link`,
  login: (username: string, password: string) =>
    post("/api/login", { username, password }),
  setup: (body: {
    username: string;
    password: string;
    callsign: string;
    node: string;
    footer: string;
  }) => post("/api/setup", body),
  logout: () => post("/api/logout"),
  changePassword: (current: string, next: string) =>
    post("/api/password", { current, new: next }),

  // ---- feed ----
  transmissions: (params: FeedParams = {}) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === "" || v === false) continue;
      qs.set(k, String(v));
    }
    return request<{ transmissions: Transmission[] }>(`/api/transmissions?${qs}`);
  },
  facets: () => request<Facets>("/api/facets"),
  reprocess: (id: number) => post(`/api/transmissions/${id}/reprocess`),
  secondOpinion: (id: number) => post(`/api/transmissions/${id}/second_opinion`),
  deleteTransmission: (id: number) => del(`/api/transmissions/${id}`),
  purgeCount: (start: number, end: number) =>
    request<{ count: number }>(`/api/transmissions/purge_count?start=${start}&end=${end}`),
  purge: (start: number, end: number) => post("/api/transmissions/purge", { start, end }),
  similar: (id: number) =>
    request<{ transmissions: SimilarRow[] }>(`/api/transmissions/${id}/similar`),
  geo: (id: number) => request<GeoData>(`/api/transmissions/${id}/geo`),

  // ---- speakers ----
  speaker: (id: number) => request<SpeakerDetail>(`/api/speakers/${id}`),
  speakers: () => request<{ speakers: SpeakerFacet[] }>("/api/speakers"),
  assignSpeaker: (txId: number, body: { speaker_id?: number; label?: string }) =>
    post(`/api/transmissions/${txId}/speaker`, body),
  renameSpeaker: (id: number, label: string) =>
    post(`/api/speakers/${id}/rename`, { label }),
  rebuildVoiceprint: (id: number) =>
    post<{ samples: number }>(`/api/speakers/${id}/rebuild`),
  resetVoiceprint: (id: number) => post(`/api/speakers/${id}/reset`),
  resetAllVoiceprints: () =>
    post<{
      samples_deleted: number;
      attributions_cleared: number;
      clusters_dropped: number;
      reseeded: number;
    }>("/api/speakers/reset_voiceprints"),

  // ---- MDC unit linking ----
  mdcUnits: () => request<{ units: MdcUnit[] }>("/api/mdc_units"),
  linkMdcUnit: (unit: string, speaker_id: number) =>
    post("/api/mdc_units", { unit, speaker_id }),
  unlinkMdcUnit: (unit: string) => del(`/api/mdc_units/${encodeURIComponent(unit)}`),

  // ---- stats / callsigns / nodes ----
  transmission: (id: number) =>
    request<{ transmission: Transmission }>(`/api/transmissions/${id}`),
  stats: (days: number, tz: number, today = false,
          since?: number | null, until?: number | null) =>
    request<Stats>(
      `/api/stats?days=${days}&tz=${tz}${today ? "&today=1" : ""}` +
        (since != null ? `&since=${since}` : "") +
        (until != null ? `&until=${until}` : ""),
    ),
  callsign: (cs: string) => request<CallsignInfo>(`/api/callsign/${encodeURIComponent(cs)}`),
  callsigns: () => request<{ callsigns: LogbookRow[] }>("/api/callsigns"),
  avatars: () => request<{ images: Record<string, string> }>("/api/avatars"),
  node: (num: string) =>
    request<{ callsign?: string; description?: string; location?: string }>(
      `/api/nodes/${encodeURIComponent(num)}`,
    ),
  network: () => request<import("./types").NetworkStatus>("/api/network"),

  // ---- users ----
  users: () => request<{ users: User[] }>("/api/users"),
  addUser: (username: string, password: string, role: string) =>
    post("/api/users", { username, password, role }),
  setUserRole: (username: string, role: string) =>
    post(`/api/users/${encodeURIComponent(username)}/role`, { role }),
  deleteUser: (username: string) => del(`/api/users/${encodeURIComponent(username)}`),

  // ---- watchlist ----
  watchlist: () => request<{ watchlist: Watch[] }>("/api/watchlist"),
  addWatch: (kind: string, value: string, webhook = "", label = "") =>
    post<{ ok: boolean; id: number }>("/api/watchlist", { kind, value, webhook, label }),
  deleteWatch: (id: number) => del(`/api/watchlist/${id}`),
  setWatchEnabled: (id: number, enabled: boolean) =>
    post(`/api/watchlist/${id}/enabled`, { enabled }),

  // ---- cases (investigative records) ----
  cases: (status?: string) =>
    request<import("./types").CasesResponse>(
      "/api/cases" + (status ? `?status=${encodeURIComponent(status)}` : "")),
  case: (id: number) => request<import("./types").CaseDetail>(`/api/cases/${id}`),
  createCase: (body: { title: string; subject?: string; summary?: string }) =>
    post<{ ok: boolean; case: import("./types").CaseDetail }>("/api/cases", body),
  updateCase: (
    id: number,
    body: Partial<{ title: string; status: string; subject: string; summary: string }>,
  ) => patch<{ ok: boolean; case: import("./types").CaseDetail }>(`/api/cases/${id}`, body),
  deleteCase: (id: number) => del(`/api/cases/${id}`),
  addCaseItem: (id: number, tx_id: number, label = "", note = "") =>
    post<{ ok: boolean; already: boolean; case: import("./types").CaseDetail }>(
      `/api/cases/${id}/items`, { tx_id, label, note }),
  removeCaseItem: (id: number, item_id: number) =>
    del<{ ok: boolean; case: import("./types").CaseDetail }>(
      `/api/cases/${id}/items/${item_id}`),
  addCaseNote: (id: number, text: string) =>
    post<{ ok: boolean; case: import("./types").CaseDetail }>(
      `/api/cases/${id}/notes`, { text }),
  caseExportUrl: (id: number) => `${BASE}/api/cases/${id}/export`,
  caseExportZipUrl: (id: number) => `${BASE}/api/cases/${id}/export.zip`,
  /** which cases a recording is already filed under (settings-gated) */
  txCases: (tx_id: number) =>
    request<{ cases: { id: number; number: string; title: string }[] }>(
      `/api/transmissions/${tx_id}/cases`),

  // ---- web push ----
  pushVapid: () => request<{ key: string; enabled: boolean }>("/api/push/vapid"),
  pushSubscribe: (sub: { endpoint: string; keys: { p256dh: string; auth: string } }) =>
    post("/api/push/subscribe", sub),
  pushUnsubscribe: (endpoint: string) => post("/api/push/unsubscribe", { endpoint }),

  // ---- site settings ----
  setTheme: (theme: string) => post("/api/settings/theme", { theme }),
  setBranding: (callsign: string, node: string) =>
    post("/api/settings/branding", { callsign, node }),
  setFooter: (text: string) => post("/api/settings/footer", { text }),
  setQrz: (username: string, password: string) =>
    post<{ requeued: number }>("/api/settings/qrz", { username, password }),
  deleteLogo: () => del("/api/settings/logo"),
  setWhisperModel: (model: string) => post("/api/settings/whisper_model", { model }),
  uploadLogo: (file: File) => uploadFile("/api/settings/logo", file),

  // ---- per-user profile picture (account tile) ----
  uploadAvatar: (file: File) => uploadFile("/api/account/avatar", file),
  deleteAvatar: () => del("/api/account/avatar"),
  avatarUrl: (ts: string | number) => `${BASE}/api/account/avatar?v=${ts}`,

  // ---- time machine export (admin-only server render) ----
  tmExport: (start: number, end: number, captions: boolean) =>
    post<{ job_id: string }>("/api/timemachine/export", { start, end, captions }),
  tmExportStatus: (job: string) =>
    request<{ id: string; status: string; progress: number; dur: number; error: string | null }>(
      `/api/timemachine/export/${encodeURIComponent(job)}`),
  tmExportFileUrl: (job: string) => `${BASE}/api/timemachine/export/${encodeURIComponent(job)}/file`,

  // ---- URLs (used directly by <audio>/<img>/download) ----
  audioUrl: (id: number) => `${BASE}/api/transmissions/${id}/audio`,
  mp3Url: (id: number) => `${BASE}/api/transmissions/${id}/audio.mp3`,
  logoUrl: (ts: number) => `${BASE}/api/logo?v=${ts}`,
};
