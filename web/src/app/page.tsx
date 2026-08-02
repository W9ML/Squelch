"use client";

import { Header } from "@/components/Header";
import { Feed } from "@/components/Feed";
import { ModalHost } from "@/components/ModalHost";

export default function Page() {
  return (
    <>
      <Header />
      <Feed />
      <ModalHost />
    </>
  );
}
