// TypeScript may reformat JSON assets when emitting; preserve canonical schema bytes.
const { copyFileSync } = require("node:fs");
const { join } = require("node:path");
copyFileSync(join(__dirname, "../src/_schema.json"), join(__dirname, "../dist/_schema.json"));
