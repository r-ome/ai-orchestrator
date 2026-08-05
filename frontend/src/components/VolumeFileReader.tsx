import { useState } from 'react'
import { fetchVolumeFile, type VolumeAttachment } from '../api/volumes'
import FileReader from './FileReader'

interface VolumeFileReaderProps {
  volumeName: string
  attachments: VolumeAttachment[]
}

/**
 * Reads one file out of a volume. The backend needs a running container to
 * read through, so the picker only offers running attachments.
 */
function VolumeFileReader({ volumeName, attachments }: VolumeFileReaderProps) {
  const runningAttachments = attachments.filter(
    (attachment) => attachment.container_status === 'running',
  )
  const [containerId, setContainerId] = useState('')

  if (runningAttachments.length === 0) {
    return (
      <p className="status">
        File access needs a running container attached to this volume. None is
        running.
      </p>
    )
  }

  return (
    <FileReader
      placeholder="config/settings.json"
      hint="Paths are relative to the volume root. Max 1 MiB."
      controls={
        <label>
          Read through
          <select
            value={containerId}
            onChange={(event) => setContainerId(event.target.value)}
          >
            <option value="">Any running container</option>
            {runningAttachments.map((attachment) => (
              <option
                key={attachment.container_id}
                value={attachment.container_id}
              >
                {attachment.container_name}
              </option>
            ))}
          </select>
        </label>
      }
      onRead={async (path) => {
        const response = await fetchVolumeFile(volumeName, path, {
          containerId: containerId || undefined,
        })
        return {
          file: response.file,
          resolvedPath: response.container_path,
          via: response.container_name,
        }
      }}
    />
  )
}

export default VolumeFileReader
