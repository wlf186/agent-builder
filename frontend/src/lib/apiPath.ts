type PathSegment = string | number;

function buildPath(prefix: string, segments: readonly PathSegment[]): string {
  return `${prefix}/${segments.map((segment) => {
    const value = String(segment);
    // URL parsers normalize dot-only segments even when the dots are percent
    // encoded, so they cannot be represented safely as one dynamic segment.
    if (value === '.' || value === '..') {
      throw new TypeError('Dot-only URL path segments are not allowed');
    }
    return encodeURIComponent(value);
  }).join('/')}`;
}

/** Build a same-origin API URL while encoding every path segment exactly once. */
export function apiPath(...segments: PathSegment[]): string {
  return buildPath('/api', segments);
}

/** Build a same-origin streaming-proxy URL with encoded path segments. */
export function streamPath(...segments: PathSegment[]): string {
  return buildPath('/stream', segments);
}
