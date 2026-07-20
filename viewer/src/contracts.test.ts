import { readFileSync } from "node:fs";
import Ajv2020 from "ajv/dist/2020";
import type { AnySchema } from "ajv";
import addFormats from "ajv-formats";
import { describe, expect, it } from "vitest";

import type { MapArtifact } from "./generated/map";
import type { ImpactArtifact } from "./generated/impact";
import type { TraceArtifact } from "./generated/trace";

const loadJson = (relativePath: string): unknown =>
  JSON.parse(
    readFileSync(new URL(relativePath, import.meta.url), { encoding: "utf8" }),
  );

const canonicalize = (value: unknown): unknown => {
  if (Array.isArray(value)) {
    return value.map(canonicalize);
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, nested]) => [key, canonicalize(nested)]),
    );
  }
  return value;
};

const canonicalJson = (value: unknown): string =>
  JSON.stringify(canonicalize(value));

const ajv = new Ajv2020({ allErrors: true, strict: true });
addFormats(ajv);

const mapSchema = loadJson("../../shared/schemas/map.schema.json");
const traceSchema = loadJson("../../shared/schemas/trace.schema.json");
const impactSchema = loadJson("../../shared/schemas/impact.schema.json");
const sampleMap = loadJson("../../shared/fixtures/sample.map.json");
const sampleTrace = loadJson("../../shared/fixtures/sample.trace.json");
const sampleImpact = loadJson("../../shared/fixtures/sample.impact.json");

const validateMap = ajv.compile<MapArtifact>(mapSchema as AnySchema);
const validateTrace = ajv.compile<TraceArtifact>(traceSchema as AnySchema);
const validateImpact = ajv.compile<ImpactArtifact>(impactSchema as AnySchema);

describe("artifact contracts", () => {
  it("round-trips a map artifact canonically", () => {
    expect(validateMap(sampleMap), JSON.stringify(validateMap.errors)).toBe(
      true,
    );
    const typedMap: MapArtifact = sampleMap as MapArtifact;
    const roundTripped: MapArtifact = JSON.parse(JSON.stringify(typedMap));

    expect(canonicalJson(roundTripped)).toBe(canonicalJson(sampleMap));
  });

  it("round-trips a trace artifact canonically", () => {
    expect(
      validateTrace(sampleTrace),
      JSON.stringify(validateTrace.errors),
    ).toBe(true);
    const typedTrace: TraceArtifact = sampleTrace as TraceArtifact;
    const roundTripped: TraceArtifact = JSON.parse(JSON.stringify(typedTrace));

    expect(canonicalJson(roundTripped)).toBe(canonicalJson(sampleTrace));
  });

  it("round-trips an impact artifact canonically", () => {
    expect(
      validateImpact(sampleImpact),
      JSON.stringify(validateImpact.errors),
    ).toBe(true);
    const typedImpact: ImpactArtifact = sampleImpact as ImpactArtifact;
    const roundTripped: ImpactArtifact = JSON.parse(
      JSON.stringify(typedImpact),
    );

    expect(canonicalJson(roundTripped)).toBe(canonicalJson(sampleImpact));
  });

  it("rejects unknown map properties", () => {
    const invalid = structuredClone(sampleMap) as Record<string, unknown>;
    invalid.unexpected = true;

    expect(validateMap(invalid)).toBe(false);
  });

  it("rejects edges without evidence", () => {
    const invalid = structuredClone(sampleMap) as {
      edges: Array<{ evidence: unknown[] }>;
    };
    invalid.edges[0].evidence = [];

    expect(validateMap(invalid)).toBe(false);
  });

  it("rejects invalid prose provenance", () => {
    const invalid = structuredClone(sampleMap) as MapArtifact;
    invalid.nodes[0].prose_source = "unknown" as "heuristic";

    expect(validateMap(invalid)).toBe(false);
  });

  it("rejects malformed timestamps", () => {
    const invalid = structuredClone(sampleMap) as MapArtifact;
    invalid.repo.generated_at = "not-a-timestamp";

    expect(validateMap(invalid)).toBe(false);
  });

  it("rejects unsupported trace tools", () => {
    const invalid = structuredClone(sampleTrace) as TraceArtifact;
    invalid.events[0].tool = "Delete" as "Read";

    expect(validateTrace(invalid)).toBe(false);
  });

  it("rejects unsupported impact statuses", () => {
    const invalid = structuredClone(sampleImpact) as ImpactArtifact;
    invalid.files[0].status = "unknown" as "modified";

    expect(validateImpact(invalid)).toBe(false);
  });
});
