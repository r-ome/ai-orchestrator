import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

// Proves the harness works: JSX compiles, jsdom renders, and the
// jest-dom matchers are registered.
describe('test harness', () => {
  it('renders a component and matches on the DOM', () => {
    render(<p>orchestrator</p>)
    expect(screen.getByText('orchestrator')).toBeInTheDocument()
  })
})
