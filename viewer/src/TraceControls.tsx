import type { ChangeEvent } from "react";
import type { TraceArtifact } from "./generated/trace";
import { traceEnd, traceTurns } from "./trace";

export type LiveStatus = "disconnected" | "connecting" | "live" | "error";

interface TraceControlsProps {
  cursor: number;
  liveStatus: LiveStatus;
  onCursorChange: (cursor: number) => void;
  onLoad: (trace: TraceArtifact) => void;
  onToggleLive: () => void;
  onTurnChange: (turn: number | null) => void;
  provisionalNodeIds: string[];
  trace: TraceArtifact | null;
  turn: number | null;
}

export function isTraceArtifact(value: unknown): value is TraceArtifact {
  if (!value || typeof value !== "object") {
    return false;
  }
  const candidate = value as Partial<TraceArtifact>;
  return (
    candidate.schema_version === "1.0" &&
    typeof candidate.session_id === "string" &&
    Array.isArray(candidate.events) &&
    Boolean(candidate.map_ref) &&
    Boolean(candidate.summary)
  );
}

export function TraceControls({
  cursor,
  liveStatus,
  onCursorChange,
  onLoad,
  onToggleLive,
  onTurnChange,
  provisionalNodeIds,
  trace,
  turn,
}: TraceControlsProps) {
  const turns = trace ? traceTurns(trace) : [];
  const end = trace ? traceEnd(trace) : 0;
  const turnIndex = turn === null ? -1 : turns.indexOf(turn);
  const openTrace = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) {
      return;
    }
    try {
      const value: unknown = JSON.parse(await file.text());
      if (!isTraceArtifact(value)) {
        throw new Error("Not an ATLAS trace artifact.");
      }
      onLoad(value);
    } finally {
      event.target.value = "";
    }
  };

  return (
    <section className="trace-controls" aria-label="Agent trace replay">
      <div className="trace-heading">
        <div>
          <p className="eyebrow">AGENT TRACE</p>
          <strong>{trace?.session_id ?? "No trace loaded"}</strong>
        </div>
        <div className="trace-actions">
          <label className="trace-button">
            Open trace
            <input
              type="file"
              accept=".json,application/json"
              onChange={openTrace}
            />
          </label>
          <button
            className={`trace-button live-status ${liveStatus}`}
            onClick={onToggleLive}
          >
            {liveStatus === "live"
              ? "Disconnect live"
              : liveStatus === "connecting"
                ? "Connecting…"
                : "Connect live"}
          </button>
        </div>
      </div>
      {trace && (
        <>
          <div className="timeline-row">
            <button
              aria-label="Previous turn"
              disabled={turnIndex <= 0}
              onClick={() => onTurnChange(turns[turnIndex - 1] ?? turns.at(-1) ?? null)}
            >
              ←
            </button>
            <button
              className={turn === null ? "active" : ""}
              onClick={() => onTurnChange(null)}
            >
              Timeline
            </button>
            <input
              aria-label="Trace timeline"
              disabled={turn !== null}
              max={Math.max(0.01, end)}
              min="0"
              step="0.01"
              type="range"
              value={Math.min(cursor, end)}
              onChange={(event) => onCursorChange(Number(event.target.value))}
            />
            <output>
              {turn === null
                ? `${cursor.toFixed(1)} / ${end.toFixed(1)}s`
                : `Turn ${turn}`}
            </output>
            <button
              aria-label="Next turn"
              disabled={turnIndex === turns.length - 1}
              onClick={() =>
                onTurnChange(
                  turn === null ? turns[0] ?? null : turns[turnIndex + 1] ?? null,
                )
              }
            >
              →
            </button>
          </div>
          <div className="trace-meta">
            <span>
              <i className="legend-swatch read" /> reads
            </span>
            <span>
              <i className="legend-swatch edit" /> edits
            </span>
            <span>
              <i className="legend-swatch risk" /> unread dependent
            </span>
            <span>{trace.events.length} events</span>
            <span>{turns.length} turns</span>
          </div>
          {provisionalNodeIds.length > 0 && (
            <div className="provisional-list">
              <span>Provisional</span>
              {provisionalNodeIds.map((nodeId) => (
                <code key={nodeId}>{nodeId.replace(/^file:/, "")}</code>
              ))}
            </div>
          )}
        </>
      )}
    </section>
  );
}
