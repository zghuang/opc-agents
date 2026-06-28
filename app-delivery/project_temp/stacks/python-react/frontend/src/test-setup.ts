import '@testing-library/jest-dom/vitest'

// Ant Design requires matchMedia in test environments
Object.defineProperty(window, 'matchMedia', {
	writable: true,
	value: (q: string) => ({
		matches: false,
		media: q,
		onchange: null,
		addListener: () => {},
		removeListener: () => {},
		addEventListener: () => {},
		removeEventListener: () => {},
		dispatchEvent: () => false,
	}),
})

// jsdom doesn't implement scrollTo
Object.defineProperty(window, 'scrollTo', {
	writable: true,
	value: () => {},
})