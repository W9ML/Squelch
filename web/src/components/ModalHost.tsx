"use client";

import { useApp } from "@/state/app-context";
import { SetupModal } from "./modals/SetupModal";
import { LoginModal } from "./modals/LoginModal";
import { LogoutModal } from "./modals/LogoutModal";
import { ConfirmModal } from "./modals/ConfirmModal";
import { FiltersModal } from "./modals/FiltersModal";
import { SpeakerModal } from "./modals/SpeakerModal";
import { RenameSpeakerModal } from "./modals/RenameSpeakerModal";
import { MdcLinkModal } from "./modals/MdcLinkModal";
import { SimilarModal } from "./modals/SimilarModal";
import { CallsignModal } from "./modals/CallsignModal";
import { SettingsModal } from "./modals/SettingsModal";
import { StatsModal } from "./modals/StatsModal";
import { NetworkModal } from "./modals/NetworkModal";
import { CasesModal } from "./modals/CasesModal";
import { AddToCaseModal } from "./modals/AddToCaseModal";
import { LogbookModal } from "./modals/LogbookModal";
import { ConnLogModal } from "./modals/ConnLogModal";
import { TimeMachine } from "./TimeMachine";
import { SqModal } from "./modals/SqModal";
import { TwoFactorEnroll } from "./modals/SecuritySections";
import { api } from "@/lib/api";

/** [web] require_2fa: an account without an authenticator can do nothing
 *  else until it enrols one (or signs out). */
function EnrollGate() {
  const { refreshStatus } = useApp();
  return (
    <SqModal
      title="Set up two-factor sign-in"
      noClose
      onClose={() => {}}
      footer={
        <button
          className="text-btn"
          onClick={async () => {
            await api.logout().catch(() => {});
            await refreshStatus();
          }}
        >
          Log out
        </button>
      }
    >
      <p className="setup-intro">
        This site requires an authenticator app for every account. Set yours up
        to continue.
      </p>
      <TwoFactorEnroll onDone={refreshStatus} />
    </SqModal>
  );
}

export function ModalHost() {
  const { modal, status, locked } = useApp();
  // first-run gate: until an admin account exists, the setup wizard supersedes
  // every other modal and can't be dismissed.
  if (status?.needs_setup) return <SetupModal />;
  // signed in, but the site requires two-factor and this account has none
  if (status?.must_enroll_2fa) return <EnrollGate />;
  // private mode: the sign-in door, which can't be dismissed
  if (locked) return <LoginModal gate />;
  if (!modal) return null;
  switch (modal.kind) {
    case "timemachine":
      return <TimeMachine />;
    case "login":
      return <LoginModal />;
    case "logout":
      return <LogoutModal />;
    case "filters":
      return <FiltersModal />;
    case "settings":
      return <SettingsModal />;
    case "stats":
      return <StatsModal />;
    case "network":
      return <NetworkModal />;
    case "cases":
      return <CasesModal />;
    case "addToCase":
      return <AddToCaseModal txs={modal.txs} onFiled={modal.onFiled} />;
    case "logbook":
      return <LogbookModal />;
    case "connlog":
      return <ConnLogModal />;
    case "speaker":
      return <SpeakerModal tx={modal.tx} />;
    case "renameSpeaker":
      return <RenameSpeakerModal speakerId={modal.speakerId} label={modal.label} />;
    case "mdcLink":
      return <MdcLinkModal unit={modal.unit} currentOp={modal.currentOp} />;
    case "similar":
      return <SimilarModal tx={modal.tx} />;
    case "callsign":
      return <CallsignModal cs={modal.cs} />;
    case "confirm":
      return <ConfirmModal m={modal} />;
    default:
      return null;
  }
}
