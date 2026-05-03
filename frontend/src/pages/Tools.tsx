import { useEffect, useRef, useState } from "react";
import api from "../services/api";
import "./Tools.css";

// ── Types ──────────────────────────────────────────────────────
type ToolKind = "mcp_vector_search" | "mcp_genie" | "skill" | "agent_call";
type ToolStatus = "pending_review" | "active" | "inactive";

interface Tool {
  tool_name: string;
  kind: ToolKind;
  ref: string;
  description: string;
  owner: string;
  status: ToolStatus;
  created_at: string;
  environment: string;
}

interface NewToolForm {
  tool_name: string;
  kind: ToolKind;
  ref: string;
  description: string;
  owner: string;
  environment: string;
}

interface GenieRoom {
  id: string;
  name: string;
  mcp_url: string;
}

interface VsIndex {
  name: string;
  endpoint: string;
  mcp_url: string;
  status: string;
}

interface AgentEntry {
  agent_id: string;
  agent_name: string;
  agent_type: string;
  status: string;
}

const KIND_LABELS: Record<ToolKind, string> = {
  mcp_vector_search: "Vector Search",
  mcp_genie:         "Genie",
  skill:             "Skill",
  agent_call:        "Agent Call",
};

const KIND_OPTIONS: ToolKind[] = [
  "mcp_vector_search",
  "mcp_genie",
  "skill",
  "agent_call",
];

const EMPTY_FORM: NewToolForm = {
  tool_name:   "",
  kind:        "mcp_vector_search",
  ref:         "",
  description: "",
  owner:       "",
  environment: "dev",
};

