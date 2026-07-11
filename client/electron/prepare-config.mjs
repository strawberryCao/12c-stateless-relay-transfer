import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const electronDir = path.dirname(fileURLToPath(import.meta.url));
const outputPath = path.join(electronDir, 'app.config.json');
const rawUrl = process.env.TWELVEC_APP_URL?.trim();

if (!rawUrl) {
  throw new Error('TWELVEC_APP_URL is required, for example https://app.example.com');
}

const appUrl = new URL(rawUrl);
if (appUrl.protocol !== 'https:') {
  throw new Error('TWELVEC_APP_URL must use HTTPS');
}
if (appUrl.username || appUrl.password) {
  throw new Error('TWELVEC_APP_URL must not contain credentials');
}
appUrl.hash = '';

await writeFile(
  outputPath,
  `${JSON.stringify({ appUrl: appUrl.toString() }, null, 2)}\n`,
  { encoding: 'utf8', mode: 0o600 },
);

console.log(`Electron production URL: ${appUrl.origin}`);
console.log(`Wrote ${outputPath}`);
