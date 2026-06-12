import { chromium } from 'playwright';

const URL = 'https://flasher.sigurdos.dev/';

async function run() {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();

  const jsErrors = [];
  page.on('pageerror', err => jsErrors.push(err.message));
  page.on('console', msg => {
    // Log our app's console output specifically
    if (msg.type() === 'log' || msg.type() === 'error') {
      console.log(`[${msg.type()}] ${msg.text().slice(0, 300)}`);
    }
  });

  await page.goto(URL, { waitUntil: 'networkidle', timeout: 30000 });

  console.log('\n=== Checking runtime state ===');

  // Check if buttons exist and have event listeners attached
  const buttonState = await page.evaluate(() => {
    const connectBtn = document.getElementById('btn-connect');
    const startMonitorBtn = document.getElementById('btn-start-monitor');
    const debugCard = document.getElementById('channel-debug');
    const captureStep = document.getElementById('step-capture');
    const flashBtn = document.getElementById('btn-flash');

    return {
      connectBtn: !!connectBtn,
      connectBtnDisabled: connectBtn?.disabled ?? 'N/A',
      startMonitorBtn: !!startMonitorBtn,
      startMonitorBtnDisabled: startMonitorBtn?.disabled ?? 'N/A',
      debugCard: !!debugCard,
      captureStep: !!captureStep,
      captureStepDisplay: captureStep?.style?.display ?? 'N/A',
      flashBtn: !!flashBtn,
      flashBtnDisabled: flashBtn?.disabled ?? 'N/A',
      // Check that the JS ran by looking for modified text (from fetchReleases)
      betaVersion: document.getElementById('beta-version')?.textContent,
      stableVersion: document.getElementById('stable-version')?.textContent,
    };
  });
  console.log('Button state:', JSON.stringify(buttonState, null, 2));

  // Simulate clicking Debug channel
  console.log('\n=== Clicking Debug channel ===');
  await page.locator('#channel-debug').click();
  await page.waitForTimeout(500);

  const afterClick = await page.evaluate(() => {
    const debugCard = document.getElementById('channel-debug');
    const flashBtn = document.getElementById('btn-flash');
    return {
      debugSelected: debugCard?.classList.contains('channel-card--selected'),
      flashBtnText: flashBtn?.textContent,
      flashBtnDisabled: flashBtn?.disabled,
    };
  });
  console.log('After click:', JSON.stringify(afterClick, null, 2));

  // Check the download card
  console.log('\n=== Debug download card ===');
  const dlCard = await page.evaluate(() => {
    const dlDebug = document.querySelector('.fd-channel--debug');
    if (!dlDebug) return { exists: false };
    const badge = dlDebug.querySelector('.fd-badge')?.textContent;
    const links = dlDebug.querySelectorAll('a[href]');
    return {
      exists: true,
      badge,
      links: Array.from(links).map(a => ({ href: a.getAttribute('href'), text: a.textContent.trim().slice(0, 60) })),
    };
  });
  console.log('Download card:', JSON.stringify(dlCard, null, 2));

  // Check for console errors specifically from our code
  console.log(`\n=== JS Errors from our code: ${jsErrors.length} ===`);
  for (const e of jsErrors) console.log(`  ✗ ${e}`);

  await browser.close();
  console.log(`\n✅ Test complete — ${jsErrors.length} JS errors`);
}

run().catch(err => {
  console.error('Test crashed:', err);
  process.exit(1);
});
