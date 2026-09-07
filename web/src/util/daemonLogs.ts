/** Assistant / tool chatter that belongs on Transcript, not Daemon. */
const CHATTER = [
  '[codex] assistant:',
  '[codex] running:',
  '[codex] command',
  '[codex] thinking:',
  '[codex] files',
  '[codex] mcp:',
  '[codex] search:',
  '[codex] todos',
  '[codex] cwd=',
]

export function isDaemonChatter(message: string): boolean {
  const text = message || ''
  return CHATTER.some((needle) => text.includes(needle))
}
