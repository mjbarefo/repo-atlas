import {
  type ChangeEvent,
  type PointerEvent as ReactPointerEvent,
  type WheelEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { Edge, MapArtifact, Node as MapNode } from "./generated/map";
import type { ImpactArtifact } from "./generated/impact";
import type { TraceArtifact } from "./generated/trace";
import { ImpactControls } from "./ImpactControls";
import {
  type ChangeDisplayStatus,
  impactOverlay,
  isImpactArtifact,
  traceCoverage,
} from "./impact";
import {
  collectFiles,
  edgesForNodes,
  fitGraphTransform,
  type GraphLayout,
  layoutGraph,
  nodesById,
  shouldUseCanvasEdges,
  sourceUrl,
  toMermaid,
  type ViewPath,
  type ViewTransform,
  viewNodeIds,
} from "./graph";
import { traceEnd, traceOverlay, visibleTraceEvents } from "./trace";
import {
  type LiveStatus,
  isTraceArtifact,
  TraceControls,
} from "./TraceControls";

interface PerformanceResult {
  averageFps: number;
  droppedFrames: number;
  durationMs: number;
  frameCount: number;
  p95FrameMs: number;
}

const EMPTY_LAYOUT: GraphLayout = {
  width: 1,
  height: 1,
  nodes: [],
  edges: [],
};

function isMapArtifact(value: unknown): value is MapArtifact {
  if (!value || typeof value !== "object") {
    return false;
  }
  const candidate = value as Partial<MapArtifact>;
  return (
    candidate.schema_version === "1.0" &&
    Array.isArray(candidate.nodes) &&
    Array.isArray(candidate.edges) &&
    Boolean(candidate.repo) &&
    Boolean(candidate.levels)
  );
}

async function readMap(file: File): Promise<MapArtifact> {
  const value: unknown = JSON.parse(await file.text());
  if (!isMapArtifact(value)) {
    throw new Error("This is not an ATLAS map artifact (schema 1.0).");
  }
  return value;
}

export function App() {
  const [artifact, setArtifact] = useState<MapArtifact | null>(null);
  const [sourceRoot, setSourceRoot] = useState(".");
  const [trace, setTrace] = useState<TraceArtifact | null>(null);
  const [impact, setImpact] = useState<ImpactArtifact | null>(null);
  const [traceCursor, setTraceCursor] = useState(0);
  const [traceTurn, setTraceTurn] = useState<number | null>(null);
  const [watchUrl, setWatchUrl] = useState("ws://127.0.0.1:8765");
  const [liveStatus, setLiveStatus] = useState<LiveStatus>("disconnected");
  const socketRef = useRef<WebSocket | null>(null);
  const [loadError, setLoadError] = useState("");
  const [path, setPath] = useState<ViewPath>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [layout, setLayout] = useState<GraphLayout>(EMPTY_LAYOUT);
  const [layoutPending, setLayoutPending] = useState(false);
  const [transform, setTransform] = useState<ViewTransform>({
    x: 56,
    y: 52,
    scale: 1,
  });
  const index = useMemo(
    () => (artifact ? nodesById(artifact) : new Map<string, MapNode>()),
    [artifact],
  );
  const visibleIds = useMemo(
    () => (artifact ? viewNodeIds(artifact, path) : []),
    [artifact, path],
  );
  const visibleNodes = useMemo(
    () => visibleIds.flatMap((id) => (index.get(id) ? [index.get(id)!] : [])),
    [index, visibleIds],
  );
  const visibleEdges = useMemo(
    () => (artifact ? edgesForNodes(artifact, visibleIds) : []),
    [artifact, visibleIds],
  );
  const selected = selectedId ? (index.get(selectedId) ?? null) : null;
  const replayEvents = useMemo(
    () =>
      trace
        ? visibleTraceEvents(trace, {
            cursor: traceCursor,
            turn: traceTurn,
          })
        : [],
    [trace, traceCursor, traceTurn],
  );
  const overlay = useMemo(
    () =>
      artifact
        ? traceOverlay(artifact, replayEvents, visibleIds)
        : {
            activity: new Map(),
            provisionalNodeIds: [],
            riskNodeIds: new Set<string>(),
          },
    [artifact, replayEvents, visibleIds],
  );
  const changeImpact = useMemo(
    () =>
      artifact && impact
        ? impactOverlay(artifact, impact, visibleIds)
        : {
            changeStatuses: new Map<string, ChangeDisplayStatus>(),
            riskNodeIds: new Set<string>(),
          },
    [artifact, impact, visibleIds],
  );
  const coverage = useMemo(
    () =>
      impact
        ? traceCoverage(impact, trace)
        : { edited: [], read: [], unobserved: [] },
    [impact, trace],
  );

  useEffect(() => {
    let active = true;
    Promise.all([
      fetch("/api/map"),
      fetch("/api/context"),
      fetch("/api/trace"),
      fetch("/api/impact"),
    ])
      .then(
        async ([
          mapResponse,
          contextResponse,
          traceResponse,
          impactResponse,
        ]) => {
          if (!mapResponse.ok) {
            throw new Error("No served map");
          }
          const mapValue = (await mapResponse.json()) as unknown;
          const contextValue: {
            repo_root?: unknown;
            watch_url?: unknown;
          } = contextResponse.ok
            ? ((await contextResponse.json()) as {
                repo_root?: unknown;
                watch_url?: unknown;
              })
            : {};
          const traceValue = traceResponse.ok
            ? ((await traceResponse.json()) as unknown)
            : null;
          const impactValue = impactResponse.ok
            ? ((await impactResponse.json()) as unknown)
            : null;
          return { mapValue, contextValue, traceValue, impactValue };
        },
      )
      .then(({ mapValue, contextValue, traceValue, impactValue }) => {
        if (active && isMapArtifact(mapValue)) {
          setArtifact(mapValue);
          if (typeof contextValue.repo_root === "string") {
            setSourceRoot(contextValue.repo_root);
          }
          if (typeof contextValue.watch_url === "string") {
            setWatchUrl(contextValue.watch_url);
          }
          if (
            isTraceArtifact(traceValue) &&
            traceValue.map_ref.commit === mapValue.repo.commit
          ) {
            setTrace(traceValue);
            setTraceCursor(traceEnd(traceValue));
          }
          if (
            isImpactArtifact(impactValue) &&
            impactValue.map_ref.commit === mapValue.repo.commit
          ) {
            setImpact(impactValue);
          }
        }
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, []);

  useEffect(
    () => () => {
      socketRef.current?.close();
    },
    [],
  );

  useEffect(() => {
    let active = true;
    setLayoutPending(true);
    layoutGraph(visibleNodes, visibleEdges)
      .then((next) => {
        if (active) {
          setLayout(next);
          setLayoutPending(false);
        }
      })
      .catch((error: unknown) => {
        if (active) {
          setLoadError(
            error instanceof Error ? error.message : "Layout failed.",
          );
          setLayoutPending(false);
        }
      });
    return () => {
      active = false;
    };
  }, [visibleNodes, visibleEdges]);

  const openArtifact = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) {
      return;
    }
    try {
      setArtifact(await readMap(file));
      setSourceRoot(".");
      setPath([]);
      setSelectedId(null);
      setTrace(null);
      setImpact(null);
      setLoadError("");
    } catch (error) {
      setLoadError(
        error instanceof Error ? error.message : "Could not load map.",
      );
    } finally {
      event.target.value = "";
    }
  };

  const loadImpact = (nextImpact: ImpactArtifact) => {
    if (artifact && nextImpact.map_ref.commit !== artifact.repo.commit) {
      setLoadError(
        `Impact map ${nextImpact.map_ref.commit} does not match this map.`,
      );
      return;
    }
    setImpact(nextImpact);
    setLoadError("");
  };

  const openImpact = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) {
      return;
    }
    try {
      const value: unknown = JSON.parse(await file.text());
      if (!isImpactArtifact(value)) {
        throw new Error("This is not an ATLAS impact artifact (schema 1.0).");
      }
      loadImpact(value);
    } catch (error) {
      setLoadError(
        error instanceof Error ? error.message : "Could not load impact.",
      );
    } finally {
      event.target.value = "";
    }
  };

  const loadTrace = (nextTrace: TraceArtifact) => {
    if (artifact && nextTrace.map_ref.commit !== artifact.repo.commit) {
      setLoadError(
        `Trace commit ${nextTrace.map_ref.commit} does not match this map.`,
      );
      return;
    }
    setTrace(nextTrace);
    setTraceCursor(traceEnd(nextTrace));
    setTraceTurn(null);
    setLoadError("");
  };

  const toggleLive = () => {
    if (socketRef.current) {
      socketRef.current.close();
      socketRef.current = null;
      setLiveStatus("disconnected");
      return;
    }
    setLiveStatus("connecting");
    const socket = new WebSocket(watchUrl);
    socketRef.current = socket;
    socket.onopen = () => setLiveStatus("live");
    socket.onmessage = (message) => {
      try {
        const value: unknown = JSON.parse(String(message.data));
        if (
          value &&
          typeof value === "object" &&
          (value as { type?: unknown }).type === "snapshot"
        ) {
          const nextTrace = (value as { trace?: unknown }).trace;
          if (isTraceArtifact(nextTrace)) {
            loadTrace(nextTrace);
          }
        }
      } catch {
        setLiveStatus("error");
      }
    };
    socket.onerror = () => setLiveStatus("error");
    socket.onclose = () => {
      socketRef.current = null;
      setLiveStatus((current) =>
        current === "error" ? "error" : "disconnected",
      );
    };
  };

  const drill = (node: MapNode) => {
    setSelectedId(node.id);
    if (node.children.length > 0) {
      setPath((current) => [...current, node]);
      setTransform({ x: 56, y: 52, scale: 1 });
    }
  };

  const showBreadcrumb = (length: number) => {
    setPath((current) => current.slice(0, length));
    setSelectedId(null);
    setTransform({ x: 56, y: 52, scale: 1 });
  };

  const mermaidUrl = `data:text/plain;charset=utf-8,${encodeURIComponent(
    toMermaid(visibleNodes, visibleEdges),
  )}`;

  if (!artifact) {
    return (
      <main className="welcome">
        <div className="brand-mark">A</div>
        <p className="eyebrow">ATLAS / LOCAL ARCHITECTURE</p>
        <h1>See the system before you touch it.</h1>
        <p className="lede">
          Open an evidence-backed map to explore components, dependencies, and
          the source beneath them.
        </p>
        <label className="primary-action">
          Open map.json
          <input
            type="file"
            accept=".json,application/json"
            onChange={openArtifact}
          />
        </label>
        {loadError && <p className="error">{loadError}</p>}
        <p className="local-note">Your artifact never leaves this browser.</p>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark small">A</span>
          <div>
            <strong>ATLAS</strong>
            <span>{sourceRoot}</span>
          </div>
        </div>
        <div className="topbar-actions">
          <span className="node-count">{visibleNodes.length} nodes</span>
          <a
            className="secondary-button"
            download={`${path.at(-1)?.label ?? "atlas-system"}.mmd`}
            href={mermaidUrl}
          >
            Export Mermaid
          </a>
          <label className="secondary-button">
            Open map
            <input
              type="file"
              accept=".json,application/json"
              onChange={openArtifact}
            />
          </label>
          <label className="secondary-button">
            Open impact
            <input
              type="file"
              accept=".json,application/json"
              onChange={openImpact}
            />
          </label>
        </div>
      </header>
      <nav className="breadcrumbs" aria-label="Map hierarchy">
        <button onClick={() => showBreadcrumb(0)}>System</button>
        {path.map((node, indexInPath) => (
          <span key={node.id}>
            <span className="crumb-separator">/</span>
            <button onClick={() => showBreadcrumb(indexInPath + 1)}>
              {node.label}
            </button>
          </span>
        ))}
        <span className="level-label">
          {path.at(-1)?.kind === "module"
            ? "Files"
            : path.at(-1)?.kind === "component"
              ? "Modules"
              : "Components"}
        </span>
      </nav>
      <section className="workspace">
        <MapCanvas
          activity={overlay.activity}
          changeStatuses={changeImpact.changeStatuses}
          impactRiskNodeIds={changeImpact.riskNodeIds}
          layout={layout}
          pending={layoutPending}
          selectedId={selectedId}
          transform={transform}
          setTransform={setTransform}
          onNodeClick={drill}
          riskNodeIds={overlay.riskNodeIds}
        />
        <DetailPanel
          sourceRoot={sourceRoot}
          index={index}
          node={selected}
          edges={artifact.edges}
          onClose={() => setSelectedId(null)}
        />
        <TraceControls
          cursor={traceCursor}
          liveStatus={liveStatus}
          onCursorChange={(cursor) => {
            setTraceCursor(cursor);
            setTraceTurn(null);
          }}
          onLoad={loadTrace}
          onToggleLive={toggleLive}
          onTurnChange={setTraceTurn}
          provisionalNodeIds={overlay.provisionalNodeIds}
          trace={trace}
          turn={traceTurn}
        />
        {impact && (
          <ImpactControls
            coverage={coverage}
            impact={impact}
            sourceRoot={sourceRoot}
          />
        )}
      </section>
      {loadError && <div className="error-toast">{loadError}</div>}
    </main>
  );
}

interface MapCanvasProps {
  activity: Map<string, { edits: number; reads: number; total: number }>;
  changeStatuses: Map<string, ChangeDisplayStatus>;
  impactRiskNodeIds: Set<string>;
  layout: GraphLayout;
  pending: boolean;
  selectedId: string | null;
  transform: ViewTransform;
  setTransform: (
    next: ViewTransform | ((current: ViewTransform) => ViewTransform),
  ) => void;
  onNodeClick: (node: MapNode) => void;
  riskNodeIds: Set<string>;
}

function MapCanvas({
  activity,
  changeStatuses,
  impactRiskNodeIds,
  layout,
  pending,
  selectedId,
  transform,
  setTransform,
  onNodeClick,
  riskNodeIds,
}: MapCanvasProps) {
  const drag = useRef<{
    x: number;
    y: number;
    origin: ViewTransform;
  } | null>(null);
  const useCanvasEdges = shouldUseCanvasEdges(layout.nodes.length);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const activityCanvasRef = useRef<HTMLCanvasElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const [performanceResult, setPerformanceResult] =
    useState<PerformanceResult | null>(null);
  const [recordingPerformance, setRecordingPerformance] = useState(false);
  const performanceProbeEnabled = new URLSearchParams(
    window.location.search,
  ).has("perf");

  const resetView = () => {
    const viewport = viewportRef.current;
    if (!viewport) {
      return;
    }
    setTransform(
      fitGraphTransform(
        layout.width,
        layout.height,
        viewport.clientWidth,
        viewport.clientHeight,
      ),
    );
  };

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || pending || layout.nodes.length === 0) {
      return;
    }
    const fit = () =>
      setTransform(
        fitGraphTransform(
          layout.width,
          layout.height,
          viewport.clientWidth,
          viewport.clientHeight,
        ),
      );
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(viewport);
    return () => observer.disconnect();
  }, [layout, pending, setTransform]);

  useEffect(() => {
    if (!useCanvasEdges || !canvasRef.current || !viewportRef.current) {
      return;
    }
    const canvas = canvasRef.current;
    const viewport = viewportRef.current;
    const draw = () => {
      const ratio = window.devicePixelRatio || 1;
      canvas.width = viewport.clientWidth * ratio;
      canvas.height = viewport.clientHeight * ratio;
      canvas.style.width = `${viewport.clientWidth}px`;
      canvas.style.height = `${viewport.clientHeight}px`;
      const context = canvas.getContext("2d");
      if (!context) {
        return;
      }
      context.scale(ratio, ratio);
      context.translate(transform.x, transform.y);
      context.scale(transform.scale, transform.scale);
      context.lineWidth = 1.2 / transform.scale;
      context.strokeStyle = "rgba(98, 125, 139, 0.48)";
      for (const edge of layout.edges) {
        context.beginPath();
        edge.points.forEach((point, index) => {
          if (index === 0) {
            context.moveTo(point.x, point.y);
          } else {
            context.lineTo(point.x, point.y);
          }
        });
        context.stroke();
      }
    };
    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(viewport);
    return () => observer.disconnect();
  }, [layout.edges, transform, useCanvasEdges]);

  useEffect(() => {
    if (!activityCanvasRef.current || !viewportRef.current) {
      return;
    }
    const canvas = activityCanvasRef.current;
    const viewport = viewportRef.current;
    const draw = () => {
      const ratio = window.devicePixelRatio || 1;
      canvas.width = viewport.clientWidth * ratio;
      canvas.height = viewport.clientHeight * ratio;
      canvas.style.width = `${viewport.clientWidth}px`;
      canvas.style.height = `${viewport.clientHeight}px`;
      const context = canvas.getContext("2d");
      if (!context) {
        return;
      }
      context.scale(ratio, ratio);
      context.translate(transform.x, transform.y);
      context.scale(transform.scale, transform.scale);
      const maximum = Math.max(
        1,
        ...[...activity.values()].map((item) => item.total),
      );
      for (const positioned of layout.nodes) {
        const item = activity.get(positioned.node.id);
        const risk = riskNodeIds.has(positioned.node.id);
        const impactRisk = impactRiskNodeIds.has(positioned.node.id);
        if (!item && !risk && !impactRisk) {
          continue;
        }
        if (item) {
          const intensity = 0.28 + (item.total / maximum) * 0.52;
          context.fillStyle =
            item.edits > 0
              ? `rgba(255, 137, 76, ${intensity})`
              : `rgba(76, 171, 255, ${intensity})`;
          context.fillRect(
            positioned.x - 5,
            positioned.y - 5,
            positioned.width + 10,
            positioned.height + 10,
          );
        }
        if (risk) {
          context.strokeStyle = "rgba(255, 63, 76, 0.98)";
          context.lineWidth = 5 / transform.scale;
          context.strokeRect(
            positioned.x - 8,
            positioned.y - 8,
            positioned.width + 16,
            positioned.height + 16,
          );
        }
        if (impactRisk) {
          context.save();
          context.setLineDash([8 / transform.scale, 5 / transform.scale]);
          context.strokeStyle = "rgba(255, 204, 92, 0.98)";
          context.lineWidth = 4 / transform.scale;
          context.strokeRect(
            positioned.x - 7,
            positioned.y - 7,
            positioned.width + 14,
            positioned.height + 14,
          );
          context.restore();
        }
      }
    };
    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(viewport);
    return () => observer.disconnect();
  }, [activity, impactRiskNodeIds, layout.nodes, riskNodeIds, transform]);

  const pointerDown = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (event.button !== 0) {
      return;
    }
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = {
      x: event.clientX,
      y: event.clientY,
      origin: transform,
    };
  };
  const pointerMove = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (!drag.current) {
      return;
    }
    setTransform({
      ...drag.current.origin,
      x: drag.current.origin.x + event.clientX - drag.current.x,
      y: drag.current.origin.y + event.clientY - drag.current.y,
    });
  };
  const pointerUp = (event: ReactPointerEvent<SVGSVGElement>) => {
    drag.current = null;
    event.currentTarget.releasePointerCapture(event.pointerId);
  };
  const zoom = (event: WheelEvent<SVGSVGElement>) => {
    event.preventDefault();
    const bounds = event.currentTarget.getBoundingClientRect();
    const point = {
      x: event.clientX - bounds.left,
      y: event.clientY - bounds.top,
    };
    const nextScale = Math.min(
      2.4,
      Math.max(0.18, transform.scale * Math.exp(-event.deltaY * 0.0015)),
    );
    const graphX = (point.x - transform.x) / transform.scale;
    const graphY = (point.y - transform.y) / transform.scale;
    setTransform({
      scale: nextScale,
      x: point.x - graphX * nextScale,
      y: point.y - graphY * nextScale,
    });
  };

  const recordPerformance = () => {
    if (recordingPerformance) {
      return;
    }
    setPerformanceResult(null);
    setRecordingPerformance(true);
    const frameTimes: number[] = [];
    const started = performance.now();
    let previous: number | null = null;
    const sample = (timestamp: number) => {
      if (previous !== null) {
        frameTimes.push(timestamp - previous);
      }
      previous = timestamp;
      if (performance.now() - started < 6000) {
        requestAnimationFrame(sample);
        return;
      }
      const sorted = [...frameTimes].sort((a, b) => a - b);
      const total = frameTimes.reduce((sum, value) => sum + value, 0);
      const droppedFrames = frameTimes.reduce(
        (sum, value) => sum + Math.max(0, Math.round(value / (1000 / 60)) - 1),
        0,
      );
      setPerformanceResult({
        averageFps: frameTimes.length / (total / 1000),
        droppedFrames,
        durationMs: performance.now() - started,
        frameCount: frameTimes.length,
        p95FrameMs:
          sorted[
            Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95))
          ] ?? 0,
      });
      setRecordingPerformance(false);
    };
    requestAnimationFrame(sample);
  };

  return (
    <div className="map-viewport" ref={viewportRef}>
      {useCanvasEdges && <canvas className="edge-canvas" ref={canvasRef} />}
      <canvas className="activity-canvas" ref={activityCanvasRef} />
      <svg
        aria-label="Architecture map"
        onPointerDown={pointerDown}
        onPointerMove={pointerMove}
        onPointerUp={pointerUp}
        onPointerCancel={pointerUp}
        onWheel={zoom}
      >
        <g
          transform={`translate(${transform.x} ${transform.y}) scale(${transform.scale})`}
        >
          {!useCanvasEdges &&
            layout.edges.map((positioned, index) => (
              <polyline
                className="graph-edge"
                key={`${positioned.edge.source}-${positioned.edge.target}-${index}`}
                points={positioned.points
                  .map((point) => `${point.x},${point.y}`)
                  .join(" ")}
              />
            ))}
          {layout.nodes.map(({ node, x, y, width, height }) => (
            <g
              className={`graph-node ${node.kind} ${
                selectedId === node.id ? "selected" : ""
              } ${
                changeStatuses.has(node.id)
                  ? `change-${changeStatuses.get(node.id)}`
                  : ""
              }`}
              data-node-id={node.id}
              key={node.id}
              transform={`translate(${x} ${y})`}
              onPointerDown={(event) => event.stopPropagation()}
              onClick={() => onNodeClick(node)}
              role="button"
              tabIndex={0}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  onNodeClick(node);
                }
              }}
            >
              <rect width={width} height={height} rx="10" />
              <text className="node-kind" x="16" y="23">
                {node.kind}
              </text>
              {changeStatuses.has(node.id) && (
                <text
                  className="change-badge"
                  x={width - 14}
                  y="23"
                  textAnchor="end"
                >
                  {changeStatuses.get(node.id)}
                </text>
              )}
              <text className="node-label" x="16" y="50">
                {node.label.length > 27
                  ? `${node.label.slice(0, 25)}…`
                  : node.label}
              </text>
              <text className="node-metric" x="16" y="72">
                {node.metrics.loc.toLocaleString()} LOC · {node.metrics.fan_in}{" "}
                in · {node.metrics.fan_out} out
              </text>
              {node.children.length > 0 && (
                <text className="drill-arrow" x={width - 22} y="49">
                  →
                </text>
              )}
            </g>
          ))}
        </g>
      </svg>
      {pending && (
        <div className="layout-status">Computing layered layout…</div>
      )}
      {!pending && layout.nodes.length === 0 && (
        <div className="empty-state">No nodes at this level.</div>
      )}
      <div className="map-controls">
        <button
          aria-label="Zoom out"
          onClick={() =>
            setTransform((current) => ({
              ...current,
              scale: Math.max(0.18, current.scale / 1.2),
            }))
          }
        >
          −
        </button>
        <button aria-label="Reset view" onClick={resetView}>
          {Math.round(transform.scale * 100)}%
        </button>
        <button
          aria-label="Zoom in"
          onClick={() =>
            setTransform((current) => ({
              ...current,
              scale: Math.min(2.4, current.scale * 1.2),
            }))
          }
        >
          +
        </button>
        {performanceProbeEnabled && (
          <button
            className="performance-record"
            disabled={recordingPerformance}
            onClick={recordPerformance}
          >
            {recordingPerformance ? "Recording 6s…" : "Record performance"}
          </button>
        )}
      </div>
      {performanceProbeEnabled && performanceResult && (
        <output
          className="performance-result"
          data-average-fps={performanceResult.averageFps}
          data-dropped-frames={performanceResult.droppedFrames}
          data-duration-ms={performanceResult.durationMs}
          data-frame-count={performanceResult.frameCount}
          data-p95-frame-ms={performanceResult.p95FrameMs}
        >
          {performanceResult.averageFps.toFixed(1)} FPS ·{" "}
          {performanceResult.p95FrameMs.toFixed(2)} ms p95 ·{" "}
          {performanceResult.droppedFrames} dropped
        </output>
      )}
    </div>
  );
}

