/** A single-series meter, capped at 100% so an over-limit value stays in bounds. */
function Meter({ percent, label }: { percent: number; label: string }) {
  const clamped = Math.min(Math.max(percent, 0), 100)
  return (
    <div className="meter" role="img" aria-label={label} title={label}>
      <span className="meter-fill" style={{ width: `${clamped}%` }} />
    </div>
  )
}

export default Meter
