import { useEffect, useRef, useState } from "react";
import api from "../services/api";
import { useDomain } from "../contexts/DomainContext";
import "./Run.css";

// ── Types ──────────────────────────────────────────────────────
type Env = "dev" | "staging" | "prod";

interface Agent {
  agent_id: string;
  agent_name: string;
  tools_enabled: string[] | null;
  model: string;
  status: string;
  environment: string;
}

interface Message {
  role: "user" | "assistant";
  content: string;
}

const ENVS: Env[] = ["dev", "staging", "prod"];

const ENV_LABELS: Record<Env, string> = {
  dev:     "Dev",
  staging: "Staging",
  prod:    "Prod",
};

// ── Component ──────────────────────────────────────────────────
export default function Run() {
  const { domain } = useDomain();
  const [selectedEnv, setSelectedEnv] = useState<Env>("dev");
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agentsLoading, setAgentsLoading] = useState(false);
  const [selectedAgentId, setSelectedAgentId] = useState("");

  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Fetch agents whenever env or domain changes
  useEffect(() => {
    setAgentsLoading(true);
    setSelectedAgentId("");
    setAgents([]);
    const params: Record<string, string> = { env: selectedEnv };
    if (domain) params.domain = domain;
    api
      .get("/agents", { params })
      .then((r) => {
        const list: Agent[] = r.data.agents ?? [];
        setAgents(list);
        if (list.length > 0) setSelectedAgentId(list[0].agent_id);
      })
      .catch(() => setAgents([]))
      .finally(() => setAgentsLoading(false));
  }, [selectedEnv, domain]); // eslint-disable-line react-hooks/exhaustive-deps

  // Reset conversation when domain changes
  useEffect(() => {
    setMessages([]);
    setError(null);
  }, [domain]);

  // Auto-scroll to latest message
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, sending]);

  // Auto-resize textarea
  function handleInputChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setInput(e.target.value);
    const el = e.target;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 140) + "px";
  }

  const selectedAgent = agents.find((a) => a.agent_id === selectedAgentId) ?? null;
  const tools = selectedAgent?.tools_enabled ?? [];

  const canSend = !!selectedAgentId && !!input.trim() && !sending;

  async function sendMessage() {
    const text = input.trim();
    if (!text || !selectedAgentId || sending) return;

    const userMsg: Message = { role: "user", content: text };
    const newMessages = [...messages, userMsg];
    setMessages(newMessages);
    setInput("");
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
    setSending(true);
    setError(null);

    try {
      const res = await api.post("/run/chat", {
        env: selectedEnv,
        agent_id: selectedAgentId,
        messages: newMessages,
        ...(domain ? { domain } : {}),
      });
      const assistantMsg: Message = {
        role: "assistant",
        content: res.data.response ?? "(sem resposta)",
      };
      setMessages((prev) => [...prev, assistantMsg]);
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail ?? "Erro ao chamar o endpoint. Verifique as configurações do ambiente.");
    } finally {
      setSending(false);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (canSend) sendMessage();
    }
  }

  function clearChat() {
    setMessages([]);
    setError(null);
  }

  return (
    <div className="rn-shell">
      {/* Header */}
      <div className="rn-header">
        <h1 className="rn-title">Run</h1>
        {messages.length > 0 && (
          <button
            className="rn-send-btn"
            style={{ height: 34, padding: "0 14px", fontSize: "0.8rem", marginLeft: "auto", background: "var(--color-navy)" }}
            onClick={clearChat}
          >
            Limpar conversa
          </button>
        )}
      </div>

      <div className="rn-body">
        {/* Sidebar */}
        <aside className="rn-sidebar">
          {/* Env selector */}
          <div>
            <div className="rn-section-label">Ambiente</div>
            <div className="rn-env-pills">
              {ENVS.map((env) => (
                <button
                  key={env}
                  className={`rn-env-pill${selectedEnv === env ? " active" : ""}`}
                  onClick={() => {
                    setSelectedEnv(env);
                    setMessages([]);
                    setError(null);
                  }}
                >
                  {ENV_LABELS[env]}
                </button>
              ))}
            </div>
          </div>

          {/* Agent selector */}
          <div>
            <div className="rn-section-label">Agente</div>
            {agentsLoading ? (
              <span className="rn-sidebar-loading">Carregando agentes…</span>
            ) : (
              <select
                className="rn-agent-select"
                value={selectedAgentId}
                onChange={(e) => {
                  setSelectedAgentId(e.target.value);
                  setMessages([]);
                  setError(null);
                }}
                disabled={agents.length === 0}
              >
                {agents.length === 0 ? (
                  <option value="">Nenhum agente encontrado</option>
                ) : (
                  agents.map((a) => (
                    <option key={a.agent_id} value={a.agent_id}>
                      {a.agent_name || a.agent_id}
                    </option>
                  ))
                )}
              </select>
            )}
          </div>

          {/* Tools */}
          <div>
            <div className="rn-section-label">
              Tools{tools.length > 0 ? ` (${tools.length})` : ""}
            </div>
            {selectedAgent === null && !agentsLoading ? (
              <span className="rn-tools-empty">Selecione um agente</span>
            ) : tools.length === 0 ? (
              <span className="rn-tools-empty">Sem tools configuradas</span>
            ) : (
              <div className="rn-tools-list">
                {tools.map((t) => (
                  <div key={t} className="rn-tool-chip">
                    <span className="rn-tool-dot" />
                    {t}
                  </div>
                ))}
              </div>
            )}
          </div>
        </aside>

        {/* Chat */}
        <div className="rn-chat">
          <div className="rn-messages">
            {messages.length === 0 && !sending ? (
              <div className="rn-empty-state">
                <div className="rn-empty-icon">💬</div>
                <div className="rn-empty-title">Pronto para conversar</div>
                <div className="rn-empty-sub">
                  Selecione um ambiente e um agente, depois envie uma mensagem.
                </div>
              </div>
            ) : (
              <>
                {messages.map((msg, i) => (
                  <div key={i} className={`rn-msg ${msg.role}`}>
                    <div className="rn-msg-role">
                      {msg.role === "user" ? "Você" : "Agente"}
                    </div>
                    <div className="rn-msg-bubble">{msg.content}</div>
                  </div>
                ))}
                {sending && (
                  <div className="rn-msg assistant">
                    <div className="rn-msg-role">Agente</div>
                    <div className="rn-typing">
                      <span /><span /><span />
                    </div>
                  </div>
                )}
              </>
            )}
            <div ref={messagesEndRef} />
          </div>

          {error && <div className="rn-error-bar">{error}</div>}

          <div className="rn-input-bar">
            <textarea
              ref={textareaRef}
              className="rn-textarea"
              placeholder={
                !selectedAgentId
                  ? "Selecione um agente para começar…"
                  : "Envie uma mensagem… (Enter para enviar, Shift+Enter para nova linha)"
              }
              value={input}
              onChange={handleInputChange}
              onKeyDown={handleKeyDown}
              disabled={!selectedAgentId || sending}
              rows={1}
            />
            <button
              className="rn-send-btn"
              onClick={sendMessage}
              disabled={!canSend}
            >
              Enviar
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
