import { describe, expect, it } from "vitest";
import sample from "../../shared/fixtures/sample.map.json";
import type { MapArtifact } from "./generated/map";
import {
  collectFiles,
  edgesForNodes,
  fitGraphTransform,
  isNonSourceFile,
  isNonSourceView,
  NON_SOURCE_PREFIX,
  nodesById,
  sourceUrl,
  shouldUseCanvasEdges,
  toMermaid,
  viewNodeIds,
  withNonSource,
} from "./graph";

const artifact = sample as unknown as MapArtifact;
const index = nodesById(artifact);

describe("viewer graph model", () => {
  it("drills system to component, module, then file nodes", () => {
    const component = index.get("comp:auth");
    const module = index.get("mod:auth.core");
    expect(component).toBeDefined();
    expect(module).toBeDefined();
    expect(viewNodeIds(artifact, [])).toEqual(["comp:auth"]);
    expect(viewNodeIds(artifact, [component!])).toEqual(["mod:auth.core"]);
    expect(viewNodeIds(artifact, [component!, module!])).toEqual([
      "file:src/auth/session.py",
      "file:src/auth/store.py",
    ]);
  });

  it("filters evidence edges and collects descendant files", () => {
    const module = index.get("mod:auth.core")!;
    expect(collectFiles(module, index)).toEqual([
      "src/auth/session.py",
      "src/auth/store.py",
    ]);
    expect(edgesForNodes(artifact, module.children)).toHaveLength(1);
  });

  it("creates source links and a Mermaid view", () => {
    expect(sourceUrl("/repo", "src/auth/session.py", 4)).toBe(
      "vscode://file//repo/src/auth/session.py:4",
    );
    expect(
      toMermaid(
        moduleNodes(),
        edgesForNodes(
          artifact,
          ["file:src/auth/session.py", "file:src/auth/store.py"],
        ),
      ),
    ).toContain('n_file_src_auth_session_py -->|"SessionStore"|');
  });

  it("switches dense levels to Canvas edges before the 500-node gate", () => {
    expect(shouldUseCanvasEdges(399)).toBe(false);
    expect(shouldUseCanvasEdges(400)).toBe(true);
    expect(shouldUseCanvasEdges(500)).toBe(true);
  });

  it("fits ordinary graphs and preserves a usable dense-view scale", () => {
    expect(fitGraphTransform(2200, 1000, 940, 612)).toEqual({
      x: 36,
      y: 108.72727272727275,
      scale: 0.39454545454545453,
    });
    expect(fitGraphTransform(100000, 1000, 940, 612)).toEqual({
      x: 36,
      y: 36,
      scale: 0.18,
    });
  });
});

describe("non-source files", () => {
  it("keeps non-source files collapsed out of the default layered view", () => {
    const generated = index.get("file:src/generated/api.ts")!;
    expect(generated.role).toBe("generated");
    expect(isNonSourceFile(generated)).toBe(true);
    // Absent from every level, so it never appears when drilling source.
    expect(viewNodeIds(artifact, [])).toEqual(["comp:auth"]);
    const module = index.get("mod:auth.core")!;
    expect(collectFiles(module, index)).not.toContain("src/generated/api.ts");
  });

  it("reveals non-source files as dimmed system buckets when toggled on", () => {
    const revealed = withNonSource(artifact, true);
    const revealedIndex = nodesById(revealed);
    const bucketId = `${NON_SOURCE_PREFIX}src`;

    const systemIds = viewNodeIds(revealed, []);
    expect(systemIds).toContain("comp:auth");
    expect(systemIds).toContain(bucketId);

    const bucket = revealedIndex.get(bucketId)!;
    expect(bucket.kind).toBe("component");
    expect(isNonSourceView(bucket)).toBe(true);
    // Drilling the bucket lists its non-source file nodes directly.
    expect(viewNodeIds(revealed, [bucket])).toEqual([
      "file:src/generated/api.ts",
    ]);
  });

  it("returns the artifact unchanged when the toggle is off", () => {
    expect(withNonSource(artifact, false)).toBe(artifact);
  });
});

function moduleNodes() {
  return [
    index.get("file:src/auth/session.py")!,
    index.get("file:src/auth/store.py")!,
  ];
}
