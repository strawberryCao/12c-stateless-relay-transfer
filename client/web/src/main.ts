import { startApp } from './app.js';
import { runRoundtripSelftest } from './roundtrip-selftest.js';

function installWebAppMetadata(): void {
  if (!document.querySelector('link[rel="manifest"]')) {
    const manifest = document.createElement('link');
    manifest.rel = 'manifest';
    manifest.href = '/manifest.webmanifest';
    document.head.appendChild(manifest);
  }

  let appleCapable = document.querySelector<HTMLMetaElement>(
    'meta[name="apple-mobile-web-app-capable"]',
  );
  if (!appleCapable) {
    appleCapable = document.createElement('meta');
    appleCapable.name = 'apple-mobile-web-app-capable';
    document.head.appendChild(appleCapable);
  }
  appleCapable.content = 'yes';
}

installWebAppMetadata();

if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    void navigator.serviceWorker.register('/service-worker.js').catch((error: unknown) => {
      console.warn('service worker registration failed', error);
    });
  });
}

const selftest = new URLSearchParams(location.search).get('selftest');
if (selftest === 'roundtrip') {
  void runRoundtripSelftest();
} else {
  void startApp();
}
