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
import { LogbookModal } from "./modals/LogbookModal";
import { ConnLogModal } from "./modals/ConnLogModal";
import { TimeMachine } from "./TimeMachine";

export function ModalHost() {
  const { modal, status } = useApp();
  // first-run gate: until an admin account exists, the setup wizard supersedes
  // every other modal and can't be dismissed.
  if (status?.needs_setup) return <SetupModal />;
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
