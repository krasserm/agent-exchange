/** The two path segments above the project directory itself, e.g.
 * `/Users/martin/Development/krasserm/dashboard` → `Development/krasserm`. */
export function parentSegments(path: string, count = 2): string {
  const parents = path.split("/").filter(Boolean).slice(0, -1);
  return parents.slice(-count).join("/") || "/";
}
