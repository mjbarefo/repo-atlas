import type { Audit, AuditFinding } from "./generated/map";

export function findingsForNode(
  audit: Audit | undefined,
  nodeId: string,
): AuditFinding[] {
  if (!audit) {
    return [];
  }
  return audit.findings.filter((finding) =>
    finding.node_ids.includes(nodeId),
  );
}

export function auditLabel(audit: Audit | undefined): string | null {
  if (!audit) {
    return null;
  }
  const { warnings, notices } = audit.summary;
  if (warnings === 0 && notices === 0) {
    return "Map audit clear";
  }
  return `${warnings} warning${warnings === 1 ? "" : "s"} · ${notices} notice${
    notices === 1 ? "" : "s"
  }`;
}
