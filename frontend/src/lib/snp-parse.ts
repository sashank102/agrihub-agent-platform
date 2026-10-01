import Papa from "papaparse";
import type { Assembly, SnpInput, StudyValidation } from "./study-api";

export const MAX_INPUT_BYTES = 5 * 1024 * 1024;

export type SnpRowStatus = "ok" | "warning" | "invalid" | "needs_lookup";

export type SnpIssue = { code: string; message: string };

export type ParsedSnp = {
  line: number;
  raw: string;
  status: SnpRowStatus;
  chrom: string | null;
  pos: number | null;
  markerId: string | null;
  score: number | null;
  pValue: number | null;
  method: string | null;
  issues: SnpIssue[];
  duplicateOf: number | null;
  input: SnpInput | null;
};

export type ColumnRole =
  "id" | "chrom" | "pos" | "score" | "p_value" | "method";
export type ColumnMapping = Partial<Record<ColumnRole, number>>;
export const COLUMN_ROLES: ColumnRole[] = [
  "id",
  "chrom",
  "pos",
  "score",
  "p_value",
  "method",
];

export type ParseFormat = "list" | "table" | "vcf";

export type ParseSummary = {
  total: number;
  valid: number;
  duplicates: number;
  invalid: number;
  needsLookup: number;
  warnings: number;
};

export type ParseResult = {
  format: ParseFormat;
  preset: "sister-team" | null;
  header: string[] | null;
  mapping: ColumnMapping | null;
  needsMapping: boolean;
  rows: ParsedSnp[];
  summary: ParseSummary;
  error: string | null;
};

export type AssemblyInfo = Pick<Assembly, "id" | "chromosomes">;

export type ParseOptions = {
  assembly?: AssemblyInfo | null;
  prefixes?: string[];
  mapping?: ColumnMapping | null;
};

