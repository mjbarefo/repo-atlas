import { describe, expect, it } from "vitest";
import type { Audit } from "./generated/map";
import { auditLabel, findingsForNode } from "./audit";

const audit: Audit = {
  ruleset: "1",
  summary: {
    files: 63,
    modules: 5,
    components: 3,
    edges: 109,
    warnings: 1,
    notices: 1,
  },
  findings: [
    {
      id: "large-file:file:viewer/src/App.tsx",
      code: "large-file",
      severity: "warning",
      message: "App.tsx is large.",
      node_ids: ["file:viewer/src/App.tsx"],
      evidence: ["loc=1121"],
    },
    {
      id: "module-cycle:mod:core",
      code: "module-cycle",
      severity: "notice",
      message: "Core and enrichment form a cycle.",
      node_ids: ["mod:core", "mod:enrichment"],
      evidence: ["Core -> Enrichment", "Enrichment -> Core"],
    },
  ],
};

describe("map audit presentation", () => {
  it("selects every finding attached to a node", () => {
    expect(findingsForNode(audit, "mod:enrichment").map((item) => item.code)).toEqual(
      ["module-cycle"],
    );
    expect(findingsForNode(audit, "file:unrelated.py")).toEqual([]);
    expect(findingsForNode(undefined, "mod:core")).toEqual([]);
  });

  it("summarizes finding counts without hiding clean audits", () => {
    expect(auditLabel(audit)).toBe("1 warning · 1 notice");
    expect(
      auditLabel({
        ...audit,
        summary: { ...audit.summary, warnings: 0, notices: 0 },
        findings: [],
      }),
    ).toBe("Map audit clear");
    expect(auditLabel(undefined)).toBeNull();
  });
});
