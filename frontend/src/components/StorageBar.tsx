interface StorageBarProps {
  /** Bytes still held by active objects. */
  inUseBytes: number
  /** Bytes Docker can free with a prune. */
  reclaimableBytes: number
  /** Largest total across all rows — every bar shares this scale. */
  scaleBytes: number
  inUseLabel: string
  reclaimableLabel: string
}

/**
 * A stacked horizontal bar: in-use bytes then reclaimable bytes, both scaled
 * against the largest row so bar lengths compare across categories.
 */
function StorageBar({
  inUseBytes,
  reclaimableBytes,
  scaleBytes,
  inUseLabel,
  reclaimableLabel,
}: StorageBarProps) {
  const scale = scaleBytes > 0 ? scaleBytes : 1
  const inUsePercent = (inUseBytes / scale) * 100
  const reclaimablePercent = (reclaimableBytes / scale) * 100

  return (
    <div
      className="storage-bar"
      role="img"
      aria-label={`${inUseLabel} in use, ${reclaimableLabel} reclaimable`}
    >
      {inUsePercent > 0 && (
        <span
          className="storage-bar-segment in-use"
          style={{ width: `${inUsePercent}%` }}
          title={`In use: ${inUseLabel}`}
        />
      )}
      {reclaimablePercent > 0 && (
        <span
          className="storage-bar-segment reclaimable"
          style={{ width: `${reclaimablePercent}%` }}
          title={`Reclaimable: ${reclaimableLabel}`}
        />
      )}
    </div>
  )
}

export default StorageBar
