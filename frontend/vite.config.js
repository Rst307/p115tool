import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import federation from '@originjs/vite-plugin-federation'

export default defineConfig({
  base: './',
  plugins: [vue(), federation({
    name: 'P115Tool', filename: 'remoteEntry.js',
    exposes: {'./Page': './src/Page.vue', './Config': './src/Config.vue'},
    shared: {vue: {requiredVersion: false, generate: false}},
  })],
  build: {target: 'esnext', outDir: 'dist', emptyOutDir: false, cssCodeSplit: true},
})