const S_ID = /^S(\d{1,2})_(\d+)$/i;
const CHROM_POS = /^((?:[A-Za-z]+[._]?)?\d{1,2})\s*[:_\s,;]\s*(\d+)$/;
const MARKER = /^[A-Za-z][A-Za-z0-9_.:|-]{2,}$/;
const LOOKUP_HINT = /^(?:ss|rs)\d+$|^BARC[_-]/i;
const NUMERIC = /^-?\d+(?:\.\d+)?(?:e-?\d+)?$/i;
const SISTER_TEAM = ["rs", "chrom", "pos", "maf", "score", "rank", "method"];
const HEADER_ALIASES: Record<ColumnRole, RegExp> = {
  id: /^(rs|rsid|snp|snp_?id|snps|marker|marker_?id|marker_?name|id|name|variant|variant_?id)$/i,
  chrom: /^(#?chrom|chr|chromosome|chrom_?id|seqid|seqname|contig)$/i,
  pos: /^(pos|position|bp|ps|base_?pair|base_?pair_?location|bp_?position|location|coordinate)$/i,
  score: /^(score|importance|weight|effect|beta)$/i,
  p_value: /^(p|pval|p_?value|p\.value|pvalue)$/i,
  method: /^(method|model|algorithm)$/i,
};

type Normalizer = {
  chrom: (token: string) => string | null;
  length: (chrom: string) => number | null;
  checked: boolean;
};

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function chromosomeNormalizer(
  assembly: AssemblyInfo | null | undefined,
  prefixes: string[] = [],
): Normalizer {
  if (!assembly) {
    return {
      chrom: (token) => token.trim() || null,
      length: () => null,
      checked: false,
    };
  }
  const known = [...new Set([...prefixes, "chr", "chromosome", "gm"])]
    .sort((a, b) => b.length - a.length)
    .map(escapeRegExp)
    .join("|");
  const numbered = new RegExp(`^(?:${known})?[._]?0*(\\d{1,3})$`, "i");
  const exact = new Map<string, string>();
  const byNumber = new Map<number, string>();
  const lengths = new Map<string, number>();
  for (const chromosome of assembly.chromosomes) {
    exact.set(chromosome.name.toLowerCase(), chromosome.name);
    for (const alias of chromosome.aliases) {
      exact.set(alias.toLowerCase(), chromosome.name);
    }
    lengths.set(chromosome.name, chromosome.length);
    const match = numbered.exec(chromosome.name);
    if (match) {
      byNumber.set(Number(match[1]), chromosome.name);
    }
  }
  return {
    chrom: (token) => {
      const value = token.trim();
      const direct = exact.get(value.toLowerCase());
      if (direct) {
        return direct;
      }
      const match = numbered.exec(value);
      return match ? (byNumber.get(Number(match[1])) ?? null) : null;
    },
    length: (chrom) => lengths.get(chrom) ?? null,
    checked: true,
  };
}

type Located =
  | { kind: "position"; chromToken: string; pos: number }
  | { kind: "marker"; marker: string }
  | { kind: "invalid"; message: string };

export function locateToken(token: string): Located {
  const value = token.trim();
  const sId = S_ID.exec(value);
  if (sId) {
    return { kind: "position", chromToken: sId[1], pos: Number(sId[2]) };
  }
  const chromPos = CHROM_POS.exec(value);
  if (chromPos) {
    return {
      kind: "position",
      chromToken: chromPos[1],
      pos: Number(chromPos[2]),
    };
  }
  if (MARKER.test(value)) {
    return { kind: "marker", marker: value };
  }
  return {
    kind: "invalid",
    message: `"${value}" is not a SNP id, chromosome:position or marker name`,
  };
}

function toNumber(value: string | undefined): number | null {
  if (value === undefined || value.trim() === "") {
    return null;
  }
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

type Extras = {
  score?: number | null;
  pValue?: number | null;
  method?: string | null;
};

function emptyRow(line: number, raw: string, extras: Extras): ParsedSnp {
  return {
    line,
    raw,
    status: "ok",
    chrom: null,
    pos: null,
    markerId: null,
    score: extras.score ?? null,
    pValue: extras.pValue ?? null,
    method: extras.method ?? null,
    issues: [],
    duplicateOf: null,
    input: null,
  };
}

function invalid(row: ParsedSnp, code: string, message: string): ParsedSnp {
  row.status = "invalid";
  row.issues.push({ code, message });
  row.input = null;
  return row;
}

function placeRow(
  row: ParsedSnp,
  chromToken: string,
  pos: number,
  normalizer: Normalizer,
): ParsedSnp {
  const chrom = normalizer.chrom(chromToken);
  if (!chrom) {
    return invalid(
      row,
      "unknown_chromosome",
      `${chromToken} is not a chromosome of this assembly`,
    );
  }
  if (!Number.isInteger(pos) || pos < 1) {
    return invalid(row, "bad_position", `${pos} is not a valid position`);
  }
  const length = normalizer.length(chrom);
  if (length !== null && pos > length) {
    return invalid(
      row,
      "out_of_bounds",
      `${chrom}:${pos} is beyond the end of ${chrom} (${length.toLocaleString("en-US")} bp)`,
    );
  }
  row.chrom = chrom;
  row.pos = pos;
  if (!normalizer.checked) {
    row.status = "warning";
    row.issues.push({
      code: "unchecked",
      message: "chromosome names are checked once the assembly is loaded",
    });
  }
  return row;
}

function fromToken(
  line: number,
  token: string,
  normalizer: Normalizer,
  extras: Extras = {},
): ParsedSnp {
  const row = emptyRow(line, token.trim(), extras);
  const located = locateToken(token);
  if (located.kind === "invalid") {
    return invalid(row, "unparsed", located.message);
  }
  if (located.kind === "marker") {
    row.markerId = located.marker;
    row.status = "needs_lookup";
    row.issues.push({
      code: "needs_lookup",
      message: LOOKUP_HINT.test(located.marker)
        ? "marker id; placed on the assembly by the server"
        : "not a position; the server will try it as a marker name",
    });
    return row;
  }
  return placeRow(row, located.chromToken, located.pos, normalizer);
}

function fromColumns(
  line: number,
  id: string | undefined,
  chromToken: string | undefined,
  posToken: string | undefined,
  normalizer: Normalizer,
  extras: Extras,
): ParsedSnp {
  const label = id?.trim() ?? "";
  const chromValue = chromToken?.trim() ?? "";
  const posValue = posToken?.trim() ?? "";
  if (chromValue && posValue) {
    const row = emptyRow(line, label || `${chromValue}:${posValue}`, extras);
    if (!/^\d+$/.test(posValue)) {
      return invalid(
        row,
        "bad_position",
        `${posValue} is not a valid position`,
      );
    }
    placeRow(row, chromValue, Number(posValue), normalizer);
    if (row.status !== "invalid" && label) {
      const named = locateToken(label);
      if (named.kind === "position") {
        const namedChrom = normalizer.chrom(named.chromToken);
        if (namedChrom !== row.chrom || named.pos !== row.pos) {
          row.status = "warning";
          row.issues.push({
            code: "position_mismatch",
            message: `${label} names ${namedChrom ?? named.chromToken}:${named.pos} but the row gives ${row.chrom}:${row.pos}; using the row`,
          });
        }
      }
    }
    return row;
  }
  if (label) {
    return fromToken(line, label, normalizer, extras);
  }
  return invalid(
    emptyRow(line, `(row ${line})`, extras),
    "empty",
    "the row has no SNP id or position",
  );
}

function finalize(rows: ParsedSnp[]): ParsedSnp[] {
  const first = new Map<string, number>();
  for (const row of rows) {
    if (row.status === "invalid") {
      continue;
    }
    const key =
      row.chrom !== null && row.pos !== null
        ? `${row.chrom}:${row.pos}`
        : `marker:${(row.markerId ?? row.raw).toLowerCase()}`;
    const earlier = first.get(key);
    if (earlier !== undefined) {
      row.status = "warning";
      row.duplicateOf = earlier;
      row.issues.push({
        code: "duplicate",
        message: `same SNP as line ${earlier}; kept once`,
      });
      row.input = null;
      continue;
    }
    first.set(key, row.line);
    row.input = {
      raw: row.raw,
      ...(row.chrom !== null && row.pos !== null
        ? { chrom: row.chrom, pos: row.pos }
        : { marker_id: row.markerId }),
      ...(row.score !== null ? { score: row.score } : {}),
      ...(row.pValue !== null && row.pValue >= 0 && row.pValue <= 1
        ? { p_value: row.pValue }
        : {}),
      ...(row.method ? { method: row.method } : {}),
    };
  }
  return rows;
}

export function summarize(rows: ParsedSnp[]): ParseSummary {
  const summary: ParseSummary = {
    total: rows.length,
    valid: 0,
    duplicates: 0,
    invalid: 0,
    needsLookup: 0,
    warnings: 0,
  };
  for (const row of rows) {
    if (row.duplicateOf !== null) {
      summary.duplicates += 1;
    } else if (row.status === "invalid") {
      summary.invalid += 1;
    } else if (row.status === "needs_lookup") {
      summary.needsLookup += 1;
    } else {
      summary.valid += 1;
      if (row.status === "warning") {
        summary.warnings += 1;
      }
    }
  }
  return summary;
}

export function guessMapping(header: string[]): {
  mapping: ColumnMapping;
  preset: "sister-team" | null;
} {
  const names = header.map((cell) => cell.trim());
  if (
    SISTER_TEAM.every((name, index) => names[index]?.toLowerCase() === name)
  ) {
    return {
      mapping: { id: 0, chrom: 1, pos: 2, score: 4, method: 6 },
      preset: "sister-team",
    };
  }
  const mapping: ColumnMapping = {};
  names.forEach((name, index) => {
    for (const role of COLUMN_ROLES) {
      if (mapping[role] === undefined && HEADER_ALIASES[role].test(name)) {
        mapping[role] = index;
        return;
      }
    }
  });
  return { mapping, preset: null };
}

export function mappingIsComplete(mapping: ColumnMapping): boolean {
  return (
    mapping.id !== undefined ||
    (mapping.chrom !== undefined && mapping.pos !== undefined)
  );
}

function looksLikeHeader(first: string[], second: string[] | undefined) {
  if (first.some((cell) => locateToken(cell).kind === "position")) {
    return false;
  }
  if (
    first.some((cell) =>
      COLUMN_ROLES.some((role) => HEADER_ALIASES[role].test(cell.trim())),
    )
  ) {
    return true;
  }
  const numeric = (cell: string) => NUMERIC.test(cell.trim());
  return (
    first.every((cell) => !numeric(cell)) &&
    second !== undefined &&
    second.some(numeric)
  );
}

function byteLength(text: string): number {
  return new TextEncoder().encode(text).length;
}

function result(
  format: ParseFormat,
  rows: ParsedSnp[],
  extra: Partial<ParseResult> = {},
): ParseResult {
  return {
    format,
    preset: null,
    header: null,
    mapping: null,
    needsMapping: false,
    rows,
    summary: summarize(rows),
    error: null,
    ...extra,
  };
}

function parseVcf(lines: string[], normalizer: Normalizer): ParseResult {
  const rows: ParsedSnp[] = [];
  lines.forEach((text, index) => {
    if (!text.trim() || text.startsWith("#")) {
      return;
    }
    const [chrom, pos, id] = text.split("\t");
    const label = id && id !== "." ? id : undefined;
    rows.push(fromColumns(index + 1, label, chrom, pos, normalizer, {}));
  });
  return result("vcf", finalize(rows));
}

function parseTable(
  data: string[][],
  header: string[],
  mapping: ColumnMapping,
  preset: "sister-team" | null,
  normalizer: Normalizer,
): ParseResult {
  if (!mappingIsComplete(mapping)) {
    return result("table", [], {
      header,
      mapping,
      preset,
      needsMapping: true,
    });
  }
  const cell = (row: string[], role: ColumnRole) =>
    mapping[role] === undefined ? undefined : row[mapping[role]];
  const rows = data.map((row, index) =>
    fromColumns(
      index + 2,
      cell(row, "id"),
      cell(row, "chrom"),
      cell(row, "pos"),
      normalizer,
      {
        score: toNumber(cell(row, "score")),
        pValue: toNumber(cell(row, "p_value")),
        method: cell(row, "method")?.trim() || null,
      },
    ),
  );
  return result("table", finalize(rows), { header, mapping, preset });
}

function parseList(lines: string[], normalizer: Normalizer): ParseResult {
  const rows: ParsedSnp[] = [];
  lines.forEach((text, index) => {
    const value = text.trim();
    if (!value || value.startsWith("#")) {
      return;
    }
    if (locateToken(value).kind === "position") {
      rows.push(fromToken(index + 1, value, normalizer));
      return;
    }
    const tokens = value.split(/[\s,;]+/).filter(Boolean);
    const rest = tokens.slice(1);
    const leading = locateToken(tokens[0]).kind !== "invalid";
    if (
      leading &&
      rest.length > 0 &&
      rest.every((token) => NUMERIC.test(token))
    ) {
      rows.push(
        rest.length >= 2 &&
          /^\d+$/.test(rest[1]) &&
          normalizer.chrom(rest[0]) !== null
          ? fromColumns(index + 1, tokens[0], rest[0], rest[1], normalizer, {})
          : fromToken(index + 1, tokens[0], normalizer),
      );
      return;
    }
    for (const token of tokens) {
      rows.push(fromToken(index + 1, token, normalizer));
    }
  });
  return result("list", finalize(rows));
}

export function parseSnpText(
  text: string,
  options: ParseOptions = {},
): ParseResult {
  if (byteLength(text) > MAX_INPUT_BYTES) {
    return result("list", [], {
      error: `the SNP input is larger than ${MAX_INPUT_BYTES / 1024 / 1024} MB`,
    });
  }
  const normalizer = chromosomeNormalizer(options.assembly, options.prefixes);
  const lines = text.replace(/^\uFEFF/, "").split(/\r?\n/);
  const firstLine = lines.find((line) => line.trim()) ?? "";
  if (
    firstLine.startsWith("##fileformat=VCF") ||
    firstLine.startsWith("#CHROM")
  ) {
    return parseVcf(lines, normalizer);
  }
  const parsed = Papa.parse<string[]>(text.replace(/^\uFEFF/, ""), {
    skipEmptyLines: "greedy",
    delimitersToGuess: [",", "\t", ";", "|"],
  });
  const data = parsed.data.filter((row) => row.some((cell) => cell.trim()));
  if (
    data.length > 0 &&
    data[0].length >= 2 &&
    looksLikeHeader(data[0], data[1])
  ) {
    const header = data[0].map((cell) => cell.trim());
    const guessed = guessMapping(header);
    const mapping = options.mapping ?? guessed.mapping;
    return parseTable(
      data.slice(1),
      header,
      mapping,
      options.mapping ? null : guessed.preset,
      normalizer,
    );
  }
  return parseList(lines, normalizer);
}

export function submittableSnps(rows: ParsedSnp[]): SnpInput[] {
  return rows.flatMap((row) => (row.input ? [row.input] : []));
}

export function rejectedRowsCsv(rows: ParsedSnp[]): string {
  const rejected = rows.filter(
    (row) => row.status === "invalid" || row.duplicateOf !== null,
  );
  return Papa.unparse({
    fields: ["line", "snp", "status", "reason"],
    data: rejected.map((row) => [
      row.line,
      row.raw,
      row.duplicateOf !== null ? "duplicate" : row.status,
      row.issues.map((issue) => issue.message).join("; "),
    ]),
  });
}

export type PreviewRow = ParsedSnp & {
  server: "placed" | "dropped" | null;
};

export function mergeValidation(
  rows: ParsedSnp[],
  validation: StudyValidation | null,
): PreviewRow[] {
  if (!validation) {
    return rows.map((row) => ({ ...row, server: null }));
  }
  const placed = new Map<string, SnpInput>();
  for (const snp of validation.placed_snps) {
    if (!placed.has(snp.raw)) {
      placed.set(snp.raw, snp);
    }
  }
  const warnings = new Map<string, SnpIssue[]>();
  for (const warning of validation.warnings) {
    if (!warning.snp) {
      continue;
    }
    const list = warnings.get(warning.snp) ?? [];
    list.push({ code: warning.code, message: warning.message });
    warnings.set(warning.snp, list);
  }
  const errors = new Map<number, SnpIssue[]>();
  for (const error of validation.errors) {
    if (error.loc[0] === "snps" && typeof error.loc[1] === "number") {
      const list = errors.get(error.loc[1]) ?? [];
      list.push({ code: "invalid", message: error.message });
      errors.set(error.loc[1], list);
    }
  }
  let index = -1;
  return rows.map((row) => {
    if (!row.input) {
      return { ...row, server: null };
    }
    index += 1;
    const rowErrors = errors.get(index);
    const rowWarnings = warnings.get(row.raw) ?? [];
    const issues = row.issues.filter((issue) => issue.code !== "needs_lookup");
    if (rowErrors) {
      return {
        ...row,
        status: "invalid",
        issues: [...issues, ...rowErrors],
        server: "dropped",
      };
    }
    const hit = placed.get(row.raw);
    if (hit) {
      return {
        ...row,
        chrom: hit.chrom ?? row.chrom,
        pos: hit.pos ?? row.pos,
        status:
          rowWarnings.length || row.status === "warning" ? "warning" : "ok",
        issues: [...issues, ...rowWarnings],
        server: "placed",
      };
    }
    const duplicate = rowWarnings.some((issue) => issue.code === "duplicate");
    return {
      ...row,
      status: duplicate ? "warning" : "invalid",
      issues: rowWarnings.length
        ? [...issues, ...rowWarnings]
        : [
            ...issues,
            { code: "not_placed", message: "the server could not place it" },
          ],
      server: "dropped",
    };
  });
}
