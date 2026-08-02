/** FontAwesome icon map + the bespoke SVGs (MDC batwing, signal bars, brand
 *  mark) kept from the original for visual fidelity. */
import {
  faPlay,
  faPause,
  faTrash,
  faDownload,
  faRotateRight,
  faClone,
  faMap,
  faIdCard,
  faPen,
  faFilter,
  faChartColumn,
  faBook,
  faGear,
  faMagnifyingGlass,
  faSatelliteDish,
  faCalendarDays,
  faBell,
  faBellSlash,
  faRightFromBracket,
  faStar,
  faForwardStep,
  faEllipsis,
  faHeadphones,
  faVolumeLow,
  faVolumeHigh,
  faVolumeXmark,
  faShareNodes,
  faCheck,
  faUsers,
  faListUl,
} from "@fortawesome/free-solid-svg-icons";
import { signalBars } from "@/lib/format";

export const ICONS = {
  play: faPlay,
  pause: faPause,
  trash: faTrash,
  download: faDownload,
  reprocess: faRotateRight,
  similar: faClone,
  map: faMap,
  id: faIdCard,
  edit: faPen,
  filter: faFilter,
  stats: faChartColumn,
  logbook: faBook,
  settings: faGear,
  search: faMagnifyingGlass,
  brand: faSatelliteDish,
  calendar: faCalendarDays,
  bell: faBell,
  bellSlash: faBellSlash,
  logout: faRightFromBracket,
  star: faStar,
  autoplay: faForwardStep,
  more: faEllipsis,
  headphones: faHeadphones,
  volumeLow: faVolumeLow,
  volumeHigh: faVolumeHigh,
  volumeMute: faVolumeXmark,
  share: faShareNodes,
  check: faCheck,
  users: faUsers,
  log: faListUl,
  voter: faSatelliteDish,
  network: faShareNodes,
};

/** Stylized batwing "M" for MDC (Motorola signaling) badges. */
export function MotoIcon() {
  return (
    <svg className="moto" viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="10.5" fill="none" stroke="currentColor" strokeWidth="1.6" />
      <path
        fill="currentColor"
        d="M6.2 16.5 9.4 7.6c.3-.8 1-.8 1.3 0l1.3 3.9 1.3-3.9c.3-.8 1-.8 1.3 0l3.2 8.9h-2.4l-1.7-5.1-1.1 3.3h-1.2l-1.1-3.3-1.7 5.1z"
      />
    </svg>
  );
}

/** 4-bar signal strength glyph driven by an SNR estimate. */
export function SignalBars({ snr }: { snr: number }) {
  const n = signalBars(snr);
  return (
    <svg className="sigbars" viewBox="0 0 15 12" fill="currentColor">
      {[0, 1, 2, 3].map((i) => {
        const h = 3 + i * 3;
        return (
          <rect
            key={i}
            x={i * 4}
            y={12 - h}
            width={3}
            height={h}
            rx={1}
            opacity={i < n ? 1 : 0.25}
          />
        );
      })}
    </svg>
  );
}

/** The Squelch brand mark — a single-cycle sine wave (the logo chosen for the
 *  squelch branding; also the empty-state glyph at larger size). */
export function BrandMark({ className = "brand-icon" }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <path d="M3 12 Q 7.5 5, 12 12 T 21 12" />
    </svg>
  );
}
