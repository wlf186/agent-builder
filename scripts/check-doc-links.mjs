import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';

const root = process.cwd();
const roots = [
  'CLAUDE.md',
  'README.md',
  'CONTRIBUTING.md',
  'SECURITY.md',
  'frontend/README.md',
  'docs',
  'docs-site',
];
const markdownFiles = [];
const ignoredPrefixes = [
  'docs/superpowers',
  'docs-site/node_modules',
  'docs-site/.vitepress/cache',
  'docs-site/.vitepress/dist',
];

function walk(entry) {
  if (ignoredPrefixes.some(prefix => entry === prefix || entry.startsWith(`${prefix}/`))) return;
  const absolute = path.join(root, entry);
  if (!fs.existsSync(absolute)) return;
  const stat = fs.statSync(absolute);
  if (stat.isDirectory()) {
    for (const child of fs.readdirSync(absolute).sort()) walk(path.join(entry, child));
  } else if (entry.endsWith('.md')) {
    markdownFiles.push(entry);
  }
}

for (const entry of roots) walk(entry);

function candidatesFor(source, rawTarget) {
  const target = decodeURIComponent(rawTarget.split('#', 1)[0].split('?', 1)[0]);
  if (!target) return [];
  if (/^(?:https?:|mailto:|tel:|data:)/i.test(target)) return [];

  let absolute;
  if (target.startsWith('/en/') || target.startsWith('/zh/')) {
    absolute = path.join(root, 'docs-site', target.slice(1));
  } else if (target === '/en/' || target === '/zh/') {
    absolute = path.join(root, 'docs-site', target.slice(1), 'index');
  } else if (target.startsWith('/docs/')) {
    absolute = path.join(root, 'docs', target.slice('/docs/'.length));
  } else if (target.startsWith('/')) {
    return []; // Application routes are not filesystem documentation links.
  } else {
    absolute = path.resolve(root, path.dirname(source), target);
  }
  return [absolute, `${absolute}.md`, path.join(absolute, 'index.md')];
}

const failures = [];
const linkPattern = /!?(?:\[[^\]]*\])\(([^)\s]+)(?:\s+["'][^"']*["'])?\)/g;
for (const source of markdownFiles) {
  const content = fs.readFileSync(path.join(root, source), 'utf8');
  for (const match of content.matchAll(linkPattern)) {
    const candidates = candidatesFor(source, match[1].replace(/^<|>$/g, ''));
    if (candidates.length && !candidates.some(candidate => fs.existsSync(candidate))) {
      failures.push(`${source}: ${match[1]}`);
    }
  }
}

if (failures.length) {
  console.error(`Broken local documentation links (${failures.length}):`);
  for (const failure of failures) console.error(`  ${failure}`);
  process.exit(1);
}
console.log(`Checked ${markdownFiles.length} Markdown files: local links are valid.`);
