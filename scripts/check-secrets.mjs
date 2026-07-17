import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import process from 'node:process';

const patterns = [
  ['GitHub token', /\bgh[pousr]_[A-Za-z0-9]{30,}\b/g],
  ['GitHub fine-grained token', /\bgithub_pat_[A-Za-z0-9_]{50,}\b/g],
  ['AWS access key', /\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/g],
  ['GitLab token', /\bglpat-[A-Za-z0-9_-]{20,}\b/g],
  ['Slack token', /\bxox[baprs]-[A-Za-z0-9-]{20,}\b/g],
  ['npm token', /\bnpm_[A-Za-z0-9]{36}\b/g],
  ['Google API key', /\bAIza[A-Za-z0-9_-]{35}\b/g],
  ['Anthropic API key', /\bsk-ant-[A-Za-z0-9_-]{40,}\b/g],
  ['provider API key', /\bsk-(?:live|test|proj)-[A-Za-z0-9_-]{16,}\b/g],
  ['generic provider API key', /\bsk-[A-Za-z0-9]{32,}\b/g],
  ['private key', /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/g],
  ['credential in URL', /https?:\/\/[^\s/@:]+:[^\s/@]+@/g],
];

const files = execFileSync('git', ['ls-files', '-co', '--exclude-standard', '-z'])
  .toString('utf8')
  .split('\0')
  .filter(Boolean);
const failures = [];
for (const file of files) {
  let content;
  try {
    const stat = fs.statSync(file);
    if (!stat.isFile() || stat.size > 5_000_000) continue;
    content = fs.readFileSync(file, 'utf8');
  } catch {
    continue;
  }
  if (content.includes('\0')) continue;
  for (const [label, pattern] of patterns) {
    pattern.lastIndex = 0;
    if (pattern.test(content)) failures.push(`${file}: ${label}`);
  }
}

if (failures.length) {
  console.error('Potential committed secrets found:');
  for (const failure of failures) console.error(`  ${failure}`);
  process.exit(1);
}
console.log(`Scanned ${files.length} tracked/unignored files: no credential patterns found.`);
