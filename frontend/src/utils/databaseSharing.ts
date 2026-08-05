import type { DatabaseSharingState } from '../api/previews'

/**
 * One sentence naming the sandbox's database coupling.
 *
 * The coupling stays visible wherever the sandbox appears, because whoever
 * debugs a surprising migration later needs to know the database is shared.
 */
export function describeSharing(state: DatabaseSharingState): string {
  const guests = state.attached_project_names
  if (state.sharing === 'shared_data') {
    return `Guest on ${state.owner_project_name}'s data (schema ${state.schema_name}). Changes here reach that sandbox.`
  }
  if (state.sharing === 'shared_server') {
    const shared =
      guests.length > 0
        ? ` ${guests.length} guest sandbox(es) write to this data: ${guests.join(', ')}.`
        : ''
    return `Shared project server, own schema ${state.schema_name}.${shared}`
  }
  return `Own server, schema ${state.schema_name}.`
}
