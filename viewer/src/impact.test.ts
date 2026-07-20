import { describe, expect, it } from "vitest";
import sampleImpact from "../../shared/fixtures/sample.impact.json";
import sampleMap from "../../shared/fixtures/sample.map.json";
import sampleTrace from "../../shared/fixtures/sample.trace.json";
import type { ImpactArtifact } from "./generated/impact";
import type { MapArtifact } from "./generated/map";
import type { TraceArtifact } from "./generated/trace";
import { impactOverlay, traceCoverage } from "./impact";

const artifact = sampleMap as unknown as MapArtifact;
const impact = sampleImpact as unknown as ImpactArtifact;
const trace = sampleTrace as unknown as TraceArtifact;

describe("change impact projection", () => {
  it("projects changed files and direct dependents through every map level", () => {
    const system = impactOverlay(artifact, impact, ["comp:auth"]);
    expect(system.changeStatuses.get("comp:auth")).toBe("modified");
    expect(system.riskNodeIds.size).toBe(0);

    const files = impactOverlay(artifact, impact, [
      "file:src/auth/session.py",
      "file:src/auth/store.py",
    ]);
    expect(files.changeStatuses.get("file:src/auth/store.py")).toBe("modified");
    expect(files.riskNodeIds).toEqual(new Set(["file:src/auth/session.py"]));
  });

  it("joins optional trace coverage without changing the impact artifact", () => {
    const covered = traceCoverage(impact, trace);
    expect(covered.edited).toEqual([]);
    expect(covered.read).toEqual([]);
    expect(covered.unobserved).toEqual(["src/auth/store.py"]);

    const absent = traceCoverage(impact, null);
    expect(absent.unobserved).toEqual(["src/auth/store.py"]);
  });
});
