import { describe, expect, it } from "vitest";
import sampleMap from "../../shared/fixtures/sample.map.json";
import sampleTrace from "../../shared/fixtures/sample.trace.json";
import type { MapArtifact } from "./generated/map";
import type { TraceArtifact } from "./generated/trace";
import {
  traceEnd,
  traceOverlay,
  traceTurns,
  visibleTraceEvents,
} from "./trace";

const artifact = sampleMap as unknown as MapArtifact;
const trace = sampleTrace as unknown as TraceArtifact;

describe("trace projection", () => {
  it("filters timeline replay and per-turn views", () => {
    expect(visibleTraceEvents(trace, { cursor: 10, turn: null })).toHaveLength(2);
    expect(visibleTraceEvents(trace, { cursor: 0, turn: 3 })).toHaveLength(1);
    expect(traceTurns(trace)).toEqual([1, 2, 3]);
    expect(traceEnd(trace)).toBe(12.5);
  });

  it("projects file activity to the visible hierarchy", () => {
    const system = traceOverlay(artifact, trace.events, ["comp:auth"]);
    expect(system.activity.get("comp:auth")).toEqual({
      edits: 1,
      reads: 1,
      total: 2,
    });
  });

  it("marks unread dependents red and preserves provisional paths", () => {
    const events = [
      {
        ...trace.events[1],
        node_id: "file:src/auth/store.py",
      },
      {
        ...trace.events[0],
        node_id: "file:src/new.py",
        path: "src/new.py",
      },
    ];
    const overlay = traceOverlay(
      artifact,
      events,
      ["file:src/auth/session.py", "file:src/auth/store.py"],
    );
    expect(overlay.riskNodeIds).toEqual(
      new Set(["file:src/auth/session.py"]),
    );
    expect(overlay.provisionalNodeIds).toEqual(["file:src/new.py"]);
  });

  it("marks unread dependents at aggregated zoom levels", () => {
    const editOnly = [
      {
        ...trace.events[1],
        node_id: "file:src/auth/store.py",
      },
    ];
    const zoomedOut = traceOverlay(artifact, editOnly, ["comp:auth"]);
    expect(zoomedOut.riskNodeIds).toEqual(new Set(["comp:auth"]));

    const withDependentRead = [
      ...editOnly,
      {
        ...trace.events[0],
        node_id: "file:src/auth/session.py",
      },
    ];
    expect(
      traceOverlay(artifact, withDependentRead, ["comp:auth"]).riskNodeIds,
    ).toEqual(new Set());
  });
});
