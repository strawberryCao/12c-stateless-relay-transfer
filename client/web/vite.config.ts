import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';

const root = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig(({ mode }) => {
  const isProduction = mode === 'production';

  return {
    server: {
      port: 5173,
      open: true,
    },
    resolve: {
      alias: {
        '@stateless-relay/transfer': path.resolve(root, '../transfer/src/index.ts'),
        '@stateless-relay/app': path.resolve(root, '../app/src/index.ts'),
      },
    },
    build: {
      target: 'es2020',
      sourcemap: isProduction ? false : true,
      minify: isProduction ? 'esbuild' : false,
      cssMinify: isProduction,
      reportCompressedSize: false,
      rollupOptions: {
        output: {
          manualChunks: {
            wasm: ['@stateless-relay/transfer'],
            app: ['@stateless-relay/app'],
          },
          assetFileNames: 'assets/[name].[hash][extname]',
          chunkFileNames: 'js/[name].[hash].js',
          entryFileNames: 'js/[name].[hash].js',
        },
      },
    },
    css: {
      devSourcemap: true,
    },
  };
});