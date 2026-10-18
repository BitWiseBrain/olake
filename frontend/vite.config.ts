import { defineConfig } from 'vite'
import path from 'path'

export default defineConfig({
  build: {
    lib: {
      entry: path.resolve(__dirname, 'src/ask-olake.ts'),
      name: 'AskOlake',
      fileName: 'ask-olake',
      formats: ['iife'],
    },
    rollupOptions: {
      output: {
        assetFileNames: 'ask-olake.[ext]',
      },
    },
    outDir: 'dist',
    minify: 'esbuild',
  },
})
