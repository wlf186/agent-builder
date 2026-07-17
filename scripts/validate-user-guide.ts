// scripts/validate-user-guide.ts
import * as fs from 'fs';
import * as path from 'path';
import { glob } from 'glob';

interface ValidationResult {
  component: string;
  valid: boolean;
  errors: string[];
}

function validateUserGuide(source: string, componentName: string): ValidationResult {
  const errors: string[] = [];

  // Check for @userGuide tag
  const jsdocRegex = /\/\*\*[\s\S]*?@userGuide[\s\S]*?\*\//;
  const match = source.match(jsdocRegex);

  if (!match) {
    // Not all components need user guides - only validate if tag exists
    return { component: componentName, valid: true, errors: [] };
  }

  const block = match[0];

  const hasTag = (tag: string): boolean => {
    const regex = new RegExp(`@${tag}\\s+`);
    return regex.test(block);
  };

  // Required tags
  const requiredTags = [
    'title.en', 'title.zh',
    'category',
    'description.en', 'description.zh',
  ];

  for (const tag of requiredTags) {
    if (!hasTag(tag)) {
      errors.push(`Missing required tag: @${tag}`);
    }
  }

  const requiredValues = ['title.en', 'title.zh', 'description.en', 'description.zh'];
  for (const tag of requiredValues) {
    const valueMatch = block.match(new RegExp(`@${tag}\\s+(.+?)(?=@\\w+|\\*\\/)`, 's'));
    const value = valueMatch?.[1].replace(/\s*\*\s*/g, ' ').trim() || '';
    if (hasTag(tag) && !value) {
      errors.push(`Empty required tag: @${tag}`);
    }
  }

  // Validate category value
  const categoryMatch = block.match(/@category\s+(core|advanced|reference)/);
  if (!categoryMatch) {
    if (hasTag('category')) {
      errors.push('Invalid @category value. Must be: core, advanced, or reference');
    }
  }

  // Validate steps have both languages if present
  const hasStepsEn = hasTag('steps.en');
  const hasStepsZh = hasTag('steps.zh');
  if (hasStepsEn !== hasStepsZh) {
    errors.push('Steps must have both .en and .zh versions');
  }

  // Validate tips have both languages if present
  const hasTipsEn = hasTag('tips.en');
  const hasTipsZh = hasTag('tips.zh');
  if (hasTipsEn !== hasTipsZh) {
    errors.push('Tips must have both .en and .zh versions');
  }

  return {
    component: componentName,
    valid: errors.length === 0,
    errors,
  };
}

async function validateAllUserGuides() {
  const componentsDir = path.resolve(__dirname, '../frontend/src/components');

  const files = (await glob('**/*.tsx', { cwd: componentsDir })).sort();
  console.log(`Validating ${files.length} component files...\n`);

  const results: ValidationResult[] = [];
  let validCount = 0;
  let errorCount = 0;

  for (const file of files) {
    const filePath = path.join(componentsDir, file);
    const source = fs.readFileSync(filePath, 'utf-8');
    const componentName = path.basename(file, '.tsx');

    const result = validateUserGuide(source, componentName);
    results.push(result);

    if (result.valid) {
      validCount++;
    } else {
      errorCount++;
      console.log(`❌ ${componentName}:`);
      for (const error of result.errors) {
        console.log(`   - ${error}`);
      }
    }
  }

  console.log(`\n${'='.repeat(50)}`);
  console.log(`Validation complete: ${validCount} valid, ${errorCount} with errors`);

  if (errorCount > 0) {
    process.exit(1);
  }
}

validateAllUserGuides().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
