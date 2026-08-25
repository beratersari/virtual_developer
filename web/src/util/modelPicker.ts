/** Sentinel for the "type an id" option in model <select>s. */
export const CUSTOM_MODEL = '__custom__'

export function modelSelectValue(
  value: string,
  knownIds: Iterable<string>,
  custom: boolean,
): string {
  const known = knownIds instanceof Set ? knownIds : new Set(knownIds)
  // A listed id always wins. Otherwise Settings "required" + a real
  // dropdown choice still looked like Other id and kept the text field.
  if (value && known.has(value)) return value
  if (custom || (value && !known.has(value))) return CUSTOM_MODEL
  return ''
}

/** True only when the dropdown is on Other id…. Never for a listed model. */
export function showCustomModelId(selectValue: string): boolean {
  return selectValue === CUSTOM_MODEL
}
