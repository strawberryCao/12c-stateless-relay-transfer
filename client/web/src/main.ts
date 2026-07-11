import { startApp } from './app.js';
import { installQrShareFeature } from './qr-share.js';
import { runRoundtripSelftest } from './roundtrip-selftest.js';

async function startInteractiveApp(): Promise<void> {
  await startApp();
  try {
    installQrShareFeature();
  } catch (error) {
    console.warn('QR share feature initialization failed', error);
  }
}

const selftest = new URLSearchParams(location.search).get('selftest');
if (selftest === 'roundtrip') {
  void runRoundtripSelftest();
} else {
  void startInteractiveApp();
}
