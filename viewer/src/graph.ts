import ELK, {
  type ElkExtendedEdge,
  type ElkNode,
} from "elkjs/lib/elk.bundled.js";
import type {
  Edge,
  MapArtifact,
  Node as MapNode,
} from "./generated/map";

export type ViewPath = MapNode[];

export interface PositionedNode {
  node: MapNode;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface PositionedEdge {
  edge: Edge;
  points: Array<{ x: number; y: number }>;
}

export interface GraphLayout {
  width: number;
  height: number;
  nodes: PositionedNode[];
  edges: PositionedEdge[];
}

export interface ViewTransform {
  x: number;
  y: number;
  scale: number;
}

const elk = new ELK();
const NODE_WIDTH = 220;
const NODE_HEIGHT = 88;

export function fitGraphTransform(
  layoutWidth: number,
  layoutHeight: number,
  viewportWidth: number,
  viewportHeight: number,
  padding = 36,
): ViewTransform {
  const availableWidth = Math.max(1, viewportWidth - padding * 2);
  const availableHeight = Math.max(1, viewportHeight - padding * 2);
  const fitScale = Math.min(
    availableWidth / Math.max(1, layoutWidth),
    availableHeight / Math.max(1, layoutHeight),
    1,
  );
  const scale = Math.max(0.18, fitScale);
  if (fitScale < 0.18) {
    return { x: padding, y: padding, scale };
  }
  return {
    x: (viewportWidth - layoutWidth * scale) / 2,
    y: (viewportHeight - layoutHeight * scale) / 2,
    scale,
  };
}

export function nodesById(artifact: MapArtifact): Map<string, MapNode> {
  return new Map(artifact.nodes.map((node) => [node.id, node]));
}

export function viewNodeIds(
  artifact: MapArtifact,
  path: ViewPath,
): string[] {
  if (path.length === 0) {
    return artifact.levels.system;
  }
  const parent = path.at(-1);
  if (!parent) {
    return [];
  }
  if (parent.kind === "component") {
    return artifact.levels.component[parent.id] ?? parent.children;
  }
  if (parent.kind === "module") {
    return artifact.levels.module[parent.id] ?? parent.children;
  }
  return [];
}

export function edgesForNodes(
  artifact: MapArtifact,
  nodeIds: string[],
): Edge[] {
  const visible = new Set(nodeIds);
  return artifact.edges.filter(
    (edge) => visible.has(edge.source) && visible.has(edge.target),
  );
}

export function shouldUseCanvasEdges(nodeCount: number): boolean {
  return nodeCount >= 400;
}

export function collectFiles(
  node: MapNode,
  index: Map<string, MapNode>,
): string[] {
  const files = new Set(node.files);
  const visit = (candidate: MapNode) => {
    for (const file of candidate.files) {
      files.add(file);
    }
    for (const childId of candidate.children) {
      const child = index.get(childId);
      if (child) {
        visit(child);
      }
    }
  };
  visit(node);
  return [...files].sort();
}

export function sourceUrl(root: string, file: string, line?: number): string {
  const normalizedRoot = root.replace(/\/+$/, "");
  const normalizedFile = file.replace(/^\/+/, "");
  const absolute = file.startsWith("/")
    ? file
    : `${normalizedRoot}/${normalizedFile}`;
  return `vscode://file/${encodeURI(absolute)}${line ? `:${line}` : ""}`;
}

function mermaidId(id: string): string {
  return `n_${id.replace(/[^a-zA-Z0-9_]/g, "_")}`;
}

function mermaidLabel(label: string): string {
  return label.replaceAll('"', "'").replaceAll("\n", " ");
}

export function toMermaid(nodes: MapNode[], edges: Edge[]): string {
  const lines = ["flowchart LR"];
  for (const node of nodes) {
    lines.push(`  ${mermaidId(node.id)}["${mermaidLabel(node.label)}"]`);
  }
  for (const edge of edges) {
    const label = edge.label ? `|"${mermaidLabel(edge.label)}"|` : "";
    lines.push(
      `  ${mermaidId(edge.source)} -->${label} ${mermaidId(edge.target)}`,
    );
  }
  return `${lines.join("\n")}\n`;
}

export async function layoutGraph(
  nodes: MapNode[],
  edges: Edge[],
): Promise<GraphLayout> {
  if (nodes.length === 0) {
    return { width: 1, height: 1, nodes: [], edges: [] };
  }
  const graph: ElkNode = {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.edgeRouting": "ORTHOGONAL",
      "elk.layered.spacing.nodeNodeBetweenLayers": "110",
      "elk.spacing.nodeNode": "42",
      "elk.padding": "[top=36,left=36,bottom=36,right=36]",
    },
    children: nodes.map((node) => ({
      id: node.id,
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
    })),
    edges: edges.map(
      (edge, index): ElkExtendedEdge => ({
        id: `edge-${index}`,
        sources: [edge.source],
        targets: [edge.target],
      }),
    ),
  };
  const result = await elk.layout(graph);
  const nodeIndex = new Map(nodes.map((node) => [node.id, node]));
  const positionedNodes = (result.children ?? []).flatMap((item) => {
    const node = nodeIndex.get(item.id);
    return node
      ? [
          {
            node,
            x: item.x ?? 0,
            y: item.y ?? 0,
            width: item.width ?? NODE_WIDTH,
            height: item.height ?? NODE_HEIGHT,
          },
        ]
      : [];
  });
  const positionedEdges = (result.edges ?? []).flatMap((item, index) => {
    const edge = edges[index];
    const section = item.sections?.[0];
    if (!edge || !section) {
      return [];
    }
    return [
      {
        edge,
        points: [
          section.startPoint,
          ...(section.bendPoints ?? []),
          section.endPoint,
        ],
      },
    ];
  });
  return {
    width: result.width ?? 1,
    height: result.height ?? 1,
    nodes: positionedNodes,
    edges: positionedEdges,
  };
}
