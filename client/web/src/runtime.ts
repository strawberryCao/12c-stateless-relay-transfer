import {
  createRelayStackFromConfig,
  loadTransferConfigFromUrl,
  loadTwelveC,
  resolveRegistryBaseUrl,
  type LoadTwelveCOptions,
  type RelayStack,
  type TwelveCClient,
  type TransferConfig,
} from '@stateless-relay/transfer';
import { DEFAULT_FILE_TTL_SECONDS } from './file-ttl.js';

const REGISTRY_URL_STORAGE_KEY = 'stateless-relay.registryUrl';
const FILE_TTL_SECONDS_STORAGE_KEY = 'stateless-relay.fileTtlSeconds';
const WASM_JS_URL = './wasm/twelve_c_cryptography.js';
const WASM_BINARY_URL = './wasm/twelve_c_cryptography.wasm';

export interface ClientRuntime {
  twelveC: TwelveCClient;
  stack: RelayStack;
  registryUrl: string;
}

type CreateTwelveCModuleFactory = NonNullable<LoadTwelveCOptions['createModule']>;

let createTwelveCModulePromise: Promise<CreateTwelveCModuleFactory> | null = null;

function readGlobalCreateTwelveCModule(): CreateTwelveCModuleFactory | null {
  const factory = (globalThis as { createTwelveCModule?: unknown }).createTwelveCModule;
  return typeof factory === 'function'
    ? (factory as CreateTwelveCModuleFactory)
    : null;
}

async function loadCreateTwelveCModule(): Promise<CreateTwelveCModuleFactory> {
  const existing = readGlobalCreateTwelveCModule();
  if (existing) {
    return existing;
  }

  if (!createTwelveCModulePromise) {
    createTwelveCModulePromise = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = WASM_JS_URL;
      script.async = true;
      script.onload = () => {
        const factory = readGlobalCreateTwelveCModule();
        if (!factory) {
          reject(new Error(`global createTwelveCModule missing after loading ${WASM_JS_URL}`));
          return;
        }
        resolve(factory);
      };
      script.onerror = () => {
        reject(new Error(`failed to load ${WASM_JS_URL}`));
      };
      document.head.appendChild(script);
    });
  }

  return createTwelveCModulePromise;
}

export function readStoredRegistryUrl(): string | null {
  const value = localStorage.getItem(REGISTRY_URL_STORAGE_KEY);
  if (!value || !value.trim()) {
    return null;
  }
  return value.trim();
}

export function saveStoredRegistryUrl(url: string): void {
  localStorage.setItem(REGISTRY_URL_STORAGE_KEY, url.trim());
}

export function clearStoredRegistryUrl(): void {
  localStorage.removeItem(REGISTRY_URL_STORAGE_KEY);
}

export function readStoredFileTtlSeconds(): number | null {
  const raw = localStorage.getItem(FILE_TTL_SECONDS_STORAGE_KEY);
  if (raw === null || raw.trim() === '') {
    return null;
  }
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed) || parsed < 0) {
    return null;
  }
  return Math.min(parsed, 24 * 60 * 60);
}

export function saveStoredFileTtlSeconds(seconds: number): void {
  localStorage.setItem(FILE_TTL_SECONDS_STORAGE_KEY, String(Math.max(0, Math.trunc(seconds))));
}

export function clearStoredFileTtlSeconds(): void {
  localStorage.removeItem(FILE_TTL_SECONDS_STORAGE_KEY);
}

export function getEffectiveFileTtlSeconds(): number {
  return readStoredFileTtlSeconds() ?? DEFAULT_FILE_TTL_SECONDS;
}

export async function loadEffectiveConfig(): Promise<TransferConfig> {
  const config = await loadTransferConfigFromUrl('./relay.config.json');
  const storedUrl = readStoredRegistryUrl();
  if (storedUrl) {
    config.registry.url = storedUrl;
  }
  return config;
}

export async function loadTwelveCClient(): Promise<TwelveCClient> {
  return loadTwelveC({
    createModule: await loadCreateTwelveCModule(),
    wasmUrl: WASM_BINARY_URL,
  });
}

export interface RoundtripCaseResult {
  size: number;
  ok: boolean;
  numTokens?: number;
  wireBlockSize?: number;
  error?: string;
}

/** 纯 WASM 加解密 roundtrip（不依赖 Registry / Relay）。 */
export async function runCryptoRoundtrip(
  sizes: readonly number[],
  credential = 'ABCDEF123456',
): Promise<RoundtripCaseResult[]> {
  const twelveC = await loadTwelveCClient();
  const results: RoundtripCaseResult[] = [];

  for (const size of sizes) {
    try {
      const plaintext = new Uint8Array(size);
      for (let i = 0; i < size; i++) {
        plaintext[i] = i & 0xff;
      }

      const uploads = twelveC.prepareUpload(plaintext, credential);
      const recovered = twelveC.receiveFromUploadMap(credential, uploads);

      if (recovered.length !== size) {
        throw new Error(`length ${recovered.length} != ${size}`);
      }
      for (let i = 0; i < size; i++) {
        if (recovered[i] !== plaintext[i]) {
          throw new Error(`byte mismatch at ${i}`);
        }
      }

      const token0 = uploads.values().next().value;
      if (!token0) {
        throw new Error('prepareUpload returned empty map');
      }
      const meta = twelveC.parseSmbEncrypted(credential, token0);
      results.push({
        size,
        ok: true,
        numTokens: meta.numTokens,
        wireBlockSize: meta.wireBlockSize,
      });
    } catch (error) {
      results.push({
        size,
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }

  return results;
}

export async function createClientRuntime(
  config: TransferConfig,
): Promise<ClientRuntime> {
  const twelveC = await loadTwelveCClient();
  const stack = createRelayStackFromConfig(config);
  return {
    twelveC,
    stack,
    registryUrl: resolveRegistryBaseUrl(config.registry),
  };
}

export async function bootstrapClientRuntime(): Promise<ClientRuntime> {
  const config = await loadEffectiveConfig();
  return createClientRuntime(config);
}
