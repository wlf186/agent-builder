import { test } from '@playwright/test';
import { testOutputDir } from './test-paths';

const SCREENSHOT_DIR = testOutputDir('demo-docs-nav');

test('Navigate docs site and click hero buttons', async ({ page }) => {
  // Go directly to /en/ page
  await page.goto('http://localhost:5173/en/');

  // Wait for page to load
  await page.waitForTimeout(2000);

  // Take screenshot of home page with hero section
  await page.screenshot({ path: `${SCREENSHOT_DIR}/hero-01-home.png`, fullPage: true });
  console.log('Screenshot 1: Home page with hero section');

  // Click the first hero button "Get Started"
  const getStartedBtn = page.locator('a:has-text("Get Started")').first();
  console.log('Clicking "Get Started" button...');
  await getStartedBtn.click();
  await page.waitForTimeout(2000);
  await page.screenshot({ path: `${SCREENSHOT_DIR}/hero-02-get-started.png`, fullPage: true });
  console.log('Screenshot 2: After clicking Get Started');
  console.log('Current URL:', page.url());

  // Go back to home page
  await page.goto('http://localhost:5173/en/');
  await page.waitForTimeout(2000);

  // Click the second hero button "Core Features"
  const coreFeaturesBtn = page.locator('a:has-text("Core Features")').first();
  console.log('Clicking "Core Features" button...');
  await coreFeaturesBtn.click();
  await page.waitForTimeout(2000);
  await page.screenshot({ path: `${SCREENSHOT_DIR}/hero-03-core-features.png`, fullPage: true });
  console.log('Screenshot 3: After clicking Core Features');
  console.log('Current URL:', page.url());

  // Wait to observe
  await page.waitForTimeout(3000);
});
