// ═══════════════════════════════════════════════════════════════
// ARIA — Royal London AI Assistant
// Backend contract unchanged: POST {backendUrl}/api/chat with
// {query, conversation_history}, SSE-style "data: {...}\n" stream
// with token / done / citations / cached / model_used / latency_ms
// / token_usage / needs_empathy / error fields.
// ═══════════════════════════════════════════════════════════════

const { useState, useRef, useEffect, useCallback } = React;

const MAX_CHARS = 2000;
const NEAR_BOTTOM_PX = 80; // Bug #9: threshold for "smart" autoscroll

const SUGGESTED = [
  "How do I make a claim?",
  "How do I contact Royal London?",
  "What is income protection?",
  "What are the types of pensions?",
  "How do I find a lost pension?",
  "What is a Stocks and Shares ISA?",
];

// ── Markdown renderer (unchanged from prior version — solid) ────
function parseInline(text) {
  const parts = [];
  const regex = /(\*\*(.+?)\*\*|\*(.+?)\*|\[([^\]]+)\]\(([^)]+)\)|\[(\d+)\])/g;
  let last = 0, match, key = 0;
  while ((match = regex.exec(text)) !== null) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    if (match[2]) {
      parts.push(<strong key={key++}>{match[2]}</strong>);
    } else if (match[3]) {
      parts.push(<em key={key++}>{match[3]}</em>);
    } else if (match[4] && match[5]) {
      parts.push(
        <a key={key++} href={match[5]} target="_blank" rel="noopener noreferrer"
           style={{ color: "var(--royal-mid)", textDecoration: "underline" }}>
          {match[4]}
        </a>
      );
    } else if (match[6]) {
      parts.push(<sup key={key++} className="cite-sup">[{match[6]}]</sup>);
    }
    last = match.index + match[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

function renderMarkdown(text) {
  if (!text) return [];
  const lines = text.split("\n");
  const elements = [];
  let i = 0, key = 0;

  while (i < lines.length) {
    const line = lines[i];
    if (line.trim() === "") { i++; continue; }

    if (line.startsWith("### ")) { elements.push(<h3 key={key++}>{parseInline(line.slice(4))}</h3>); i++; continue; }
    if (line.startsWith("## "))  { elements.push(<h2 key={key++}>{parseInline(line.slice(3))}</h2>); i++; continue; }
    if (line.startsWith("# "))   { elements.push(<h1 key={key++}>{parseInline(line.slice(2))}</h1>); i++; continue; }

    if (line.match(/^(\s*[-*•])\s/)) {
      const items = [];
      while (i < lines.length && lines[i].match(/^(\s*[-*•])\s/)) {
        const content = lines[i].replace(/^\s*[-*•]\s/, "");
        items.push(<li key={i}>{parseInline(content)}</li>);
        i++;
      }
      elements.push(<ul key={key++}>{items}</ul>);
      continue;
    }

    if (line.match(/^\d+\.\s/)) {
      const items = [];
      let olCounter = 1;
      while (i < lines.length) {
        const l = lines[i];
        if (l.match(/^\d+\.\s/)) {
          const content = l.replace(/^\d+\.\s/, "");
          items.push(<li key={i} value={olCounter}>{parseInline(content)}</li>);
          olCounter++; i++;
        } else if (l.match(/^\s*[-*•]\s/)) {
          const content = l.replace(/^\s*[-*•]\s/, "");
          items.push(
            <li key={i} style={{ listStyleType: "disc", marginLeft: "16px", fontWeight: "normal" }}>
              {parseInline(content)}
            </li>
          );
          i++;
        } else if (l.trim() === "") {
          i++;
        } else {
          break;
        }
      }
      elements.push(<ol key={key++}>{items}</ol>);
      continue;
    }

    elements.push(<p key={key++}>{parseInline(line)}</p>);
    i++;
  }
  return elements;
}

function MarkdownContent({ text, streaming }) {
  return (
    <div className="md-content">
      {renderMarkdown(text)}
      {streaming && text && <span className="cursor" />}
    </div>
  );
}

// ── Citation pill ─────────────────────────────────────────────
// BUG #6 FIX: key was the array index (post-sort position, which
// isn't a stable identity). Each citation already carries its own
// unique numeric `index` from the backend — use that instead.
function CitationPill({ c }) {
  const label = c.title || c.section || c.url || "";
  return (
    <a href={c.url} target="_blank" rel="noopener noreferrer"
       className="citation-pill" title={c.url}>
      <span className="cite-num">{c.index}</span>
      <span className="cite-label">{label}</span>
    </a>
  );
}

// ── Meta row ─────────────────────────────────────────────────
function MetaRow({ meta }) {
  if (!meta) return null;
  const totalMs = meta.latency_ms
    ? Math.round(Object.values(meta.latency_ms).reduce((a, b) => a + b, 0))
    : null;
  return (
    <div className="meta">
      {meta.cached && <span className="meta-tag cached">⚡ Cached</span>}
      {meta.needs_empathy && <span className="meta-tag empathy">Empathy</span>}
      {meta.model_used && <span className="meta-tag model">{meta.model_used}</span>}
      {totalMs !== null && <span className="meta-tag">{totalMs}ms</span>}
      {meta.token_usage && meta.token_usage.total_tokens && (
        <span className="meta-tag">{meta.token_usage.total_tokens} tokens</span>
      )}
    </div>
  );
}

// ── Message component ────────────────────────────────────────
// BUG #5 FIX: a mid-stream error used to overwrite `content` with
// the error text, discarding whatever had already streamed in.
// The partial content is now preserved and the error renders as a
// distinct note underneath, instead of replacing the answer.
function Message({ msg }) {
  const isUser     = msg.role === "user";
  const hasEmpathy = msg.meta && msg.meta.needs_empathy;
  return (
    <div className={`message-row ${isUser ? "user" : ""}`}>
      <div className={`seal msg-avatar ${isUser ? "user" : "aria"}`}>
        {isUser ? "Y" : "A"}
      </div>
      <div className="msg-body">
        <div className={`bubble ${isUser ? "user" : "assistant"} ${hasEmpathy && !isUser ? "empathy" : ""} ${msg.error ? "error-note" : ""}`}>
          {isUser
            ? msg.content
            : <MarkdownContent text={msg.content} streaming={msg.streaming} />
          }
          {!isUser && msg.error && (
            <div className="error-note">⚠ {msg.error}</div>
          )}
        </div>
        {!isUser && !msg.streaming && msg.citations && msg.citations.length > 0 && (
          <div className="citations">
            {msg.citations
              .slice()
              .sort((a, b) => a.index - b.index)
              .map(c => <CitationPill key={c.index} c={c} />)
            }
          </div>
        )}
        {!isUser && !msg.streaming && <MetaRow meta={msg.meta} />}
      </div>
    </div>
  );
}

function TypingIndicator() {
  return (
    <div className="typing-row">
      <div className="seal msg-avatar aria">A</div>
      <div className="typing-bubble">
        <div className="dot" /><div className="dot" /><div className="dot" />
      </div>
    </div>
  );
}

// ── Main App ─────────────────────────────────────────────────
function App() {
  const [messages, setMessages] = useState([{
    id: 0, role: "assistant",
    content: "Hello! I'm Aria, RLG's AI Assistant. I'm here to help you with questions about Royal London insurance, pensions, ISAs and other financial products. How can I help you today?",
    citations: [], streaming: false, meta: null, error: null,
  }]);
  const [input, setInput]               = useState("");
  const [loading, setLoading]           = useState(false);
  const [error, setError]               = useState(null);
  const [showTyping, setShowTyping]     = useState(false);
  const [backendUrl, setBackendUrl]     = useState("http://127.0.0.1:8000");
  const [showSettings, setShowSettings] = useState(false);
  const [showScrollBtn, setShowScrollBtn] = useState(false);

  const bottomRef        = useRef(null);
  const messagesRef       = useRef(null);
  const inputRef          = useRef(null);
  const abortControllerRef = useRef(null);   // Bug #3
  const isNearBottomRef    = useRef(true);   // Bug #9 (smart autoscroll)

  // Bug #1: textarea auto-resize — grow with content up to the
  // CSS max-height, then let it scroll internally.
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 160) + "px";
  }, [input]);

  // Bug #9: only auto-scroll to the newest message if the user was
  // already near the bottom — otherwise scrolling up to read
  // earlier messages kept getting yanked back down on every token.
  useEffect(() => {
    if (isNearBottomRef.current) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, showTyping]);

  const handleMessagesScroll = () => {
    const el = messagesRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    const near = distanceFromBottom < NEAR_BOTTOM_PX;
    isNearBottomRef.current = near;
    setShowScrollBtn(!near);
  };

  const scrollToBottom = () => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    isNearBottomRef.current = true;
    setShowScrollBtn(false);
  };

  // Clean up any in-flight request if the component unmounts.
  useEffect(() => () => abortControllerRef.current?.abort(), []);

  const history = messages
    .filter(m => m.id !== 0)
    .map(m => ({ role: m.role === "assistant" ? "assistant" : "user", content: m.content }));

  // Bug #2: parse structured error responses (e.g. FastAPI 422
  // validation errors) instead of dumping raw JSON/text.
  const parseErrorResponse = async (res) => {
    const raw = await res.text().catch(() => "");
    try {
      const body = JSON.parse(raw);
      if (Array.isArray(body.detail)) {
        // Pydantic validation error shape: [{loc, msg, type}, ...]
        const msgs = body.detail.map(d => d.msg || JSON.stringify(d)).join("; ");
        return `${res.status}: ${msgs}`;
      }
      if (typeof body.detail === "string") return `${res.status}: ${body.detail}`;
      if (body.error) return `${res.status}: ${body.error}`;
      return `${res.status}: ${raw.slice(0, 160)}`;
    } catch (_) {
      return `${res.status}${raw ? ": " + raw.slice(0, 160) : ""}`;
    }
  };

  const send = useCallback(async (query) => {
    if (!query.trim() || loading || query.length > MAX_CHARS) return;

    // Bug #3: cancel any still-running request before starting a new one.
    abortControllerRef.current?.abort();
    const controller = new AbortController();
    abortControllerRef.current = controller;

    const userMsg = { id: Date.now(), role: "user", content: query.trim(), citations: [], streaming: false, meta: null, error: null };
    setMessages(prev => [...prev, userMsg]);
    setInput("");
    setLoading(true);
    setShowTyping(true);
    setError(null);
    isNearBottomRef.current = true;
    const aId = Date.now() + 1;

    try {
      const res = await fetch(`${backendUrl}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: query.trim(), conversation_history: history }),
        signal: controller.signal,
      });

      if (!res.ok) {
        throw new Error(await parseErrorResponse(res));
      }

      const reader  = res.body.getReader();
      const decoder = new TextDecoder();
      let fullText = "";
      let started  = false;
      let buffer   = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          try {
            const data = JSON.parse(line.slice(6));

            if (data.token) {
              if (!started) {
                setShowTyping(false);
                setMessages(prev => [...prev, {
                  id: aId, role: "assistant", content: "", streaming: true, citations: [], meta: null, error: null,
                }]);
                started = true;
              }
              fullText += data.token;
              setMessages(prev => prev.map(m => m.id === aId ? { ...m, content: fullText } : m));
            }

            if (data.done) {
              setMessages(prev => prev.map(m =>
                m.id === aId ? {
                  ...m,
                  content: fullText,
                  streaming: false,
                  citations: data.citations || [],
                  meta: {
                    cached:        data.cached      || false,
                    model_used:    data.model_used  || null,
                    latency_ms:    data.latency_ms  || null,
                    token_usage:   data.token_usage || null,
                    needs_empathy: data.needs_empathy || false,
                  },
                } : m
              ));
            }

            if (data.error) {
              // Bug #5: preserve whatever streamed in so far —
              // attach the error as a note rather than replacing it.
              setShowTyping(false);
              setMessages(prev => {
                const hasStreaming = prev.some(m => m.id === aId);
                if (hasStreaming) {
                  return prev.map(m => m.id === aId
                    ? { ...m, content: fullText, streaming: false, error: data.error }
                    : m
                  );
                }
                return [...prev, {
                  id: aId, role: "assistant", content: fullText, streaming: false,
                  citations: [], meta: null, error: data.error,
                }];
              });
            }
          } catch (_) {}
        }
      }

      if (buffer.startsWith("data: ")) {
        try {
          const data = JSON.parse(buffer.slice(6));
          if (data.done) {
            setMessages(prev => prev.map(m =>
              m.id === aId ? { ...m, streaming: false, citations: data.citations || [] } : m
            ));
          }
        } catch (_) {}
      }

    } catch (err) {
      setShowTyping(false);
      if (err.name === "AbortError") {
        // User-initiated cancel — not an error state.
        setMessages(prev => prev.map(m =>
          m.id === aId && m.content === "" ? { ...m, content: "_(stopped)_", streaming: false } : m
        ));
      } else {
        setError(`${err.message} — Is the backend running on ${backendUrl}?`);
        setMessages(prev => prev.filter(m => !(m.id === aId && m.content === "")));
      }
    } finally {
      setLoading(false);
      setShowTyping(false);
      abortControllerRef.current = null;
      inputRef.current?.focus();
    }
  }, [loading, backendUrl, history]);

  const stopGenerating = () => {
    abortControllerRef.current?.abort();
  };

  const onKey = (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input); }
  };

  const showSuggestions = messages.length === 1 && !loading;
  const charCount = input.length;
  const overLimit = charCount > MAX_CHARS;
  const nearLimit  = !overLimit && charCount > MAX_CHARS * 0.9;

  return (
    <>
      {/* ── Header ─────────────────────────────────── */}
      <div className="header">
        <div className="header-left">
          <div className="seal aria-mark">A</div>
          <div>
            <div className="header-name">Aria</div>
            <div className="header-sub">Royal London AI Assistant</div>
          </div>
        </div>
        <div className="header-actions">
          <div className="header-status"><span className="status-ring" /> Online</div>
          <button
            className="icon-btn"
            onClick={() => setShowSettings(!showSettings)}
            aria-label="Toggle developer settings"
          >
            ⚙ Settings
          </button>
        </div>
      </div>

      {/* ── Dev settings ───────────────────────────── */}
      {showSettings && (
        <div className="settings-bar">
          <label>Backend URL:</label>
          <input
            type="text" value={backendUrl}
            onChange={e => setBackendUrl(e.target.value)}
            placeholder="http://127.0.0.1:8000"
            aria-label="Backend URL"
          />
          <button onClick={() => setShowSettings(false)}>✓ Done</button>
        </div>
      )}

      {/* ── Messages ────────────────────────────────── */}
      <div className="messages" ref={messagesRef} onScroll={handleMessagesScroll}>
        <div className="messages-inner">
          {messages.map(m => <Message key={m.id} msg={m} />)}
          {showTyping && <TypingIndicator />}
          {error && (
            <div className="error-bar">
              <div className="error-msg">⚠ {error}</div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>
        <button
          className={`scroll-to-bottom ${showScrollBtn ? "visible" : ""}`}
          onClick={scrollToBottom}
          aria-label="Scroll to latest message"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>
      </div>

      {/* ── Suggested questions ──────────────────────── */}
      {showSuggestions && (
        <div className="suggestions">
          <div className="suggestions-inner">
            <span className="suggestion-label">Try asking</span>
            {SUGGESTED.map((q, i) => (
              <button key={i} className="suggestion-btn" onClick={() => send(q)}>{q}</button>
            ))}
          </div>
        </div>
      )}

      {/* ── Input area ──────────────────────────────── */}
      <div className="input-area">
        <div className="input-inner">
          <div className="textarea-wrap">
            <textarea
              ref={inputRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={onKey}
              placeholder="Ask about Royal London insurance, pensions or ISAs…"
              disabled={loading}
              rows={1}
              aria-label="Message Aria"
            />
            {/* Bug #4: character counter */}
            <span className={`char-counter ${overLimit ? "over" : nearLimit ? "warn" : ""}`}>
              {charCount}/{MAX_CHARS}
            </span>
          </div>
          {loading ? (
            <button className="stop-btn" onClick={stopGenerating} aria-label="Stop generating" title="Stop">
              <span className="stop-square" />
            </button>
          ) : (
            <button
              className="send-btn"
              onClick={() => send(input)}
              disabled={loading || !input.trim() || overLimit}
              aria-label="Send message"
              title="Send"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="white"
                   strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <line x1="22" y1="2" x2="11" y2="13" />
                <polygon points="22 2 15 22 11 13 2 9 22 2" />
              </svg>
            </button>
          )}
        </div>
        {/* Bug #8: phone number removed — links to contact page instead */}
        <div className="disclaimer">
          Aria may make mistakes. For complex queries, please see our{" "}
          <a href="https://www.royallondon.com/contact-us/" target="_blank" rel="noopener noreferrer">
            contact page
          </a>.
        </div>
      </div>
    </>
  );
}

ReactDOM.render(<App />, document.getElementById("root"));
