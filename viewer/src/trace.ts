import type { MapArtifact } from "./generated/map";
import type {
  Event as TraceEvent,
  TraceArtifact,
} from "./generated/trace";

export interface NodeActivity {
  edits: number;
  reads: number;
  total: number;
}

export interface TraceOverlay {
  activity: Map<string, NodeActivity>;
  provisionalNodeIds: string[];
  riskNodeIds: Set<string>;
}

export function visibleTraceEvents(
  trace: TraceArtifact,
  {
    cursor,
    turn,
  }: {
    cursor: number;
    turn: number | null;
  },
): TraceEvent[] {
  if (turn !== null) {
    return trace.events.filter((event) => event.turn === turn);
  }
  return trace.events.filter((event) => event.t <= cursor);
}

function hierarchy(artifact: MapArtifact) {
  const moduleByFile = new Map<string, string>();
  for (const [moduleId, fileIds] of Object.entries(artifact.levels.module)) {
    for (const fileId of fileIds) {
      moduleByFile.set(fileId, moduleId);
    }
  }
  const componentByModule = new Map<string, string>();
  for (const [componentId, moduleIds] of Object.entries(
    artifact.levels.component,
  )) {
    for (const moduleId of moduleIds) {
      componentByModule.set(moduleId, componentId);
    }
  }
  return { componentByModule, moduleByFile };
}

function projectNode(
  nodeId: string,
  visible: Set<string>,
  moduleByFile: Map<string, string>,
  componentByModule: Map<string, string>,
): string | null {
  if (visible.has(nodeId)) {
    return nodeId;
  }
  const moduleId = moduleByFile.get(nodeId);
  if (moduleId && visible.has(moduleId)) {
    return moduleId;
  }
  const componentId = moduleId ? componentByModule.get(moduleId) : undefined;
  return componentId && visible.has(componentId) ? componentId : null;
}

export function traceOverlay(
  artifact: MapArtifact,
  events: TraceEvent[],
  visibleNodeIds: string[],
): TraceOverlay {
  const visible = new Set(visibleNodeIds);
  const known = new Set(artifact.nodes.map((node) => node.id));
  const { componentByModule, moduleByFile } = hierarchy(artifact);
  const activity = new Map<string, NodeActivity>();
  const provisional = new Set<string>();
  const readFileIds = new Set<string>();
  const editedFileIds = new Set<string>();

  for (const event of events) {
    if (!event.node_id) {
      continue;
    }
    if (!known.has(event.node_id)) {
      provisional.add(event.node_id);
      continue;
    }
    if (event.tool === "Edit" || event.tool === "Write") {
      editedFileIds.add(event.node_id);
    } else if (event.tool === "Read" || event.tool === "Grep") {
      readFileIds.add(event.node_id);
    }
    const projected = projectNode(
      event.node_id,
      visible,
      moduleByFile,
      componentByModule,
    );
    if (!projected) {
      continue;
    }
    const current = activity.get(projected) ?? { edits: 0, reads: 0, total: 0 };
    if (event.tool === "Edit" || event.tool === "Write") {
      current.edits += 1;
    } else if (event.tool === "Read" || event.tool === "Grep") {
      current.reads += 1;
    }
    current.total += 1;
    activity.set(projected, current);
  }

  // Risk is decided at file granularity (map edges are file→file), then each
  // risky dependent is projected up to the visible level; comparing raw edge
  // endpoints against module/component ids would silently never match.
  const riskNodeIds = new Set<string>();
  for (const edge of artifact.edges) {
    if (!editedFileIds.has(edge.target) || readFileIds.has(edge.source)) {
      continue;
    }
    const projected = projectNode(
      edge.source,
      visible,
      moduleByFile,
      componentByModule,
    );
    if (projected) {
      riskNodeIds.add(projected);
    }
  }
  return {
    activity,
    provisionalNodeIds: [...provisional].sort(),
    riskNodeIds,
  };
}

export function traceTurns(trace: TraceArtifact): number[] {
  return [...new Set(trace.events.map((event) => event.turn))].sort(
    (left, right) => left - right,
  );
}

export function traceEnd(trace: TraceArtifact): number {
  return Math.max(0, ...trace.events.map((event) => event.t));
}
