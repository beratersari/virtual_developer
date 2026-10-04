/** Address of one temp-clone folder on Storage and Sessions. */
export function storageFolderPath(name: string): string {
  return `/storage/${encodeURIComponent(name)}`
}
