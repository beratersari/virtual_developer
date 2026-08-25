/** Sentinel for the "type an id" option in model <select>s. */
export const CUSTOM_MODEL = '__custom__'

export function modelSelectValue(
  value: string,
  knownIds: Iterable<string>,
  custom: boolean,
): string {
  const known = knownIds instanceof Set ? knownIds : new Set(knownIds)
  if (custom || (value && !known.has(value))) return CUSTOM_MODEL
  if (value && known.has(value)) return value
  return ''
}

/** Model-id text field is only for "Other id…". Listed models hide it. */
export function showCustomModelId(selectValue: string, custom: boolean): boolean {
  return Boolean(custom) || selectValue === CUSTOM_MODEL
}
