// Published SKILL.md frontmatter is metadata, not Markdown headings.
const KEY_LINE = /^([A-Za-z0-9_-]+):\s*(.*)$/;

/** Split a leading flat key/value block, preserving documents without a close. */
export function splitFrontmatter(markdown) {
  const lines = markdown.split(/\r?\n/);
  if (lines[0]?.trim() !== "---") return { fields: [], body: markdown };
  const close = lines.findIndex((line, index) => index > 0 && line.trim() === "---");
  if (close < 0) return { fields: [], body: markdown };

  const fields = [];
  for (const line of lines.slice(1, close)) {
    if (!line.trim()) continue;
    const match = KEY_LINE.exec(line);
    if (match) {
      fields.push({ key: match[1], value: match[2].trim() });
    } else if (fields.length) {
      fields[fields.length - 1].value += ` ${line.trim()}`;
    }
  }
  const body = lines.slice(close + 1).join("\n").replace(/^\n+/, "");
  return { fields, body };
}
