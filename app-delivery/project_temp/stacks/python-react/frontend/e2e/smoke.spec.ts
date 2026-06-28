import { test, expect } from '@playwright/test'

test('app loads without console errors', async ({ page }) => {
  const consoleErrors: string[] = []
  page.on('console', msg => {
    if (msg.type() === 'error') consoleErrors.push(msg.text())
  })

  await page.goto('/')
  await expect(page.locator('body')).toBeVisible()
  expect(consoleErrors, `console errors on /: ${consoleErrors.join('\n')}`).toHaveLength(0)
})

test('root route renders without crashing', async ({ page }) => {
  await page.goto('/')
  await expect(page).not.toHaveTitle(/error/i)
  await expect(page.locator('#root')).toBeVisible()
})
