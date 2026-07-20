"""ATLAS command-line interface."""

import asyncio
from enum import Enum
from pathlib import Path
import tempfile

import typer

from .analysis.analyzer import (
    analyze_repository,
    analyze_repository_incremental,
    write_map,
)
from .config import load_config, provider_environment
from .enrichment import (
    BudgetedEnrichmentClient,
    LiteLLMProvider,
    Pricing,
    enrich_map,
)
from .models import MapArtifact
from .impact import build_impact, write_impact
from .ingestion import ingest_file, write_trace
from .query import cycles as find_cycles
from .query import dependencies, hotspots as find_hotspots, load_map
from .serve import create_server
from .watch import watch_trace


app = typer.Typer(no_args_is_help=True)
query_app = typer.Typer(no_args_is_help=True)
app.add_typer(query_app, name="query", help="Query a completed map artifact.")


class ProviderChoice(str, Enum):
    litellm = "litellm"


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _warn_if_exposed(host: str, what: str) -> None:
    if host not in _LOOPBACK_HOSTS:
        typer.echo(
            f"WARNING: {what} is binding non-loopback host {host!r}; ATLAS has "
            "no authentication and this exposes the served data to the network.",
            err=True,
        )


@app.callback()
def main() -> None:
    """Map source architecture and agent activity."""


@app.command()
def analyze(
    repo: Path = typer.Argument(..., exists=True, file_okay=False, resolve_path=True),
    output: Path | None = typer.Option(None, "--output", "-o"),
    incremental: bool = typer.Option(
        False,
        "--incremental",
        help="Update an existing map by parsing files changed from its source commit.",
    ),
) -> None:
    """Analyze REPO and emit an evidence-backed map artifact."""
    destination = output or repo / ".atlas" / "map.json"
    if incremental:
        if not destination.is_file():
            raise typer.BadParameter(
                f"incremental analysis requires an existing map: {destination}"
            )
        artifact, report = analyze_repository_incremental(repo, _artifact(destination))
        typer.echo(
            f"Incremental: {len(report.changed_files)} changed; "
            f"parsed {report.parsed_files}, reused {report.reused_files}; "
            f"clustering={report.clustering}"
        )
    else:
        artifact = analyze_repository(repo)
    write_map(artifact, destination)
    file_ids = {node.id for node in artifact.nodes if node.kind.value == "file"}
    file_edges = [
        edge
        for edge in artifact.edges
        if edge.source in file_ids and edge.target in file_ids
    ]
    typer.echo(
        f"Analyzed {len(file_ids)} files and {len(file_edges)} imports; "
        f"built {len(artifact.levels.module)} modules and "
        f"{len(artifact.levels.system)} components"
    )
    typer.echo(destination)


@app.command()
def impact(
    repo: Path = typer.Argument(..., exists=True, file_okay=False, resolve_path=True),
    base: str = typer.Option(..., "--base", help="Base Git ref or commit."),
    head: str | None = typer.Option(
        None,
        "--head",
        help="Optional committed head ref; defaults to the current worktree.",
    ),
    map_path: Path | None = typer.Option(
        None,
        "--map",
        exists=True,
        dir_okay=False,
        resolve_path=True,
        help="Current map; defaults to REPO/.atlas/map.json.",
    ),
    output: Path | None = typer.Option(None, "--output", "-o"),
) -> None:
    """Project a local Git comparison onto a completed map."""
    selected_map = map_path or repo / ".atlas" / "map.json"
    if not selected_map.is_file():
        raise typer.BadParameter(f"impact analysis requires a map: {selected_map}")
    try:
        artifact = build_impact(
            repo,
            _artifact(selected_map),
            base=base,
            head=head,
        )
    except ValueError as error:
        typer.echo(f"Impact failed: {error}", err=True)
        raise typer.Exit(1) from error
    destination = output or repo / ".atlas" / "impact.json"
    write_impact(artifact, destination)
    typer.echo(
        f"Impact: {artifact.summary.changed_files} changed; "
        f"{artifact.summary.mapped_files} mapped; "
        f"{artifact.summary.direct_dependents} direct dependents"
    )
    typer.echo(destination)


