import type { ImpactArtifact, FileChange } from "./generated/impact";
import type { MapArtifact } from "./generated/map";
import type { TraceArtifact } from "./generated/trace";

export type ChangeDisplayStatus = FileChange["status"] | "mixed";

export interface ImpactOverlay {
  changeStatuses: Map<string, ChangeDisplayStatus>;
  riskNodeIds: Set<string>;
}

export interface TraceCoverage {
  edited: string[];
  read: string[];
  unobserved: string[];
}

export function isImpactArtifact(value: unknown): value is ImpactArtifact {
  if (!value || typeof value !== "object") {
    return false;
  }
  const candidate = value as Partial<ImpactArtifact>;
  return (
    candidate.schema_version === "1.0" &&
    Boolean(candidate.map_ref) &&
    Boolean(candidate.comparison) &&
    Array.isArray(candidate.files) &&
    Array.isArray(candidate.direct_dependents) &&
    Array.isArray(candidate.review_order) &&
    Boolean(candidate.summary)
  );
}

function parentsByChild(artifact: MapArtifact): Map<string, string> {
  const result = new Map<string, string>();
  for (const node of artifact.nodes) {
    for (const child of node.children) {
      result.set(child, node.id);
    }
  }
  return result;
}

function project(
  nodeId: string,
  visible: Set<string>,
  parents: Map<string, string>,
): string | null {
  let current: string | undefined = nodeId;
  while (current) {
    if (visible.has(current)) {
      return current;
    }
    current = parents.get(current);
  }
  return null;
}

export function impactOverlay(
  artifact: MapArtifact,
  impact: ImpactArtifact,
  visibleNodeIds: string[],
): ImpactOverlay {
  const visible = new Set(visibleNodeIds);
  const parents = parentsByChild(artifact);
  const statuses = new Map<string, Set<FileChange["status"]>>();
  for (const change of impact.files) {
    if (!change.node_id) {
      continue;
    }
    const projected = project(change.node_id, visible, parents);
    if (!projected) {
      continue;
    }
    const current = statuses.get(projected) ?? new Set<FileChange["status"]>();
    current.add(change.status);
    statuses.set(projected, current);
  }
  const changeStatuses = new Map<string, ChangeDisplayStatus>();
  for (const [nodeId, values] of statuses) {
    changeStatuses.set(nodeId, values.size === 1 ? [...values][0] : "mixed");
  }
  const riskNodeIds = new Set<string>();
  for (const pair of impact.direct_dependents) {
    const projected = project(pair.dependent_node_id, visible, parents);
    if (projected && !changeStatuses.has(projected)) {
      riskNodeIds.add(projected);
    }
  }
  return { changeStatuses, riskNodeIds };
}

export function traceCoverage(
  impact: ImpactArtifact,
  trace: TraceArtifact | null,
): TraceCoverage {
  const changed = new Map(
    impact.files.flatMap((file) =>
      file.node_id ? [[file.node_id, file.path] as const] : [],
    ),
  );
  const editedIds = new Set<string>();
  const readIds = new Set<string>();
  for (const event of trace?.events ?? []) {
    if (!event.node_id || !changed.has(event.node_id)) {
      continue;
    }
    if (event.tool === "Edit" || event.tool === "Write") {
      editedIds.add(event.node_id);
    }
    if (event.tool === "Read" || event.tool === "Grep") {
      readIds.add(event.node_id);
    }
  }
  const edited = [...editedIds].map((id) => changed.get(id)!).sort();
  const read = [...readIds].map((id) => changed.get(id)!).sort();
  const observed = new Set([...editedIds, ...readIds]);
  const unobserved = [...changed]
    .filter(([id]) => !observed.has(id))
    .map(([, path]) => path)
    .sort();
  return { edited, read, unobserved };
}
