"use client";

/**
 * The live, in-progress transmission. There is no real card yet — `tx_new`
 * only fires once the over ends and the recording is finalized — so this
 * stands in at the bottom of the feed while someone is keyed up, streaming
 * the words in as they're spoken (committed text + a provisional tail). When
 * the over ends its text is handed to the real card so nothing blinks out.
 */
export function LiveCard({
  text,
  pending,
  rxActive,
}: {
  text: string;
  pending: string;
  rxActive: boolean;
}) {
  const hasText = !!(text || pending);
  return (
    <div className="card live" id="live-card" aria-live="polite">
      <div className="avatar live-av" title="On the air">
        <span className="eq" aria-hidden="true">
          <i />
          <i />
          <i />
          <i />
        </span>
      </div>
      <div className="card-main">
        <div className="card-head">
          <span className="speaker-name live-name">On the air</span>
          <span className="live-tag">LIVE</span>
          <span className="head-right">
            <span className="live-now">now</span>
          </span>
        </div>
        <div className="transcript live-transcript">
          {hasText ? (
            <>
              {text}
              {pending && (
                <span className="live-pending">
                  {text ? " " : ""}
                  {pending}
                </span>
              )}
              <span className="live-caret" />
            </>
          ) : (
            <span className="live-listening">{rxActive ? "Receiving…" : "…"}</span>
          )}
        </div>
      </div>
    </div>
  );
}