// ── Component ──────────────────────────────────────────────────
export default function Tools() {
  const [tools, setTools]         = useState<Tool[]>([]);
  const [loading, setLoading]     = useState(true);
  const [error, setError]         = useState("");
  const [search, setSearch]       = useState("");
  const [kindFilter, setKindFilter] = useState<ToolKind | "all">("all");
  const [activeTab, setActiveTab] = useState<"tools" | "admin">("tools");
  const [promoting, setPromoting] = useState<Record<string, boolean>>({});
  const [promoteMsg, setPromoteMsg] = useState<Record<string, string>>({});
  const [deleting, setDeleting]     = useState<Record<string, boolean>>({});
  const [deleteMsg, setDeleteMsg]   = useState<Record<string, string>>({});
  const [promoting2, setPromoting2] = useState<Record<string, boolean>>({});
  const [promote2Msg, setPromote2Msg] = useState<Record<string, string>>({});
  const [promoteError, setPromoteError] = useState<{ tool: string; msg: string } | null>(null);

  const [showModal, setShowModal] = useState(false);
  const [form, setForm]           = useState<NewToolForm>(EMPTY_FORM);
  const [saving, setSaving]       = useState(false);
  const [saveError, setSaveError] = useState("");
  const [saveOk, setSaveOk]       = useState("");

  // MCP resource lists
  const [genieRooms, setGenieRooms]   = useState<GenieRoom[]>([]);
  const [vsIndexes, setVsIndexes]     = useState<VsIndex[]>([]);
  const [ucFunctions, setUcFunctions] = useState<string[]>([]);
  const [agentsList, setAgentsList]   = useState<AgentEntry[]>([]);
  const [workspaceHost, setWorkspaceHost] = useState("");
  const [refLoading, setRefLoading]   = useState(false);
  const [refError, setRefError]       = useState("");
  const [manualId, setManualId]       = useState("");

  const firstInputRef = useRef<HTMLInputElement>(null);

  // Load tools on mount
  useEffect(() => {
    setLoading(true);
    api
      .get<{ tools: Tool[] }>("/tools")
      .then((res) => setTools(res.data.tools))
      .catch((err) => {
        const detail =
          err?.response?.data?.detail ??
          err?.message ??
          "Erro desconhecido";
        setError(`GET /api/tools falhou: ${detail}`);
      })
      .finally(() => setLoading(false));
  }, []);

  // Focus first input when modal opens
  useEffect(() => {
    if (showModal) {
      setTimeout(() => firstInputRef.current?.focus(), 50);
    }
  }, [showModal]);

  // Load workspace host once
  useEffect(() => {
    if (workspaceHost) return;
    api.get<{ host: string }>("/workspace-host").then((r) => setWorkspaceHost(r.data.host));
  }, [workspaceHost]);

  // Load MCP resources when kind changes inside modal
  useEffect(() => {
    if (!showModal) return;
    setRefError("");
    setManualId("");

    if (form.kind === "mcp_genie" && genieRooms.length === 0) {
      setRefLoading(true);
      api
        .get<{ rooms: GenieRoom[]; host?: string }>("/genie-rooms")
        .then((res) => {
          setGenieRooms(res.data.rooms);
          if (res.data.host) setWorkspaceHost(res.data.host);
        })
        .catch(() => setRefError("listing_failed"))
        .finally(() => setRefLoading(false));
    }

    if (form.kind === "mcp_vector_search" && vsIndexes.length === 0) {
      setRefLoading(true);
      api
        .get<{ indexes: VsIndex[] }>("/vector-search-indexes")
        .then((res) => setVsIndexes(res.data.indexes))
        .catch(() => setRefError("listing_failed"))
        .finally(() => setRefLoading(false));
    }

    if (form.kind === "skill" && ucFunctions.length === 0) {
      setRefLoading(true);
      api
        .get<{ functions: string[] }>("/uc-functions")
        .then((res) => setUcFunctions(res.data.functions))
        .catch(() => setRefError("listing_failed"))
        .finally(() => setRefLoading(false));
    }

    if (form.kind === "agent_call" && agentsList.length === 0) {
      setRefLoading(true);
      api
        .get<{ agents: AgentEntry[] }>("/agents-list")
        .then((res) => setAgentsList(res.data.agents))
        .catch(() => setRefError("listing_failed"))
        .finally(() => setRefLoading(false));
    }

    // Reset ref when kind changes
    setForm((prev) => ({ ...prev, ref: "" }));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [form.kind, showModal]);

  // ── Filtered list ────────────────────────────────────────────
  const filtered = tools.filter((t) => {
    const q = search.toLowerCase();
    const matchSearch =
      !q ||
      t.tool_name.toLowerCase().includes(q) ||
      t.description.toLowerCase().includes(q) ||
      t.ref.toLowerCase().includes(q);
    const matchKind = kindFilter === "all" || t.kind === kindFilter;
    return matchSearch && matchKind;
  });

  // ── Register tool ────────────────────────────────────────────
  async function handleRegister(e: React.FormEvent) {
    e.preventDefault();
    if (!form.tool_name.trim() || !form.ref.trim()) return;
    setSaving(true);
    setSaveError("");
    setSaveOk("");
    try {
      await api.post("/tools", form);
      const res = await api.get<{ tools: Tool[] }>("/tools");
      setTools(res.data.tools);
      setSaveOk(`Tool "${form.tool_name}" registrada com status pending_review.`);
      setForm(EMPTY_FORM);
      setTimeout(() => {
        setShowModal(false);
        setSaveOk("");
      }, 1800);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Erro ao registrar tool.";
      setSaveError(msg);
    } finally {
      setSaving(false);
    }
  }

  function openModal() {
    setForm(EMPTY_FORM);
    setSaveError("");
    setSaveOk("");
    setRefError("");
    setShowModal(true);
  }

  function closeModal() {
    if (saving) return;
    setShowModal(false);
  }

  // Build MCP URL from manual room/index ID
  function buildGenieUrl(id: string) {
    return id && workspaceHost ? `${workspaceHost}/api/2.0/mcp/genie/${id.trim()}` : "";
  }
  function buildVsUrl(name: string) {
    return name && workspaceHost ? `${workspaceHost}/api/2.0/mcp/vector-search/${name.trim()}` : "";
  }

  // ── Ref field: smart selector or text input ───────────────────
  function RefField() {
    if (form.kind === "mcp_genie") {
      if (refLoading) {
        return <div className="tl-ref-loading">Carregando espaços Genie...</div>;
      }
      if (!refError && genieRooms.length > 0) {
        return (
          <select
            className="tl-input"
            value={form.ref}
            onChange={(e) => setForm({ ...form, ref: e.target.value })}
            required
          >
            <option value="">— Selecione um espaço Genie —</option>
            {genieRooms.map((r) => (
              <option key={r.id} value={r.mcp_url}>
                {r.name || r.id}
              </option>
            ))}
          </select>
        );
      }
      return (
        <div className="tl-ref-manual-wrap">
          {refError === "listing_failed" && (
            <div className="tl-ref-info">
              Não foi possível listar automaticamente. Cole o ID da sala Genie
              (encontrado na URL: <code>/genie/rooms/</code><strong>{"<ID>"}</strong>).
            </div>
          )}
          <input
            className="tl-input"
            placeholder="01f13dbc1ff7131e826a647911e39234"
            value={manualId}
            onChange={(e) => {
              const id = e.target.value;
              setManualId(id);
              setForm({ ...form, ref: buildGenieUrl(id) });
            }}
            required={!form.ref}
          />
          {manualId && (
            <div className="tl-ref-constructed">
              MCP URL: <code>{buildGenieUrl(manualId)}</code>
            </div>
          )}
        </div>
      );
    }

    if (form.kind === "mcp_vector_search") {
      if (refLoading) {
        return <div className="tl-ref-loading">Carregando índices Vector Search...</div>;
      }
      if (!refError && vsIndexes.length > 0) {
        return (
          <select
            className="tl-input"
            value={form.ref}
            onChange={(e) => setForm({ ...form, ref: e.target.value })}
            required
          >
            <option value="">— Selecione um índice Vector Search —</option>
            {vsIndexes.map((idx) => (
              <option key={idx.name} value={idx.mcp_url}>
                {idx.name}{idx.status !== "READY" ? ` (${idx.status})` : ""}{idx.endpoint ? ` · ${idx.endpoint}` : ""}
              </option>
            ))}
          </select>
        );
      }
      return (
        <div className="tl-ref-manual-wrap">
          {refError === "listing_failed" && (
            <div className="tl-ref-info">
              Não foi possível listar automaticamente. Informe o nome completo
              do índice: <code>catalog.schema.index_name</code>
            </div>
          )}
          <input
            className="tl-input"
            placeholder="catalog.schema.index_name"
            value={manualId}
            onChange={(e) => {
              const name = e.target.value;
              setManualId(name);
              setForm({ ...form, ref: buildVsUrl(name) });
            }}
            required={!form.ref}
          />
          {manualId && (
            <div className="tl-ref-constructed">
              MCP URL: <code>{buildVsUrl(manualId)}</code>
            </div>
          )}
        </div>
      );
    }

    if (form.kind === "skill") {
      if (refLoading) {
        return <div className="tl-ref-loading">Carregando funções UC...</div>;
      }
      if (!refError && ucFunctions.length > 0) {
        return (
          <select
            className="tl-input"
            value={form.ref}
            onChange={(e) => setForm({ ...form, ref: e.target.value })}
            required
          >
            <option value="">— Selecione uma UC Function —</option>
            {ucFunctions.map((fn) => (
              <option key={fn} value={fn}>{fn}</option>
            ))}
          </select>
        );
      }
      return (
        <div className="tl-ref-manual-wrap">
          {refError === "listing_failed" && (
            <div className="tl-ref-info">
              Não foi possível listar automaticamente. Informe o nome completo
              da função: <code>catalog.schema.function_name</code>
            </div>
          )}
          <input
            className="tl-input"
            placeholder="catalog.schema.function_name"
            value={form.ref}
            onChange={(e) => setForm({ ...form, ref: e.target.value })}
            required
          />
        </div>
      );
    }

    // agent_call — dropdown from agents_config
    if (refLoading) {
      return <div className="tl-ref-loading">Carregando agentes...</div>;
    }
    if (!refError && agentsList.length > 0) {
      return (
        <select
          className="tl-input"
          value={form.ref}
          onChange={(e) => setForm({ ...form, ref: e.target.value })}
          required
        >
          <option value="">— Selecione um agente —</option>
          {agentsList.map((a) => (
            <option key={a.agent_id} value={a.agent_id}>
              {a.agent_name} ({a.agent_id}){a.status !== "active" ? ` · ${a.status}` : ""}
            </option>
          ))}
        </select>
      );
    }
    return (
      <div className="tl-ref-manual-wrap">
        {refError === "listing_failed" && (
          <div className="tl-ref-info">
            Não foi possível listar automaticamente. Informe o <code>agent_id</code> do agente destino.
          </div>
        )}
        <input
          className="tl-input"
          placeholder="agent_id_destino"
          value={form.ref}
          onChange={(e) => setForm({ ...form, ref: e.target.value })}
          required
        />
      </div>
    );
  }

  // ── Promote / reject tool ────────────────────────────────────
  async function handlePromote(tool_name: string, new_status: "active" | "inactive") {
    setPromoting((p) => ({ ...p, [tool_name]: true }));
    setPromoteMsg((m) => ({ ...m, [tool_name]: "" }));
    try {
      await api.patch(`/tools/${encodeURIComponent(tool_name)}/status`, { new_status });
      const res = await api.get<{ tools: Tool[] }>("/tools");
      setTools(res.data.tools);
      setPromoteMsg((m) => ({
        ...m,
        [tool_name]: new_status === "active" ? "Aprovada!" : "Rejeitada",
      }));
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Erro ao atualizar status.";
      setPromoteMsg((m) => ({ ...m, [tool_name]: `Erro: ${msg}` }));
    } finally {
      setPromoting((p) => ({ ...p, [tool_name]: false }));
    }
  }

  async function handlePromoteEnv(tool_name: string, target_environment: string) {
    setPromoting2((p) => ({ ...p, [tool_name]: true }));
    setPromote2Msg((m) => ({ ...m, [tool_name]: "" }));
    try {
      await api.post(`/tools/${encodeURIComponent(tool_name)}/promote`, { target_environment });
      setTools((prev) =>
        prev.map((t) => t.tool_name === tool_name ? { ...t, environment: target_environment } : t)
      );
      setPromote2Msg((m) => ({ ...m, [tool_name]: `Promovido para ${target_environment}!` }));
      setTimeout(() => setPromote2Msg((m) => ({ ...m, [tool_name]: "" })), 2500);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Erro ao promover ambiente.";
      setPromoteError({ tool: tool_name, msg });
    } finally {
      setPromoting2((p) => ({ ...p, [tool_name]: false }));
    }
  }

  async function handleDelete(tool_name: string) {
    if (!window.confirm(`Remover "${tool_name}" permanentemente?`)) return;
    setDeleting((d) => ({ ...d, [tool_name]: true }));
    setDeleteMsg((m) => ({ ...m, [tool_name]: "" }));
    try {
      await api.delete(`/tools/${encodeURIComponent(tool_name)}`);
      setTools((prev) => prev.filter((t) => t.tool_name !== tool_name));
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Erro ao remover tool.";
      setDeleteMsg((m) => ({ ...m, [tool_name]: `Erro: ${msg}` }));
    } finally {
      setDeleting((d) => ({ ...d, [tool_name]: false }));
    }
  }

  const pendingTools = tools.filter((t) => t.status === "pending_review");

  // ── Render ───────────────────────────────────────────────────
  return (
    <div className="tl-shell">
      {/* ── Header bar ── */}
      <div className="tl-header">
        <div className="tl-header-left">
          <h1 className="tl-title">Tools</h1>
          <span className="tl-count">{tools.length} registradas</span>
        </div>

        <div className="tl-tab-bar">
          <button
            className={`tl-tab${activeTab === "tools" ? " tl-tab-active" : ""}`}
            onClick={() => setActiveTab("tools")}
          >
            Todas as tools
          </button>
          <button
            className={`tl-tab${activeTab === "admin" ? " tl-tab-active" : ""}`}
            onClick={() => setActiveTab("admin")}
          >
            Admin
            {pendingTools.length > 0 && (
              <span className="tl-tab-badge">{pendingTools.length}</span>
            )}
          </button>
        </div>

        <div className="tl-header-right">
          {activeTab === "tools" && (
            <>
              <div className="tl-search-wrap">
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" className="tl-search-icon">
                  <circle cx="6.5" cy="6.5" r="5" stroke="currentColor" strokeWidth="1.5"/>
                  <path d="M10.5 10.5L14 14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
                </svg>
                <input
                  className="tl-search"
                  placeholder="Buscar tools..."
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              </div>

              <div className="tl-kind-wrap">
                <select
                  className="tl-kind-select"
                  value={kindFilter}
                  onChange={(e) => setKindFilter(e.target.value as ToolKind | "all")}
                >
                  <option value="all">Todos os tipos</option>
                  {KIND_OPTIONS.map((k) => (
                    <option key={k} value={k}>{KIND_LABELS[k]}</option>
                  ))}
                </select>
              </div>

              <button className="tl-new-btn" onClick={openModal}>
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                  <path d="M8 2v12M2 8h12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
                </svg>
                Registrar Tool
              </button>
            </>
          )}
        </div>
      </div>

      {/* ── Body ── */}
      <div className="tl-body">
        {loading && <div className="tl-state">Carregando tools...</div>}

        {!loading && error && (
          <div className="tl-error-banner">{error}</div>
        )}

        {/* Tools tab */}
        {activeTab === "tools" && !loading && !error && filtered.length === 0 && (
          <div className="tl-state">
            {tools.length === 0
              ? "Nenhuma tool registrada. Clique em \"Registrar Tool\" para começar."
              : "Nenhuma tool encontrada para os filtros aplicados."}
          </div>
        )}

        {activeTab === "tools" && !loading && !error && filtered.length > 0 && (
          <div className="tl-table-wrap">
            <table className="tl-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Kind</th>
                  <th>Status</th>
                  <th>Ambientes</th>
                  <th>Description</th>
                  <th>Ref</th>
                  <th>Owner</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((tool) => (
                  <tr key={tool.tool_name}>
                    <td className="tl-col-name">
                      <span className="tl-tool-name">{tool.tool_name}</span>
                    </td>
                    <td>
                      <span className={`tl-badge kind-${tool.kind}`}>
                        {KIND_LABELS[tool.kind] ?? tool.kind}
                      </span>
                    </td>
                    <td>
                      <span className={`tl-status-badge status-${tool.status}`}>
                        {tool.status.replace("_", " ")}
                      </span>
                    </td>
                    <td>
                      <div className="tl-env-badges">
                        {tool.environment
                          ? <span className={`tl-env-badge tl-env-${tool.environment}`}>{tool.environment}</span>
                          : <span className="tl-env-none">—</span>}
                      </div>
                    </td>
                    <td className="tl-col-desc">
                      <span className="tl-desc-text">{tool.description || "—"}</span>
                    </td>
                    <td className="tl-col-ref">
                      <code className="tl-ref-code">{tool.ref}</code>
                    </td>
                    <td className="tl-col-owner">{tool.owner}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* Admin tab */}
        {activeTab === "admin" && !loading && !error && (() => {
          const pending  = tools.filter((t) => t.status === "pending_review");
          const approved = tools.filter((t) => t.status !== "pending_review");

          function ToolRow({ tool }: { tool: Tool }) {
            return (
              <tr key={tool.tool_name}>
                <td className="tl-col-name">
                  <span className="tl-tool-name">{tool.tool_name}</span>
                </td>
                <td>
                  <span className={`tl-badge kind-${tool.kind}`}>
                    {KIND_LABELS[tool.kind] ?? tool.kind}
                  </span>
                </td>
                <td>
                  <div className="tl-env-badges">
                    {tool.environment
                      ? <span className={`tl-env-badge tl-env-${tool.environment}`}>{tool.environment}</span>
                      : <span className="tl-env-none">—</span>}
                  </div>
                </td>
                <td className="tl-col-desc">
                  <span className="tl-desc-text">{tool.description || "—"}</span>
                </td>
                <td className="tl-col-ref">
                  <code className="tl-ref-code">{tool.ref}</code>
                </td>
                <td className="tl-col-owner">{tool.owner}</td>
                <td className="tl-col-owner">
                  {tool.created_at ? new Date(tool.created_at).toLocaleDateString("pt-BR") : "—"}
                </td>
                <td className="tl-col-actions">
                  {promote2Msg[tool.tool_name] ? (
                    <span className={`tl-promote-msg ${promote2Msg[tool.tool_name].startsWith("Erro") ? "tl-promote-err" : "tl-promote-ok"}`}>
                      {promote2Msg[tool.tool_name]}
                    </span>
                  ) : deleteMsg[tool.tool_name] ? (
                    <span className="tl-promote-msg tl-promote-err">{deleteMsg[tool.tool_name]}</span>
                  ) : (
                    <div className="tl-action-btns">
                      {tool.status === "pending_review" && (
                        <>
                          <button
                            className="tl-approve-btn"
                            disabled={!!promoting[tool.tool_name] || !!deleting[tool.tool_name]}
                            onClick={() => handlePromote(tool.tool_name, "active")}
                          >
                            {promoting[tool.tool_name] ? "..." : "Aprovar"}
                          </button>
                          <button
                            className="tl-reject-btn"
                            disabled={!!promoting[tool.tool_name] || !!deleting[tool.tool_name]}
                            onClick={() => handlePromote(tool.tool_name, "inactive")}
                          >
                            Rejeitar
                          </button>
                        </>
                      )}
                      {tool.environment === "dev" && tool.status === "active" && (
                        <button
                          className="tl-env-promote-btn tl-env-promote-staging"
                          disabled={!!promoting2[tool.tool_name] || !!deleting[tool.tool_name]}
                          onClick={() => handlePromoteEnv(tool.tool_name, "staging")}
                        >
                          {promoting2[tool.tool_name] ? "..." : "→ Staging"}
                        </button>
                      )}
                      {tool.environment === "staging" && tool.status === "active" && (
                        <button
                          className="tl-env-promote-btn tl-env-promote-prod"
                          disabled={!!promoting2[tool.tool_name] || !!deleting[tool.tool_name]}
                          onClick={() => handlePromoteEnv(tool.tool_name, "prod")}
                        >
                          {promoting2[tool.tool_name] ? "..." : "→ Prod"}
                        </button>
                      )}
                      <button
                        className="tl-delete-btn"
                        disabled={!!deleting[tool.tool_name] || !!promoting[tool.tool_name] || !!promoting2[tool.tool_name]}
                        onClick={() => handleDelete(tool.tool_name)}
                      >
                        {deleting[tool.tool_name] ? "..." : "Remover"}
                      </button>
                    </div>
                  )}
                </td>
              </tr>
            );
          }

          function ToolTable({ rows }: { rows: Tool[] }) {
            return (
              <div className="tl-table-wrap">
                <table className="tl-table">
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Kind</th>
                      <th>Ambiente</th>
                      <th>Description</th>
                      <th>Ref</th>
                      <th>Owner</th>
                      <th>Criada em</th>
                      <th>Ações</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((tool) => <ToolRow key={tool.tool_name} tool={tool} />)}
                  </tbody>
                </table>
              </div>
            );
          }

          return (
            <>
              {/* ── Pendentes ── */}
              <div className="tl-admin-section">
                <div className="tl-admin-section-header">
                  <h2 className="tl-admin-section-title">
                    Aguardando aprovação
                    {pending.length > 0 && <span className="tl-tab-badge">{pending.length}</span>}
                  </h2>
                  <p className="tl-admin-subtitle">
                    Tools registradas que aguardam revisão da plataforma antes de ficarem disponíveis.
                  </p>
                </div>
                {pending.length === 0
                  ? <div className="tl-state tl-state-sm">Nenhuma tool pendente.</div>
                  : <ToolTable rows={pending} />}
              </div>

              {/* ── Aprovadas ── */}
              <div className="tl-admin-section">
                <div className="tl-admin-section-header">
                  <h2 className="tl-admin-section-title">
                    Tools aprovadas
                    {approved.length > 0 && <span className="tl-tab-badge tl-tab-badge-neutral">{approved.length}</span>}
                  </h2>
                  <p className="tl-admin-subtitle">
                    Tools ativas — promova entre ambientes ou remova quando necessário.
                  </p>
                </div>
                {approved.length === 0
                  ? <div className="tl-state tl-state-sm">Nenhuma tool aprovada ainda.</div>
                  : <ToolTable rows={approved} />}
              </div>
            </>
          );
        })()}
      </div>

      {/* ── Promote error modal ── */}
      {promoteError && (
        <div className="tl-overlay" onClick={() => setPromoteError(null)}>
          <div className="tl-modal tl-modal-sm" onClick={(e) => e.stopPropagation()}>
            <div className="tl-modal-header">
              <h2 className="tl-modal-title">Falha ao promover tool</h2>
              <button className="tl-modal-close" onClick={() => setPromoteError(null)} aria-label="Fechar">×</button>
            </div>
            <div className="tl-modal-body">
              <p className="tl-error-tool-name">{promoteError.tool}</p>
              <div className="tl-save-error">{promoteError.msg}</div>
            </div>
            <div className="tl-modal-footer">
              <button className="tl-submit-btn" onClick={() => setPromoteError(null)}>
                Fechar
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── New Tool modal ── */}
      {showModal && (
        <div className="tl-overlay" onClick={closeModal}>
          <div className="tl-modal" onClick={(e) => e.stopPropagation()}>
            <div className="tl-modal-header">
              <h2 className="tl-modal-title">Registrar nova tool</h2>
              <button className="tl-modal-close" onClick={closeModal} aria-label="Fechar">×</button>
            </div>

            <form className="tl-modal-body" onSubmit={handleRegister}>
              {saveOk && <div className="tl-save-ok">{saveOk}</div>}
              {saveError && <div className="tl-save-error">{saveError}</div>}

              <div className="tl-form-row">
                <div className="tl-field tl-field-grow">
                  <label className="tl-label">
                    Nome <span className="req">*</span>
                  </label>
                  <input
                    ref={firstInputRef}
                    className="tl-input"
                    placeholder="ex: hris_vector_search"
                    value={form.tool_name}
                    onChange={(e) => setForm({ ...form, tool_name: e.target.value })}
                    required
                  />
                </div>
                <div className="tl-field">
                  <label className="tl-label">Tipo <span className="req">*</span></label>
                  <select
                    className="tl-input"
                    value={form.kind}
                    onChange={(e) =>
                      setForm({ ...form, kind: e.target.value as ToolKind, ref: "" })
                    }
                  >
                    {KIND_OPTIONS.map((k) => (
                      <option key={k} value={k}>{KIND_LABELS[k]}</option>
                    ))}
                  </select>
                </div>
              </div>

              <div className="tl-field">
                <label className="tl-label">
                  {form.kind === "mcp_genie"
                    ? "Espaço Genie"
                    : form.kind === "mcp_vector_search"
                    ? "Índice Vector Search"
                    : form.kind === "skill"
                    ? "UC Function"
                    : "Agent destino"}
                  {" "}<span className="req">*</span>
                  {form.ref && (
                    <span className="tl-label-hint tl-ref-preview" title={form.ref}>
                      → {form.ref}
                    </span>
                  )}
                </label>
                <RefField />
              </div>

              <div className="tl-field">
                <label className="tl-label">Descrição</label>
                <input
                  className="tl-input"
                  placeholder="O que essa tool faz?"
                  value={form.description}
                  onChange={(e) => setForm({ ...form, description: e.target.value })}
                />
              </div>

              <div className="tl-field">
                <label className="tl-label">Owner (e-mail)</label>
                <input
                  className="tl-input"
                  type="email"
                  placeholder="seu-time@empresa.com"
                  value={form.owner}
                  onChange={(e) => setForm({ ...form, owner: e.target.value })}
                />
              </div>

              <div className="tl-modal-footer">
                <button
                  type="button"
                  className="tl-cancel-btn"
                  onClick={closeModal}
                  disabled={saving}
                >
                  Cancelar
                </button>
                <button
                  type="submit"
                  className="tl-submit-btn"
                  disabled={saving || !form.tool_name.trim() || !form.ref.trim()}
                >
                  {saving ? "Registrando..." : "Registrar tool"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
