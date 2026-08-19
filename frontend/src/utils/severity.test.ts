import { describe, expect, it } from 'vitest'
import { severityPill, severityRank } from './severity'

describe('severityRank', () => {
  it('ranks the planning reviewer vocabulary', () => {
    expect(severityRank('blocking')).toBe(3)
    expect(severityRank('major')).toBe(2)
    expect(severityRank('minor')).toBe(1)
  })

  it('ranks the risk and integration review vocabulary on the same rungs', () => {
    expect(severityRank('high')).toBe(severityRank('blocking'))
    expect(severityRank('medium')).toBe(severityRank('major'))
    expect(severityRank('low')).toBe(severityRank('minor'))
  })

  it('ranks an unknown word below every known one', () => {
    expect(severityRank('catastrophic')).toBe(0)
    expect(severityRank('')).toBe(0)
  })
})

describe('severityPill', () => {
  it('gives each rung its own colour, so the top rung is not the middle one', () => {
    expect(severityPill('blocking')).toBe('err')
    expect(severityPill('major')).toBe('warn')
    expect(severityPill('minor')).toBe('muted')
    expect(severityPill('blocking')).not.toBe(severityPill('minor'))
  })

  it('colours both vocabularies alike', () => {
    expect(severityPill('high')).toBe('err')
    expect(severityPill('medium')).toBe('warn')
    expect(severityPill('low')).toBe('muted')
  })

  it('leaves an unknown word unstyled rather than asserting a seriousness', () => {
    expect(severityPill('catastrophic')).toBe('')
  })
})
