import { chromium } from 'playwright';

const URL = 'https://flasher.sigurdos.dev/';

async function run() {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();

  // Log ALL console messages including API responses
  page.on('console', msg => {
    console.log(`[${msg.type()}] ${msg.text().slice(0, 300)}`);
  });

  page.on('pageerror', err => {
    console.log(`[PAGE ERROR] ${err.message}`);
  });

  await page.goto(URL, { waitUntil: 'networkidle', timeout: 30000 });

  // Check what the API returns
  console.log('\n--- Checking API directly ---');
  const apiResponse = await page.evaluate(async () => {
    try {
      const resp = await fetch('/api/releases', { headers: { Accept: 'application/json' } });
      return { status: resp.status, ok: resp.ok, text: await resp.text().then(t => t.slice(0, 200)) };
    } catch (e) {
      return { error: e.message };
    }
  });
  console.log('API /api/releases:', JSON.stringify(apiResponse, null, 2));

  // Check the JS module loaded
  console.log('\n--- Checking JS state ---');
  const jsState = await page.evaluate(() => {
    return {
      hasFetchReleases: typeof fetchReleases !== 'undefined',
      hasInit: typeof init !== 'undefined',
      hasStartMonitor: typeof startMonitorConnect !== 'undefined',
      hasBeginCapture: typeof beginCaptureRead !== 'undefined',
      releaseDataExists: !!window.releaseData,
      releaseDataType: typeof window.releaseData,
    };
  }).catch(e => ({ error: `Cannot evaluate: ${e.message}` }));
  console.log('JS State:', JSON.stringify(jsState, null, 2));

  // Check what the fetchReleases function returns
  console.log('\n--- Checking release data ---');
  const releaseInfo = await page.evaluate(async () => {
    // Manually fetch
    try {
      const resp = await fetch('/api/releases', { headers: { Accept: 'application/json' } });
      if (!resp.ok) return { error: `HTTP ${resp.status}` };
      const data = await resp.json();
      return { count: data.length, first: data[0]?.tag_name, prerelease: data[0]?.prerelease };
    } catch (e) {
      return { error: e.message };
    }
  });
  console.log('Release info:', JSON.stringify(releaseInfo, null, 2));

  await browser.close();
}

run().catch(err => {
  console.error('Test crashed:', err);
  process.exit(1);
});
