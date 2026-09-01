/** Shapes returned by the FastAPI backend. Kept close to the JSON so the port
 *  matches the original app.js field-for-field. */

export interface MdcEntry {
  type?: string;
  unit_raw?: string | null;
  unit_id_hex?: string;
  unit_id?: string | number;
  label: string;
  source?: string;
  node?: string | number;
  op?: number;
}

export interface Quality {
  snr: number;
  label: string;
  clipping?: boolean;
  flutter?: number;
}

export interface VoterNode {
  node: string | number;
  info?: string;
  clients: string[];
  /** each sample: [t, rssi[], votedMask] */
  samples: [number, number[], number][];
}

export interface Voter {
  nodes: VoterNode[];
}

/** whisper word timestamp: [word, start, end] */
export type Word = [string, number, number];

/** one DTMF key press: digit + seconds into the transmission */
export interface DtmfPress {
  d: string;
  t: number;
}

/** Say Again: one extracted callsign, cross-checked against voiceprint /
 *  self-ID / QRZ. `status` is corrected | confirmed | valid | uncertain |
 *  unverified. `spell` is the NATO spellback of the resolved call. */
export interface ResolvedCall {
  heard: string;
  resolved: string;
  status: "corrected" | "confirmed" | "valid" | "uncertain" | "unverified" | string;
  sources: string[];
  spell: string;
  alt?: string;
  alt_spell?: string;
}

export interface Transmission {
  id: number;
  started_at: number;
  ended_at: number | null;
  duration_ms: number;
  has_audio: boolean;
  /** a voiceprint exists — enables "find similar voice" even for unknown voices */
  has_embedding?: boolean;
  transcript: string | null;
  transcript_model: string | null;
  speaker_id: number | null;
  speaker_score: number | null;
  speaker_verified: string | null;
  suggest_speaker_id: number | null;
  suggest_score: number | null;
  suggest_label: string | null;
  mdc: MdcEntry[];
  peaks: number[] | null;
  dtmf: DtmfPress[] | null;
  origin: string | null;
  origin_hub: string | null;
  voter: Voter | null;
  words: Word[] | null;
  quality: Quality | null;
  status: "processing" | "done" | "error" | string;
  qso_id: number | null;
  speaker_label: string | null;
  speaker_named: number | boolean | null;
  callsigns: string[];
  /** present only when Say Again ([sayagain] enabled) is on */
  callsigns_resolved?: ResolvedCall[];
}

export interface Status {
  is_admin: boolean;
  is_super?: boolean;
  can_settings?: boolean;
  username: string | null;
  /** first run — no admin account exists yet; the setup wizard gates the app */
  needs_setup?: boolean;
  avatar_ts?: string | null;
  rx_active: boolean;
  theme: string;
  themes: string[];
  logo_ts?: number | null;
  footer_text?: string;
  callsign?: string;
  node_number?: string;
  /** header brand name shown top-left (defaults to "Squelch") */
  site_name?: string;
  /** optional override for the "Node <n>" subline (e.g. several linked nodes) */
  node_label?: string;
  brand_callsign?: string;
  brand_node?: string;
  models?: string[];
  whisper_model?: string;
  whisper_loading?: string | null;
  queue_depth?: number;
  /** live count of open /ws connections — present only for logged-in users */
  connections?: number;
  last_frame?: number | null;
  /** UDP port the node streams chan_usrp audio to (default 32001) */
  usrp_port?: number;
  has_geo?: boolean;
  /** saved QRZ XML username — only present for settings-capable accounts */
  qrz_username?: string;
  /** voter polling state — only present for settings-capable accounts */
  voter_idle_timeout?: boolean;
  voter_idle_minutes?: number;
  voter_polling_disabled?: boolean;
  /** whether any voter source is configured (settings-capable accounts) */
  has_voter?: boolean;
  /** live audio waterfall bandscope is available (public) */
  has_bandscope?: boolean;
  /** the Time Machine DVR is available */
  has_timemachine?: boolean;
  /** server-side render/export of a scrubbed range is available */
  has_export?: boolean;
  /** Say Again — cross-source callsign resolution + tap-to-loop */
  has_sayagain?: boolean;
  /** ElevenLabs on-demand "second opinion" reprocess is available (admin) */
  has_elevenlabs?: boolean;
  /** stuck-repeater gate is up — transcription paused until it clears */
  storm_active?: boolean;
}

export interface Connection {
  id: number;
  ip: string | null;
  username: string | null;
  user_agent: string | null;
  connected_at: number;
  disconnected_at: number | null;
}

/** Aggregated analytics over the connection log (super/admin only). */
export interface ConnStats {
  totals: {
    sessions: number;
    unique_ips: number;
    unique_users: number;
    total_seconds: number;
    avg_seconds: number;
    anon_sessions: number;
    identified_sessions: number;
    active_now: number;
    peak_concurrent: number;
    first: number | null;
    last: number | null;
  };
  heatmap: number[][];
  trend: { day: string; count: number }[];
  top_ips: { ip: string; sessions: number; seconds: number; last: number }[];
  top_users: { username: string; sessions: number; seconds: number; last: number }[];
  durations: { label: string; count: number }[];
  clients: { name: string; count: number }[];
  live?: number;
}

