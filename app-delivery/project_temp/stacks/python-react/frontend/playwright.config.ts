import process from 'node:process'
import { defineConfig, devices } from '@playwright/test'

// Host is pinned to 127.0.0.1 to match opc-runtime.py, which binds the dev
// servers to 127.0.0.1. On macOS `localhost` can resolve to ::1 and silently
// hit a different (or stale) listener, producing false-positive browser QA.
const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:5173'
const BACKEND_COMMAND = (process.env.E2E_BACKEND_CMD || '').trim()
const BACKEND_HEALTHCHECK_URL = process.env.E2E_BACKEND_HEALTHCHECK_URL || 'http://127.0.0.1:8000/health'

const frontendWebServer = {
  command: `VITE_API_PROXY_TARGET=${process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000'} npm run dev -- --host 127.0.0.1 --port 5173 --strictPort`,
  url: BASE_URL,
  reuseExistingServer: !process.env.CI,
  timeout: 120_000,
}

const backendWebServer = BACKEND_COMMAND
  ? {
      command: BACKEND_COMMAND,
      url: BACKEND_HEALTHCHECK_URL,
      reuseExistingServer: false,
      timeout: 120_000,
    }
  : null

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  // `list` keeps automated runs non-blocking; the HTML report is written but
  // never auto-served (auto-serving hangs headless verify runs).
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: BASE_URL,
    headless: true,
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: backendWebServer ? [backendWebServer, frontendWebServer] : frontendWebServer,
})