@app.command("enrich")
def enrich_command(
    map_path: Path = typer.Argument(
        Path(".atlas/map.json"),
        exists=True,
        dir_okay=False,
        resolve_path=True,
    ),
    provider: ProviderChoice | None = typer.Option(None, "--provider"),
    model: str | None = typer.Option(
        None, "--model", help="Provider/model identifier."
    ),
    input_cost: float | None = typer.Option(
        None,
        "--input-cost-per-million",
        min=0,
        help="Explicit input token price in USD.",
    ),
    output_cost: float | None = typer.Option(
        None,
        "--output-cost-per-million",
        min=0,
        help="Explicit output token price in USD.",
    ),
    budget: float | None = typer.Option(None, "--budget", min=0),
    config_path: Path | None = typer.Option(
        None,
        "--config",
        dir_okay=False,
        resolve_path=True,
        help="Config path; defaults to ~/.atlas/config.toml.",
    ),
    output: Path | None = typer.Option(None, "--output", "-o"),
    cache_directory: Path | None = typer.Option(None, "--cache-directory"),
    no_cache: bool = typer.Option(False, "--no-cache"),
    max_prompt_chars: int = typer.Option(30_000, "--max-prompt-chars", min=1_000),
    max_output_tokens: int = typer.Option(8_000, "--max-output-tokens", min=100),
) -> None:
    """Rewrite map prose through a budgeted provider without changing topology."""
    try:
        config = load_config(config_path)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    provider_name = provider.value if provider is not None else config.provider
    if provider_name != ProviderChoice.litellm.value:
        raise typer.BadParameter(f"unsupported provider: {provider_name}")
    selected_model = model or config.model
    selected_input_cost = (
        input_cost if input_cost is not None else config.input_cost_per_million
    )
    selected_output_cost = (
        output_cost if output_cost is not None else config.output_cost_per_million
    )
    selected_budget = budget if budget is not None else config.budget
    if not selected_model:
        raise typer.BadParameter("--model or enrichment.model is required")
    if selected_input_cost is None:
        raise typer.BadParameter(
            "--input-cost-per-million or enrichment.input_cost_per_million is required"
        )
    if selected_output_cost is None:
        raise typer.BadParameter(
            "--output-cost-per-million or enrichment.output_cost_per_million is required"
        )
    artifact = _artifact(map_path)
    cache = (
        None
        if no_cache
        else cache_directory or map_path.parent / "cache" / "enrichment"
    )
    client = BudgetedEnrichmentClient(
        LiteLLMProvider(),
        model=selected_model,
        pricing=Pricing(selected_input_cost, selected_output_cost),
        budget_usd=selected_budget,
        cache_directory=cache,
        max_prompt_chars=max_prompt_chars,
        max_output_tokens=max_output_tokens,
    )
    try:
        with provider_environment(config.provider_keys):
            enriched, report = enrich_map(artifact, client)
    except (RuntimeError, ValueError) as error:
        typer.echo(f"Enrichment failed: {error}", err=True)
        raise typer.Exit(1) from error

    destination = output or map_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary_file:
        temporary = Path(temporary_file.name)
    try:
        write_map(enriched, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)

    for record in client.records:
        typer.echo(
            f"LLM {record.purpose}: model={record.model} temperature=0 "
            f"input={record.input_tokens} output={record.output_tokens} "
            f"cost=${record.cost_usd:.6f} cached={str(record.cached).lower()}"
        )
    typer.echo(
        f"Enriched {report.modules_enriched} modules, "
        f"{report.components_enriched} components, and "
        f"{report.edges_enriched} edges"
    )
    typer.echo(f"LLM total cost: ${client.total_cost_usd:.6f} / ${selected_budget:.6f}")
    typer.echo(destination)


@app.command()
def serve(
    map_path: Path = typer.Argument(
        Path(".atlas/map.json"),
        exists=True,
        dir_okay=False,
        resolve_path=True,
    ),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(4173, "--port", min=0, max=65535),
    viewer_dist: Path | None = typer.Option(
        None,
        "--viewer-dist",
        exists=True,
        file_okay=False,
        resolve_path=True,
    ),
    repo_root: Path | None = typer.Option(
        None,
        "--repo-root",
        exists=True,
        file_okay=False,
        resolve_path=True,
        help="Source root for editor links; inferred for .atlas/map.json.",
    ),
    trace: Path | None = typer.Option(
        None,
        "--trace",
        exists=True,
        dir_okay=False,
        resolve_path=True,
        help="Optional trace artifact to load with the map.",
    ),
    impact_path: Path | None = typer.Option(
        None,
        "--impact",
        exists=True,
        dir_okay=False,
        resolve_path=True,
        help="Optional impact artifact to load with the map.",
    ),
    watch_url: str = typer.Option(
        "ws://127.0.0.1:8765",
        "--watch-url",
        help="Loopback WebSocket URL used by live mode.",
    ),
) -> None:
    """Serve one map artifact in the local interactive viewer."""
    _warn_if_exposed(host, "atlas serve")
    try:
        server = create_server(
            map_path,
            host=host,
            port=port,
            viewer_directory=viewer_dist,
            repo_root=repo_root,
            trace_path=trace,
            impact_path=impact_path,
            watch_url=watch_url,
        )
    except (OSError, ValueError) as error:
        typer.echo(f"Could not start viewer: {error}", err=True)
        raise typer.Exit(1) from error

    bound_host, bound_port = server.server_address[:2]
    typer.echo(f"ATLAS viewer: http://{bound_host}:{bound_port}")
    typer.echo(f"Map: {map_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        typer.echo("Stopping ATLAS viewer")
    finally:
        server.server_close()


@app.command()
def ingest(
    raw_path: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        resolve_path=True,
    ),
    map_path: Path = typer.Option(
        Path(".atlas/map.json"),
        "--map",
        exists=True,
        dir_okay=False,
        resolve_path=True,
    ),
    repo: Path = typer.Option(
        Path("."),
        "--repo",
        exists=True,
        file_okay=False,
        resolve_path=True,
    ),
    output: Path | None = typer.Option(None, "--output", "-o"),
) -> None:
    """Resolve raw Claude Code JSONL events against a completed map."""
    try:
        artifact = _artifact(map_path)
        trace = ingest_file(raw_path, artifact, repo_root=repo)
    except (OSError, ValueError) as error:
        typer.echo(f"Ingest failed: {error}", err=True)
        raise typer.Exit(1) from error
    destination = output or map_path.parent / "traces" / f"{trace.session_id}.json"
    write_trace(trace, destination)
    known_node_ids = {node.id for node in artifact.nodes}
    provisional = {
        event.node_id
        for event in trace.events
        if event.node_id is not None and event.node_id not in known_node_ids
    }
    typer.echo(
        f"Ingested {len(trace.events)} events across "
        f"{max((event.turn for event in trace.events), default=-1) + 1} turns; "
        f"{len(provisional)} provisional paths"
    )
    typer.echo(destination)