export interface SpeakerFacet {
  id: number;
  label: string;
  is_named: boolean;
}

export interface Facets {
  origins: string[];
  speakers: SpeakerFacet[];
  mdc_units: string[];
}

export interface SpeakerDetail {
  label: string;
  tx_count: number;
  airtime_ms: number;
  last_heard: number | null;
  hourly: number[];
  is_named?: boolean;
  speaker_id?: number;
}

export interface SimilarRow {
  id: number;
  similarity: number;
  speaker_label: string | null;
  speaker_id: number | null;
  transcript: string | null;
  started_at: number;
}

export interface MdcUnit {
  unit_raw: string;
  label: string;
  speaker_id: number;
}

export interface CallsignInfo {
  callsign: string;
  status: "found" | "not_found" | "disabled" | "error" | string;
  name?: string;
  opclass?: string;
  type?: string;
  city?: string;
  state?: string;
  grid?: string;
  email?: string;
  image?: string;
  source?: string;
  speaker_id?: number | null;
}

export interface LogbookRow {
  callsign: string;
  status?: string;
  name?: string;
  city?: string;
  state?: string;
  opclass?: string;
  count: number;
  last_heard: number;
  speaker_id: number | null;
}

export interface StatsTotals {
  count: number;
  airtime_ms: number;
  speakers: number;
  kerchunks: number;
}

export interface Talker {
  id: number;
  label: string;
  is_named: boolean;
  airtime_ms: number;
  count: number;
}

export interface TrendPoint {
  day: string;
  count: number;
}

export interface NodeStat {
  node: string;
  count: number;
  airtime_ms: number;
}

export interface HubStat {
  hub: string;
  count: number;
  airtime_ms: number;
}

export interface Stats {
  totals: StatsTotals;
  heatmap: number[][];
  talkers: Talker[];
  trend: TrendPoint[];
  by_node: NodeStat[];
  by_hub: HubStat[];
}

export interface NetworkNode {
  node: string;
  name: string | null;
  is_hub: boolean;
}

export interface NetworkHub {
  hub: string;
  name: string | null;
  online: boolean;
  reported_at: number;
  since: number;
  connected: NetworkNode[];
}

export interface NetworkStatus {
  hubs: NetworkHub[];
  generated_at: number;
}

export interface User {
  username: string;
  role: "super" | "admin" | "user" | string;
}

export interface Watch {
  id: number;
  kind: "callsign" | "mdc_unit" | "emergency" | "speaker" | string;
  value: string;
  label?: string;
  webhook?: string;
  enabled?: number | boolean;
}

export interface GeoData {
  receivers: { name: string; lat: number; lon: number }[];
  distances?: Record<string, number>;
  best_est: [number, number];
  track: { t: number; est?: [number, number] | null; rssi: Record<string, number> }[];
  /** ~68% credible radius (km) — the honest error bar on best_est. */
  credible_km?: number;
  confidence?: "low" | "medium" | "high";
  n_measured?: number;
  method?: string;
}

/** WebSocket events pushed from the server. */
export type WsEvent =
  | { type: "rx"; active: boolean }
  | { type: "storm"; active: boolean }
  | { type: "tx_new"; tx: Transmission }
  | { type: "tx_update"; tx: Transmission }
  | { type: "tx_deleted"; id: number }
  | { type: "feed_reload" }
  | { type: "watch_hit"; label?: string; reason?: string; kind?: string; tx?: Transmission }
  | { type: "speaker_renamed"; speaker_id: number; label: string }
  | { type: "model_changed"; model: string }
  | { type: "theme_changed"; theme: string }
  | { type: "footer_changed"; text: string }
  | { type: "subline_changed"; callsign?: string; node?: string }
  | { type: "branding_changed"; logo_ts?: number | null }
  | { type: "mdc_units_changed" }
  | { type: "dtmf"; digit: string }
  | { type: "caption"; text: string; pending: string; seq: number }
  | { type: "presence"; count: number }
  | { type: "bscope"; c: string; seq: number }
  | { type: string; [k: string]: unknown };

/** A case = an investigative record (interference / incident documentation). */
export interface CaseSummary {
  id: number;
  number: string;
  title: string;
  status: string;
  subject: string | null;
  summary: string | null;
  opened_at: number;
  closed_at: number | null;
  created_by: string | null;
  updated_at: number;
  item_count: number;
  first_evidence: number | null;
  last_evidence: number | null;
}

/** One recording (transmission) filed as evidence under a case. */
export interface CaseItem {
  id: number;
  tx_id: number;
  label: string | null;
  note: string | null;
  added_at: number;
  added_by: string | null;
  started_at: number;
  duration_ms: number | null;
  origin: string | null;
  origin_hub: string | null;
  transcript: string | null;
  has_audio: number | boolean;
}

/** An activity-log entry: an operator note or a system audit event. */
export interface CaseNote {
  id: number;
  ts: number;
  author: string | null;
  kind: string; // 'note' | 'system'
  text: string;
}

export interface CaseDetail extends CaseSummary {
  items: CaseItem[];
  notes: CaseNote[];
}

export interface CasesResponse {
  cases: CaseSummary[];
  statuses: string[];
}
