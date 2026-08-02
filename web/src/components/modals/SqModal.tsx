"use client";

import { Modal, ModalContent, ModalOverlay } from "@chakra-ui/react";
import type { ReactNode } from "react";

/** Chakra Modal shell wearing the app's `.sq-modal` styling. Provides the
 *  overlay + centered card + Escape/click-outside close; content and actions
 *  are plain elements so the ported `.sq-modal ...` form styles apply. */
export function SqModal({
  title,
  wide,
  onClose,
  children,
  footer,
  noClose,
}: {
  title: string;
  wide?: boolean;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  noClose?: boolean;
}) {
  return (
    <Modal
      isOpen
      onClose={onClose}
      isCentered
      scrollBehavior="inside"
      closeOnOverlayClick={!noClose}
      closeOnEsc={!noClose}
    >
      <ModalOverlay bg="rgba(4,6,10,.55)" backdropFilter="blur(3px)" />
      <ModalContent
        className={"sq-modal" + (wide ? " wide" : "")}
        maxW={wide ? "760px" : "520px"}
        maxH="88vh"
        bg="var(--card)"
        color="var(--text)"
      >
        {!noClose && (
          <button className="sq-close" aria-label="Close" title="Close" onClick={onClose}>
            ×
          </button>
        )}
        <h3>{title}</h3>
        <div className="sq-modal-body">{children}</div>
        {footer && <div className="modal-actions">{footer}</div>}
      </ModalContent>
    </Modal>
  );
}
