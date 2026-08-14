import type { ManagedVolume } from '../api/volumes'

/** Set on the project volume itself. */
const LABEL_PROJECT_NAME = 'orchestrator.project.name'
/** Set on the project volume and on every preview volume of that sandbox. */
const LABEL_SANDBOX_ID = 'orchestrator.sandbox.id'
/** Set on shared-database volumes, which belong to a project, not a sandbox. */
const LABEL_PROJECT_ID = 'orchestrator.project.id'
const LABEL_PROJECT_SOURCE = 'orchestrator.project.source'

export interface VolumeGroup {
  key: string
  /** Project name when known, otherwise a short sandbox id. */
  title: string
  volumes: ManagedVolume[]
}

export interface GroupedVolumes {
  groups: VolumeGroup[]
  /** Volumes no sandbox owns: shared credentials, and anything unlabelled. */
  ungrouped: ManagedVolume[]
}

/**
 * Groups volumes by the sandbox that owns them.
 *
 * Ownership comes from the volume's own labels, never from what happens to be
 * mounted where. A shared credential volume is mounted into many sandboxes but
 * belongs to none, so it stays ungrouped.
 *
 */
function folderName(sourcePath: string): string {
  const parts = sourcePath.split('/').filter(Boolean)
  return parts.length > 0 ? parts[parts.length - 1] : ''
}

export function groupVolumesByProject(
  volumes: ManagedVolume[],
): GroupedVolumes {
  const groups = new Map<string, VolumeGroup>()
  const ungrouped: ManagedVolume[] = []

  for (const volume of volumes) {
    const labels = volume.labels ?? {}
    const sandboxId = labels[LABEL_SANDBOX_ID] ?? ''
    const projectName = labels[LABEL_PROJECT_NAME] || ''

    // A shared database serves every sandbox of a project, so it is grouped by
    // the project itself rather than by any one sandbox.
    const sharedProjectId = !sandboxId ? (labels[LABEL_PROJECT_ID] ?? '') : ''
    if (!sandboxId && !projectName && !sharedProjectId) {
      ungrouped.push(volume)
      continue
    }

    const key = sandboxId
      ? sandboxId
      : projectName
        ? `name:${projectName}`
        : `project:${sharedProjectId}`
    let group = groups.get(key)
    if (!group) {
      group = {
        key,
        title:
          projectName ||
          (sandboxId
            ? `Sandbox ${sandboxId.slice(0, 12)}`
            : `Shared: ${folderName(labels[LABEL_PROJECT_SOURCE] ?? '') || sharedProjectId.slice(0, 12)}`),
        volumes: [],
      }
      groups.set(key, group)
    }
    // A later volume can carry the name an earlier one lacked.
    group.volumes.push(volume)
  }

  // Named projects first, alphabetical; unnamed sandboxes after them.
  const ordered = [...groups.values()].sort((a, b) => {
    if (a.title.startsWith('Sandbox ') !== b.title.startsWith('Sandbox ')) {
      return a.title.startsWith('Sandbox ') ? 1 : -1
    }
    return a.title.localeCompare(b.title)
  })

  for (const group of ordered) {
    group.volumes.sort((a, b) => a.name.localeCompare(b.name))
  }
  ungrouped.sort((a, b) => a.name.localeCompare(b.name))

  return { groups: ordered, ungrouped }
}
