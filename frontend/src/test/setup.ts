// Registers the jest-dom matchers (toBeInTheDocument, toHaveTextContent, ...)
// on Vitest's expect, and clears the rendered DOM between tests.
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(() => {
  cleanup()
})
