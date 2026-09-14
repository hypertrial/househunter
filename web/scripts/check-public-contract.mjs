import { readFile, readdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const webRoot = fileURLToPath(new URL("../", import.meta.url));
const roots = ["src", "e2e", "e2e-performance", "dist"];
const banned = ["mountain_score", "Mountain Score", "/api/v1", "mountain_min"];
const failures = [];

async function filesWithin(path) {
  const entries = await readdir(path, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const child = `${path}/${entry.name}`;
    if (entry.isDirectory()) files.push(...await filesWithin(child));
    else if (entry.isFile()) files.push(child);
  }
  return files;
}

for (const root of roots) {
  for (const path of await filesWithin(`${webRoot}${root}`)) {
    const content = await readFile(path, "utf8");
    const relative = path.slice(webRoot.length);
    for (const value of banned) {
      if (content.includes(value)) failures.push(`${relative}: contains ${JSON.stringify(value)}`);
    }
    for (let index = content.indexOf("/100"); index !== -1; index = content.indexOf("/100", index + 4)) {
      const compiledLibraryArithmetic = root === "dist" && /[\d\]]/.test(content[index - 1] || "");
      if (!compiledLibraryArithmetic) failures.push(`${relative}: contains legacy /100 presentation`);
    }
  }
}

if (failures.length) {
  throw new Error(`Legacy public frontend contract found:\n${failures.join("\n")}`);
}

console.log("Frontend public contract scan passed");
