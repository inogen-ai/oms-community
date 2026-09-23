export interface FrontmatterField { key: string; value: string }
export interface SplitDocument { fields: FrontmatterField[]; body: string }
/** Only flat key/value metadata is parsed; other documents remain untouched. */
export function splitFrontmatter(markdown: string): SplitDocument;
