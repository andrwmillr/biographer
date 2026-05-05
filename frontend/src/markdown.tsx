import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export const MD_COMPONENTS = {
  p: (props: any) => <p className="my-2 leading-relaxed" {...props} />,
  ul: (props: any) => <ul className="my-2 list-disc pl-5 space-y-0.5" {...props} />,
  ol: (props: any) => <ol className="my-2 list-decimal pl-5 space-y-0.5" {...props} />,
  li: (props: any) => <li className="leading-relaxed" {...props} />,
  h1: (props: any) => <h1 className="my-3 text-base font-semibold text-stone-900" {...props} />,
  h2: (props: any) => <h2 className="my-3 text-sm font-semibold text-stone-900" {...props} />,
  h3: (props: any) => <h3 className="my-2 text-sm font-semibold text-stone-800" {...props} />,
  strong: (props: any) => <strong className="font-semibold text-stone-900" {...props} />,
  em: (props: any) => <em className="italic" {...props} />,
  code: ({ inline, ...props }: any) =>
    inline ? (
      <code className="rounded bg-stone-100 px-1 py-0.5 font-mono text-[0.85em]" {...props} />
    ) : (
      <code className="block rounded bg-stone-100 p-2 font-mono text-xs overflow-auto" {...props} />
    ),
  blockquote: (props: any) => (
    <blockquote className="my-2 border-l-2 border-stone-300 pl-3 text-stone-600" {...props} />
  ),
  a: (props: any) => <a className="text-stone-700 underline" {...props} />,
  hr: () => <hr className="my-3 border-stone-200" />,
};

export const CHAPTER_MD_COMPONENTS = {
  p: (props: any) => <p className="my-4" {...props} />,
  ul: (props: any) => <ul className="my-4 list-disc pl-6 space-y-1" {...props} />,
  ol: (props: any) => <ol className="my-4 list-decimal pl-6 space-y-1" {...props} />,
  li: (props: any) => <li {...props} />,
  h1: (props: any) => <h1 className="mt-6 mb-3 text-2xl font-semibold text-stone-900" {...props} />,
  h2: (props: any) => <h2 className="mt-6 mb-3 text-xl font-semibold text-stone-900" {...props} />,
  h3: (props: any) => <h3 className="mt-5 mb-2 text-lg font-semibold text-stone-900" {...props} />,
  strong: (props: any) => <strong className="font-semibold text-stone-900" {...props} />,
  em: (props: any) => <em className="italic" {...props} />,
  blockquote: (props: any) => (
    <blockquote className="my-5 border-l-4 border-stone-300 pl-4 italic text-stone-700" {...props} />
  ),
  a: (props: any) => <a className="text-stone-700 underline" {...props} />,
  hr: () => <hr className="my-5 border-stone-200" />,
  code: ({ inline, ...props }: any) =>
    inline ? (
      <code className="rounded bg-stone-100 px-1 py-0.5 font-mono text-[0.85em]" {...props} />
    ) : (
      <code className="block rounded bg-stone-100 p-3 font-mono text-sm overflow-auto" {...props} />
    ),
};

function shortPath(p: string, runDir: string): string {
  if (!p) return "";
  if (runDir) {
    const tail = runDir.split("/").pop()!;
    const idx = p.indexOf(tail + "/");
    if (idx >= 0) return p.slice(idx + tail.length + 1);
  }
  return p.split("/").pop() || p;
}

const TOOL_VERB: Record<string, string> = {
  Read: "read",
  Write: "write",
  Edit: "edit",
  TodoWrite: "todos",
};

export function formatTool(name: string, input: unknown, runDir: string): string {
  const verb = TOOL_VERB[name] ?? name.toLowerCase();
  const i = (input ?? {}) as Record<string, unknown>;
  if (name === "Read" || name === "Write" || name === "Edit") {
    const path = shortPath(String(i.file_path ?? ""), runDir);
    let suffix = "";
    if (name === "Read" && (i.offset || i.limit)) {
      const off = Number(i.offset ?? 0);
      const lim = Number(i.limit ?? 0);
      suffix = ` :${off}${lim ? `-${off + lim}` : ""}`;
    }
    return `${verb} ${path || "?"}${suffix}`;
  }
  if (name === "TodoWrite") {
    const todos = Array.isArray(i.todos) ? i.todos : [];
    return `${verb} (${todos.length})`;
  }
  return verb;
}

