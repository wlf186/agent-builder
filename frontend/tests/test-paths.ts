import fs from 'node:fs';
import path from 'node:path';

function findProjectRoot(start: string): string {
  let current = path.resolve(start);

  while (true) {
    if (
      fs.existsSync(path.join(current, 'CLAUDE.md')) &&
      fs.existsSync(path.join(current, 'frontend', 'package.json'))
    ) {
      return current;
    }

    const parent = path.dirname(current);
    if (parent === current) {
      throw new Error(`Unable to locate the Agent Builder project root from ${start}`);
    }
    current = parent;
  }
}

export const PROJECT_ROOT = findProjectRoot(process.cwd());
export const TEST_RESULTS_ROOT = path.join(PROJECT_ROOT, '.runtime', 'test-results');

export function testOutputDir(suite: string): string {
  const outputDir = path.join(TEST_RESULTS_ROOT, suite);
  fs.mkdirSync(outputDir, { recursive: true });
  return outputDir;
}

export function testOutputPath(suite: string, ...parts: string[]): string {
  const outputPath = path.join(TEST_RESULTS_ROOT, suite, ...parts);
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  return outputPath;
}

export const AC130_FIXTURE_DIR = path.join(
  PROJECT_ROOT,
  'teams',
  'AC130',
  'iterations',
  '202603170949',
  'test_documents',
);

export const AC130_PDF_FIXTURE = path.join(
  AC130_FIXTURE_DIR,
  'Cyberpunk公司2026员工手册.pdf',
);
