/** Display form of a repository URL: a trailing `.git` is dropped. Never use it for identity or comparison. */
export function sourceDisplayUrl(url) {
  return String(url).replace(/\.git$/i, "");
}
