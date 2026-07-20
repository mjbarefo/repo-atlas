import type { ImpactArtifact } from "./generated/impact";
import type { TraceCoverage } from "./impact";
import { sourceUrl } from "./graph";

interface ImpactControlsProps {
  coverage: TraceCoverage;
  impact: ImpactArtifact;
  sourceRoot: string;
}

const shortRef = (value: string): string => {
  if (value.startsWith("worktree:")) {
    return `worktree:${value.split(":")[1]?.slice(0, 8) ?? "unknown"}`;
  }
  return value.slice(0, 8);
};

export function ImpactControls({
  coverage,
  impact,
  sourceRoot,
}: ImpactControlsProps) {
  const changes = new Map(impact.files.map((file) => [file.path, file]));

  return (
    <section className="impact-controls" aria-label="Change impact review">
      <div className="impact-heading">
        <div>
          <p className="eyebrow">CHANGE IMPACT</p>
          <strong>
            {shortRef(impact.comparison.base)} →{" "}
            {shortRef(impact.comparison.head)}
          </strong>
        </div>
      </div>
      <div className="impact-meta">
        <span>{impact.summary.changed_files} changed</span>
        <span>{impact.summary.mapped_files} mapped</span>
        <span>{impact.summary.direct_dependents} at risk</span>
        <span>{coverage.unobserved.length} trace-unobserved</span>
      </div>
      <div className="impact-legend">
        <span>
          <i className="change-swatch added" />
          added
        </span>
        <span>
          <i className="change-swatch modified" />
          modified
        </span>
        <span>
          <i className="change-swatch deleted" />
          deleted
        </span>
        <span>
          <i className="change-swatch risk" />
          dependent
        </span>
      </div>
      <h3>Dependency-first review order</h3>
      <ol className="review-order">
        {impact.review_order.map((path) => {
          const change = changes.get(path);
          const observed = coverage.edited.includes(path)
            ? "edited"
            : coverage.read.includes(path)
              ? "read"
              : change?.node_id
                ? "unobserved"
                : "unmapped";
          return (
            <li key={path}>
              <span className={`change-code ${change?.status ?? "modified"}`}>
                {(change?.status ?? "modified").slice(0, 1).toUpperCase()}
              </span>
              <a href={sourceUrl(sourceRoot, path)}>{path}</a>
              <span className={`coverage ${observed}`}>{observed}</span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
