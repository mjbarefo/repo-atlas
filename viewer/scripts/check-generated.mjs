import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const viewer = resolve(fileURLToPath(new URL("..", import.meta.url)));
const root = resolve(viewer, "..");
const executable = resolve(viewer, "node_modules", ".bin", "json2ts");
const temporary = mkdtempSync(join(tmpdir(), "atlas-types-"));

try {
  for (const name of ["map", "trace", "impact"]) {
    const schema = resolve(root, "shared", "schemas", `${name}.schema.json`);
    const generated = resolve(temporary, `${name}.ts`);
    const committed = resolve(viewer, "src", "generated", `${name}.ts`);

    execFileSync(executable, ["-i", schema, "-o", generated], {
      cwd: viewer,
      stdio: "inherit",
    });

    if (
      readFileSync(generated, "utf8") !== readFileSync(committed, "utf8")
    ) {
      console.error(`Generated type is stale: src/generated/${name}.ts`);
      process.exitCode = 1;
    }
  }
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
