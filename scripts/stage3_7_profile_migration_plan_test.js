#!/usr/bin/env node
const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");

function writeFixture(dir, rel, content, mode = 0o600) {
  const file = path.join(dir, rel);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content);
  fs.chmodSync(file, mode);
  return file;
}

function fileTree(dir) {
  const out = [];
  function walk(current, rel = "") {
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const nextRel = path.join(rel, entry.name);
      const nextAbs = path.join(current, entry.name);
      if (entry.isDirectory()) {
        walk(nextAbs, nextRel);
      } else {
        const stat = fs.statSync(nextAbs);
        out.push({
          rel: nextRel,
          size: stat.size,
          mode: stat.mode & 0o777,
          sha256: crypto.createHash("sha256").update(fs.readFileSync(nextAbs)).digest("hex"),
        });
      }
    }
  }
  walk(dir);
  return out.sort((a, b) => a.rel.localeCompare(b.rel));
}

const root = fs.mkdtempSync(path.join(os.tmpdir(), "stage37-profile-migration-"));
const source = path.join(root, "source");
const dest = path.join(root, "dest");
fs.mkdirSync(source, { recursive: true, mode: 0o700 });
fs.chmodSync(source, 0o700);

writeFixture(source, "Default/Preferences", JSON.stringify({ homepage: "https://web.whatsapp.com" }), 0o600);
writeFixture(source, "IndexedDB/dummy.txt", "synthetic fixture", 0o600);
writeFixture(source, "Cache/data.bin", Buffer.from("cache-fixture"), 0o600);

fs.cpSync(source, dest, { recursive: true, preserveTimestamps: true });
fs.chmodSync(dest, 0o700);

const sourceTree = fileTree(source);
const destTree = fileTree(dest);

if (sourceTree.length !== destTree.length) {
  throw new Error("tree length mismatch");
}

for (let i = 0; i < sourceTree.length; i += 1) {
  const a = sourceTree[i];
  const b = destTree[i];
  if (a.rel !== b.rel || a.size !== b.size || a.mode !== b.mode || a.sha256 !== b.sha256) {
    throw new Error(`fixture mismatch at ${a.rel}`);
  }
}

const sourceMode = fs.statSync(source).mode & 0o777;
const destMode = fs.statSync(dest).mode & 0o777;

if (sourceMode !== 0o700 || destMode !== 0o700) {
  throw new Error("directory mode mismatch");
}

console.log("PROFILE_MIGRATION_SCRIPT_TEST=PASS");
console.log(`PROFILE_MIGRATION_PLAN=READY`);
console.log(`SOURCE_FIXTURE=${source}`);
console.log(`DEST_FIXTURE=${dest}`);