@app.command()
def watch(
    raw_path: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        resolve_path=True,
    ),
    map_path: Path = typer.Option(
        Path(".atlas/map.json"),
        "--map",
        exists=True,
        dir_okay=False,
        resolve_path=True,
    ),
    repo: Path = typer.Option(
        Path("."),
        "--repo",
        exists=True,
        file_okay=False,
        resolve_path=True,
    ),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port", min=1, max=65535),
) -> None:
    """Tail raw JSONL and publish resolved trace snapshots over WebSocket."""
    _warn_if_exposed(host, "atlas watch")
    typer.echo(f"ATLAS live trace: ws://{host}:{port}")
    try:
        asyncio.run(
            watch_trace(
                raw_path,
                _artifact(map_path),
                repo_root=repo,
                host=host,
                port=port,
            )
        )
    except KeyboardInterrupt:
        typer.echo("Stopping ATLAS live trace")


def _artifact(map_path: Path) -> MapArtifact:
    try:
        return load_map(map_path)
    except (OSError, ValueError) as error:
        raise typer.BadParameter(f"could not load map: {error}") from error


def _dependency_query(node: str, map_path: Path, *, reverse: bool) -> None:
    artifact = _artifact(map_path)
    try:
        matches = dependencies(artifact, node, reverse=reverse)
    except KeyError as error:
        raise typer.BadParameter(f"unknown node: {node}") from error
    for match in matches:
        typer.echo(match)


@query_app.command()
def deps(
    node: str = typer.Argument(..., help="Node ID to query."),
    map_path: Path = typer.Option(
        Path(".atlas/map.json"),
        "--map",
        exists=True,
        dir_okay=False,
        resolve_path=True,
    ),
) -> None:
    """List direct dependencies of NODE."""
    _dependency_query(node, map_path, reverse=False)


@query_app.command()
def rdeps(
    node: str = typer.Argument(..., help="Node ID to query."),
    map_path: Path = typer.Option(
        Path(".atlas/map.json"),
        "--map",
        exists=True,
        dir_okay=False,
        resolve_path=True,
    ),
) -> None:
    """List direct reverse dependencies of NODE."""
    _dependency_query(node, map_path, reverse=True)


@query_app.command("cycles")
def cycles_command(
    map_path: Path = typer.Option(
        Path(".atlas/map.json"),
        "--map",
        exists=True,
        dir_okay=False,
        resolve_path=True,
    ),
) -> None:
    """List one cycle per strongly connected dependency region."""
    for cycle in find_cycles(_artifact(map_path)):
        typer.echo(" -> ".join((*cycle, cycle[0])))


@query_app.command("hotspots")
def hotspots_command(
    map_path: Path = typer.Option(
        Path(".atlas/map.json"),
        "--map",
        exists=True,
        dir_okay=False,
        resolve_path=True,
    ),
    repo: Path = typer.Option(
        Path("."),
        "--repo",
        exists=True,
        file_okay=False,
        resolve_path=True,
    ),
    limit: int = typer.Option(20, "--limit", min=1),
) -> None:
    """Rank nodes by fan-in multiplied by local Git churn."""
    typer.echo("score\tfan_in\tchurn\tnode")
    for score, fan_in, churn, node_id in find_hotspots(
        _artifact(map_path), repo, limit=limit
    ):
        typer.echo(f"{score}\t{fan_in}\t{churn}\t{node_id}")


if __name__ == "__main__":
    app()
