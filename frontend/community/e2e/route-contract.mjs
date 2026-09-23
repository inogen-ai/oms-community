/** Closed request allowlist, independently exercised by a mutation check. */
const operations = [
  ["GET", /^\/api\/(capabilities|health|skills|skill-changes|review|rules|constraints|settings|graph|import-review)$/],
  ["POST", /^\/api\/(ingest|skills|constraints|import|publish)$/],
  ["GET", /^\/api\/publish\/preview$/],
  ["GET", /^\/api\/skills\/[^/]+$/],
  ["GET", /^\/api\/skills\/[^/]+\/(document|versions|files)$/],
  ["GET", /^\/api\/skills\/[^/]+\/versions\/[^/]+\/compare$/],
  ["PUT", /^\/api\/skills\/[^/]+\/document$/],
  ["POST", /^\/api\/skills\/[^/]+\/versions\/[^/]+\/restore$/],
  ["POST", /^\/api\/import\/directory$/],
  ["PATCH", /^\/api\/skills\/[^/]+$/],
  ["DELETE", /^\/api\/skills\/[^/]+$/],
  ["PUT", /^\/api\/skills\/[^/]+\/sections\/[^/]+$/],
  ["POST", /^\/api\/review\/[^/]+\/decision$/],
  ["POST", /^\/api\/import-review\/[^/]+\/decision$/],
  ["PATCH", /^\/api\/constraints\/[^/]+$/],
  ["PATCH", /^\/api\/rules\/[^/]+$/],
  ["PATCH", /^\/api\/settings$/],
  ["GET", /^\/api\/graph\/nodes\/[^/]+$/],
  ["GET", /^\/api\/graph\/nodes\/[^/]+\/neighbours$/],
];
export const allowedRequest = (method, pathname) => operations.some(([verb, pattern]) => method === verb && pattern.test(pathname));