interface DetailPanelProps {
  sourceRoot: string;
  index: Map<string, MapNode>;
  node: MapNode | null;
  edges: Edge[];
  onClose: () => void;
}

function DetailPanel({
  sourceRoot,
  index,
  node,
  edges: artifactEdges,
  onClose,
}: DetailPanelProps) {
  if (!node) {
    return (
      <aside className="detail-panel empty">
        <p className="eyebrow">INSPECT</p>
        <h2>Select a node</h2>
        <p>
          Choose any node to see its metrics, source files, and dependency
          evidence.
        </p>
      </aside>
    );
  }
  const files = collectFiles(node, index);
  const edges = artifactEdges.filter(
    (edge) => edge.source === node.id || edge.target === node.id,
  );
  return (
    <aside className="detail-panel">
      <button
        className="close-detail"
        aria-label="Close details"
        onClick={onClose}
      >
        ×
      </button>
      <p className="eyebrow">{node.kind.toUpperCase()}</p>
      <h2>{node.label}</h2>
      <p className="summary">{node.summary || "No summary available."}</p>
      <div className="metrics">
        <Metric value={node.metrics.loc} label="LOC" />
        <Metric value={node.metrics.fan_in} label="FAN IN" />
        <Metric value={node.metrics.fan_out} label="FAN OUT" />
      </div>
      <DetailSection title={`Files · ${files.length}`}>
        <ul className="source-list">
          {files.slice(0, 80).map((file) => (
            <li key={file}>
              <a href={sourceUrl(sourceRoot, file)}>{file}</a>
            </li>
          ))}
        </ul>
        {files.length > 80 && <p className="muted">Showing first 80 files.</p>}
      </DetailSection>
      <DetailSection title={`Evidence-backed edges · ${edges.length}`}>
        <ul className="edge-list">
          {edges.map((edge, indexInList) => {
            const other = index.get(
              edge.source === node.id ? edge.target : edge.source,
            );
            return (
              <li key={`${edge.source}-${edge.target}-${indexInList}`}>
                <span>
                  {edge.source === node.id ? "depends on" : "used by"}{" "}
                  <strong>{other?.label ?? "Unknown"}</strong>
                </span>
                {edge.label && <span className="edge-label">{edge.label}</span>}
                <div className="evidence-links">
                  {edge.evidence.slice(0, 4).map((evidence) => (
                    <a
                      key={`${evidence.file}:${evidence.line}`}
                      href={sourceUrl(sourceRoot, evidence.file, evidence.line)}
                    >
                      {evidence.file}:{evidence.line}
                    </a>
                  ))}
                </div>
              </li>
            );
          })}
        </ul>
        {edges.length === 0 && (
          <p className="muted">No edges at this view level.</p>
        )}
      </DetailSection>
    </aside>
  );
}

function Metric({ value, label }: { value: number; label: string }) {
  return (
    <div>
      <strong>{value.toLocaleString()}</strong>
      <span>{label}</span>
    </div>
  );
}

function DetailSection({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="detail-section">
      <h3>{title}</h3>
      {children}
    </section>
  );
}
