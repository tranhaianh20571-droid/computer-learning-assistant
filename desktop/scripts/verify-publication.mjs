import fs from "node:fs/promises";
import path from "node:path";
import { execFileSync } from "node:child_process";

const files = execFileSync("git", ["ls-files", "-z"], { encoding: "utf8" }).split("\0").filter(Boolean);
const failures = [];
for (const file of files) {
  if (file.startsWith("文档/") || /(^|\/)(?:\.env(?:\..*)?|node_modules|\.venv|\.runtime|\.backup|storage|asr-models)(\/|$)/.test(file) &&
      !file.endsWith(".env.example") || /\.(?:dpapi|pem|pfx|private\.zip|log)$/.test(file)) {
    failures.push(`${file}: private or generated path`);
    continue;
  }
  const stat = await fs.stat(file).catch(() => null);
  if (!stat) continue;
  if (stat.size > 40 * 1024 * 1024) failures.push(`${file}: unexpected large source file`);
  if (!/\.(?:py|mjs|cjs|js|ts|tsx|json|toml|ya?ml|md|txt|ps1|sh|css|html)$/.test(file)) continue;
  let text = await fs.readFile(file, "utf8");
  // Exact, public synthetic fixtures are the only credential-pattern exceptions.
  if (file.startsWith("tests/")) text = text.replaceAll("sk-test-" + "fake-key-000000000000", "SYNTHETIC_TEST_KEY");
  if (file === "deploy/langfuse-compose.yml")
    text = text.replaceAll("lf-sk-dev-" + "fedcba0987654321abcdefabcdef1234", "PUBLIC_DEVELOPMENT_EXAMPLE");
  const secrets = [
    /\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{24,}\b/,
    /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/,
    /(?:api[_-]?key|secret[_-]?key|password)\s*[:=]\s*["'](?:sk-|ghp_|github_pat_)[^"']{16,}/i
  ];
  if (secrets.some(pattern => pattern.test(text))) failures.push(`${file}: possible credential`);
}
if (failures.length) { console.error(failures.join("\n")); process.exit(1); }
console.log(`Publication paths and credential patterns checked: ${files.length} files.`);