// Wrap citation patterns in markdown link syntax pointing at
// #cite-<date>::<encoded-context>.  The context is the rest of the
// line surrounding the date — used downstream to disambiguate when
// multiple notes share a date.
//   - `[YYYY-MM-DD]` (bracketed)                  — anywhere
//   - `- YYYY-MM-DD` / `* YYYY-MM-DD` (list-item) — bare date at start of a list item
// The list-item form is a fallback for when the themes agent strips brackets
// off the prompt template; the bracketed form is the canonical citation.
function citeHref(date: string, context: string): string {
  // Strip markdown formatting — we only need plain text for title matching.
  const plain = context
    .replace(/\*+/g, "")
    .replace(/[\[\]()_~`#]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 120);
  if (!plain) return `#cite-${date}`;
  // encodeURIComponent leaves ()!*~' unencoded — they break markdown
  // link syntax [text](url), so percent-encode them too.
  const encoded = encodeURIComponent(plain).replace(
    /[()!*~']/g,
    (c) => "%" + c.charCodeAt(0).toString(16).toUpperCase().padStart(2, "0"),
  );
  return `#cite-${date}::${encoded}`;
}

function wrapCitations(text: string): string {
  return text
    .replace(
      /(?<!\])\[(\d{4}-\d{2}-\d{2})\](?!\()/g,
      (match, date, offset, str) => {
        // Grab the full line for context (title may precede or follow the date).
        const lineStart = str.lastIndexOf("\n", offset) + 1;
        const lineEnd = str.indexOf("\n", offset + match.length);
        const line = str.slice(lineStart, lineEnd >= 0 ? lineEnd : str.length);
        return `[\\[${date}\\]](${citeHref(date, line)})`;
      },
    )
    .replace(
      /^(\s*[-*]\s+)(\d{4}-\d{2}-\d{2})(?=\s|$)/gm,
      (match, prefix, date, offset, str) => {
        const afterMatch = offset + match.length;
        const lineEnd = str.indexOf("\n", afterMatch);
        const rest = str.slice(afterMatch, lineEnd >= 0 ? lineEnd : str.length);
        return `${prefix}[${date}](${citeHref(date, rest)})`;
      },
    );
}

type MarkdownProps = {
  content: string;
  variant: "narration" | "chapter";
  onCiteClick?: (dateKey: string, title?: string) => void;
};

// Renders markdown with shared link handling: `#cite-YYYY-MM-DD` and bare
// `YYYY-MM-DD` hrefs become clickable date jumps when onCiteClick is provided.
export function Markdown({ content, variant, onCiteClick }: MarkdownProps) {
  const base = variant === "chapter" ? CHAPTER_MD_COMPONENTS : MD_COMPONENTS;
  const components = {
    ...base,
    a: ({ href, children, ...props }: any) => {
      let dateKey = "";
      let citeContext = "";
      if (href?.startsWith("#cite-")) {
        const payload = href.slice(6);
        const sep = payload.indexOf("::");
        if (sep >= 0) {
          dateKey = payload.slice(0, sep);
          try { citeContext = decodeURIComponent(payload.slice(sep + 2)); } catch { /* */ }
        } else {
          dateKey = payload;
        }
      } else if (href && /^\d{4}-\d{2}-\d{2}$/.test(href)) {
        dateKey = href;
      }
      if (dateKey && onCiteClick) {
        return (
          <a
            href={href}
            className="text-stone-700 underline decoration-dotted underline-offset-2 cursor-pointer hover:text-stone-900"
            onClick={(e) => {
              e.preventDefault();
              onCiteClick(dateKey, citeContext);
            }}
          >
            {children}
          </a>
        );
      }
      return (
        <a href={href} className="text-stone-700 underline" {...props}>
          {children}
        </a>
      );
    },
  };
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
      {wrapCitations(content)}
    </ReactMarkdown>
  );
}
