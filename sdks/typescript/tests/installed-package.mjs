// Pack, install and exercise the public entry point from outside the repository.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { copyFileSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const packageDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const root = resolve(packageDir, "../..");
const temporary = mkdtempSync(join(tmpdir(), "andy-typescript-sdk-"));
function run(command, args, cwd = temporary) {
  execFileSync(command, args, { cwd, stdio: "inherit" });
}

// npm 10 yields an array; npm 12 yields an object keyed by package name.
// Both must contain exactly one complete pack entry. Never skip file checks.
function onePackEntry(result) {
  assert(result && typeof result === "object", "npm pack JSON must be an object or array");
  const entries = Array.isArray(result) ? result : Object.values(result);
  assert.equal(entries.length, 1, "npm pack must describe exactly one package");
  const [entry] = entries;
  assert(entry && typeof entry === "object" && !Array.isArray(entry), "invalid npm pack entry");
  assert.equal(entry.name, "@attention-router/integration-sdk");
  assert.equal(typeof entry.filename, "string");
  assert(Array.isArray(entry.files), "npm pack metadata must contain a files array");
  return entry;
}

try {
  // Exercise the two verified CLI output schemas independently of installed npm.
  const fixture = { name: "@attention-router/integration-sdk", filename: "fixture.tgz", files: [] };
  assert.equal(onePackEntry([fixture]), fixture);
  assert.equal(onePackEntry({ "@attention-router/integration-sdk": fixture }), fixture);
  const packed = onePackEntry(JSON.parse(execFileSync("npm", [
    "pack", "--ignore-scripts", "--json", "--pack-destination", temporary,
  ], { cwd: packageDir, encoding: "utf8" })));
  assert(packed.files.some(file => file.path === "dist/_schema.json"));
  assert(packed.files.some(file => file.path === "dist/index.d.ts"));
  assert(packed.files.every(file => /^(dist\/|package.json$|README.md$|LICENSE$)/.test(file.path)));
  writeFileSync(join(temporary, "package.json"), JSON.stringify({ private: true }));
  run("npm", ["install", "--ignore-scripts", "--no-audit", "--no-fund", join(temporary, packed.filename)]);
  for (const name of ["sdk.test.cjs", "esm.test.mjs", "types.test.ts"]) {
    copyFileSync(join(packageDir, "tests", name), join(temporary, name));
  }
  for (const name of ["integration-contract.schema.json", "conformance-cases.json"]) {
    copyFileSync(join(root, "contracts/integration/v1", name), join(temporary, name));
  }
  run(process.execPath, ["--test", "sdk.test.cjs", "esm.test.mjs"]);
  copyFileSync(join(packageDir, "examples/offline.ts"), join(temporary, "offline.ts"));
  writeFileSync(join(temporary, "tsconfig.json"), JSON.stringify({
    compilerOptions: {
      strict: true, target: "ES2022", module: "Node16", moduleResolution: "Node16",
      outDir: "compiled",
    },
    files: ["offline.ts", "types.test.ts"],
  }));
  run(join(packageDir, "node_modules/.bin/tsc"), ["-p", join(temporary, "tsconfig.json")]);
  run(process.execPath, [join(temporary, "compiled/offline.js")]);
  const installed = JSON.parse(readFileSync(join(temporary,
    "node_modules/@attention-router/integration-sdk/package.json"), "utf8"));
  assert.deepEqual(Object.keys(installed.dependencies).sort(), ["ajv", "ajv-formats"]);
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
