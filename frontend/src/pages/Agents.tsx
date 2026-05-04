import { useEffect, useRef, useState, useCallback } from "react";
import api from "../services/api";
import "./Agents.css";

// ── Types ──────────────────────────────────────────────────────
type AgentStatus = "draft" | "pending_approval" | "evaluating" | "qa" | "approved" | "deployed" | "disabled";
type AgentType   = "qa_docs" | "data_assistant" | "conversational" | "mixed";

interface Agent {
  agent_id: string;
  agent_name: string;
  agent_type: AgentType;
  owner_principal: string;
  description: string;
  instructions: string;
  tools_enabled: string[] | null;
  model: string;
  serving_endpoint_name: string;
  eval_profile: string;
  min_safety_score: number | null;
  min_correctness_score: number | null;
  environment: string;
  status: AgentStatus;
  runtime_mode: string;
  mlflow_experiment_id: string | null;
  mlflow_url: string | null;
  eval_run_id: string | null;
  eval_status: string | null;
  created_at: string;
  updated_at: string;
}

interface ActiveTool {
  tool_name: string;
  kind: string;
  description: string;
  status: string;
  environment: string;
}

interface EvalEntry {
  id: string;
  agent_id: string;
  request: string;
  expected_response: string;
  created_at: string;
}

interface NewAgentForm {
  agent_id: string;
  agent_name: string;
  agent_type: AgentType;
  owner_principal: string;
  description: string;
  instructions: string;
  tools_enabled: string[];
  model: string;
  eval_profile: string;
  min_safety_score: string;
  min_correctness_score: string;
  runtime_mode: string;
}

const EMPTY_FORM: NewAgentForm = {
  agent_id:              "",
  agent_name:            "",
  agent_type:            "conversational",
  owner_principal:       "",
  description:           "",
  instructions:          "",
  tools_enabled:         [],
  model:                 "",
  eval_profile:          "default_internal",
  min_safety_score:      "",
  min_correctness_score: "",
  runtime_mode:          "config-driven-single-endpoint",
};

const AGENT_TYPE_LABELS: Record<AgentType, string> = {
  qa_docs:        "Q&A Documentos",
  data_assistant: "Data Assistant",
  conversational: "Conversacional",
  mixed:          "Misto",
};

const STATUS_LABELS: Record<AgentStatus, string> = {
  draft:            "Draft",
  pending_approval: "Aguardando Aprovação",
  evaluating:       "Em Avaliação",
  qa:               "Em QA",
  approved:         "Aprovado",
  deployed:         "Deployed",
  disabled:         "Desativado",
};

// Status transitions for the admin certification table (non-pending agents)
// action="promote" → POST /agents/{id}/promote
const STATUS_TRANSITIONS: Record<AgentStatus, { label: string; next: AgentStatus; variant: "promote" | "deploy" | "danger"; action: "promote" }[]> = {
  draft:            [],
  pending_approval: [],
  evaluating:       [{ label: "Promover para Prod", next: "approved", variant: "deploy", action: "promote" }],
  approved:         [],
  deployed:         [],
  qa:               [],
  disabled:         [],
};

// ── Component ──────────────────────────────────────────────────
export default function Agents() {
  const [agents, setAgents]     = useState<Agent[]>([]);
  const [loading, setLoading]   = useState(true);
  const [error, setError]       = useState("");
  const [search, setSearch]     = useState("");
  const [statusFilter, setStatusFilter] = useState<AgentStatus | "all">("all");
  const [activeTab, setActiveTab] = useState<"agents" | "admin">("agents");

  // Modal (create / edit / view)
  const [showModal, setShowModal]       = useState(false);
  const [editingAgent, setEditingAgent] = useState<Agent | null>(null);
  const [form, setForm]                 = useState<NewAgentForm>(EMPTY_FORM);
  const [saving, setSaving]             = useState(false);
  const [saveError, setSaveError]       = useState("");
  const [saveOk, setSaveOk]             = useState("");
  const [formTab, setFormTab]           = useState<"basic" | "prompt" | "eval">("basic");

  // Eval dataset
  const [evalEntries, setEvalEntries]       = useState<EvalEntry[]>([]);
  const [evalLoading, setEvalLoading]       = useState(false);
  const [evalError, setEvalError]           = useState("");
  const [evalAdding, setEvalAdding]         = useState(false);
  const [evalAddForm, setEvalAddForm]       = useState({ request: "", expected_response: "" });
  const [evalAddSaving, setEvalAddSaving]   = useState(false);
  const [evalEditingId, setEvalEditingId]   = useState<string | null>(null);
  const [evalEditForm, setEvalEditForm]     = useState({ request: "", expected_response: "" });
  const [evalEditSaving, setEvalEditSaving] = useState(false);
  const [evalDeletingId, setEvalDeletingId] = useState<string | null>(null);

  // Active tools for multi-select
  const [activeTools, setActiveTools]   = useState<ActiveTool[]>([]);
  const [toolsLoading, setToolsLoading] = useState(false);
  const [toolsError, setToolsError]     = useState("");

  // Approved serving endpoints for model dropdown
  const [approvedModels, setApprovedModels]     = useState<{ name: string; model_name: string; state: string }[]>([]);
  const [modelsLoading, setModelsLoading]       = useState(false);

  // Delete
  const [deleting, setDeleting] = useState<Record<string, boolean>>({});

  // Admin actions
  const [promoting, setPromoting]   = useState<Record<string, boolean>>({});
  const [promoteMsg, setPromoteMsg] = useState<Record<string, string>>({});

  // Eval run
  const [triggeringEval, setTriggeringEval] = useState<Record<string, boolean>>({});
  const [evalRunMsg, setEvalRunMsg]         = useState<Record<string, string>>({});

  // Approval request (domain team)
  const [requesting, setRequesting]         = useState<Record<string, boolean>>({});
  const [requestMsg, setRequestMsg]         = useState<Record<string, string>>({});
  const [evalWarningAgentId, setEvalWarningAgentId] = useState<string | null>(null);

  // Admin approval review
  const [reviewingApproval, setReviewingApproval] = useState<Record<string, boolean>>({});
  const [reviewMsg, setReviewMsg]                 = useState<Record<string, string>>({});


  // Chat modal
  const [chatModal, setChatModal]         = useState<{ agent: Agent } | null>(null);
  const [chatMessages, setChatMessages]   = useState<{ role: "user" | "assistant"; content: string }[]>([]);
  const [chatInput, setChatInput]         = useState("");
  const [chatLoading, setChatLoading]     = useState(false);
  const [chatError, setChatError]         = useState("");
  const chatEndRef  = useRef<HTMLDivElement>(null);
  const chatInputRef = useRef<HTMLTextAreaElement>(null);

  const firstInputRef = useRef<HTMLInputElement>(null);

  // ── Load agents ───────────────────────────────────────────────
  function loadAgents() {
    setLoading(true);
    api
      .get<{ agents: Agent[] }>("/agents")
      .then((r) => setAgents(r.data.agents))
      .catch((err) => {
        const detail = err?.response?.data?.detail ?? err?.message ?? "Erro desconhecido";
        setError(`GET /api/agents falhou: ${detail}`);
      })
      .finally(() => setLoading(false));
  }

  useEffect(loadAgents, []);

  // Load active tools and approved models when modal opens
  useEffect(() => {
    if (!showModal) return;
    if (activeTools.length === 0) {
      setToolsLoading(true);
      setToolsError("");
      api
        .get<{ tools: ActiveTool[] }>("/tools")
        .then((r) => {
          const approved = r.data.tools.filter((t) => t.status === "active");
          setActiveTools(approved);
          if (approved.length === 0) setToolsError("Nenhuma tool ativa. Registre tools na aba Tools.");
        })
        .catch((err) => {
          const detail = err?.response?.data?.detail ?? err?.message ?? "Erro";
          setToolsError(`Erro ao carregar tools: ${detail}`);
        })
        .finally(() => setToolsLoading(false));
    }
    if (approvedModels.length === 0) {
      setModelsLoading(true);
      api
        .get<{ models: { name: string; model_name: string; state: string; approval_status: string }[] }>("/settings/models")
        .then((r) => {
          setApprovedModels(r.data.models.filter((m) => m.approval_status === "approved"));
        })
        .catch(() => {})
        .finally(() => setModelsLoading(false));
    }
  }, [showModal]);

  useEffect(() => {
    if (showModal) setTimeout(() => firstInputRef.current?.focus(), 50);
  }, [showModal]);

  useEffect(() => {
    if (formTab !== "eval" || !editingAgent) return;
    setEvalLoading(true);
    setEvalError("");
    api
      .get<{ entries: EvalEntry[] }>(`/agents/${encodeURIComponent(editingAgent.agent_id)}/eval-dataset`)
      .then((r) => setEvalEntries(r.data.entries))
      .catch((err) => {
        const detail = err?.response?.data?.detail ?? err?.message ?? "Erro";
        setEvalError(`Erro ao carregar dataset: ${detail}`);
      })
      .finally(() => setEvalLoading(false));
  }, [formTab, editingAgent?.agent_id]);

  // ── Filters ───────────────────────────────────────────────────
  const filtered = agents.filter((a) => {
    const q = search.toLowerCase();
    const matchSearch =
      !q ||
      a.agent_name.toLowerCase().includes(q) ||
      a.agent_id.toLowerCase().includes(q) ||
      (a.description || "").toLowerCase().includes(q);
    const matchStatus = statusFilter === "all" || a.status === statusFilter;
    return matchSearch && matchStatus;
  });

  const pendingAgents = agents.filter((a) => a.status === "pending_approval");

  // ── Create / Save ─────────────────────────────────────────────
  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    if (!form.agent_id.trim() || !form.agent_name.trim()) return;
    setSaving(true);
    setSaveError("");
    setSaveOk("");
    const payload = {
      ...form,
      min_safety_score:     form.min_safety_score     ? parseFloat(form.min_safety_score)     : null,
      min_correctness_score: form.min_correctness_score ? parseFloat(form.min_correctness_score) : null,
    };
    try {
      if (editingAgent) {
        const { agent_id, ...updatePayload } = payload;
        void agent_id;
        await api.put(`/agents/${encodeURIComponent(editingAgent.agent_id)}`, updatePayload);
        setSaveOk(`Agente "${form.agent_name}" atualizado.`);
      } else {
        await api.post("/agents", payload);
        setSaveOk(`Agente "${form.agent_name}" criado com status draft.`);
      }
      loadAgents();
      setTimeout(() => { setShowModal(false); setSaveOk(""); }, 1500);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        (editingAgent ? "Erro ao atualizar agente." : "Erro ao criar agente.");
      setSaveError(msg);
    } finally {
      setSaving(false);
    }
  }

  function _resetEvalState() {
    setEvalEntries([]);
    setEvalError("");
    setEvalAdding(false);
    setEvalAddForm({ request: "", expected_response: "" });
    setEvalEditingId(null);
  }

  function openModal() {
    setEditingAgent(null);
    setForm(EMPTY_FORM);
    setSaveError("");
    setSaveOk("");
    setFormTab("basic");
    setActiveTools([]);
    setToolsError("");
    setApprovedModels([]);
    _resetEvalState();
    setShowModal(true);
  }

  function openEditModal(agent: Agent, tab: "basic" | "prompt" | "eval" = "basic") {
    setEditingAgent(agent);
    setForm({
      agent_id:              agent.agent_id,
      agent_name:            agent.agent_name,
      agent_type:            agent.agent_type,
      owner_principal:       agent.owner_principal || "",
      description:           agent.description || "",
      instructions:          agent.instructions || "",
      tools_enabled:         Array.isArray(agent.tools_enabled) ? agent.tools_enabled : [],
      model:        agent.model || "",
      eval_profile: agent.eval_profile || "",
      min_safety_score:      agent.min_safety_score != null ? String(agent.min_safety_score) : "",
      min_correctness_score: agent.min_correctness_score != null ? String(agent.min_correctness_score) : "",
      runtime_mode:          agent.runtime_mode || "config-driven-single-endpoint",
    });
    setSaveError("");
    setSaveOk("");
    setFormTab(tab);
    setActiveTools([]);
    setToolsError("");
    setApprovedModels([]);
    _resetEvalState();
    setShowModal(true);
  }

  function closeModal() {
    if (saving) return;
    setShowModal(false);
  }

  function toggleTool(name: string) {
    setForm((prev) => ({
      ...prev,
      tools_enabled: prev.tools_enabled.includes(name)
        ? prev.tools_enabled.filter((t) => t !== name)
        : [...prev.tools_enabled, name],
    }));
  }

  // ── Eval dataset CRUD ─────────────────────────────────────────
  async function handleEvalAdd() {
    if (!editingAgent || !evalAddForm.request.trim() || !evalAddForm.expected_response.trim()) return;
    setEvalAddSaving(true);
    try {
      const r = await api.post<EvalEntry>(
        `/agents/${encodeURIComponent(editingAgent.agent_id)}/eval-dataset`,
        evalAddForm,
      );
      setEvalEntries((prev) => [...prev, r.data]);
      setEvalAddForm({ request: "", expected_response: "" });
      setEvalAdding(false);
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Erro ao adicionar.";
      setEvalError(detail);
    } finally {
      setEvalAddSaving(false);
    }
  }

  async function handleEvalUpdate(entry_id: string) {
    if (!editingAgent || !evalEditForm.request.trim() || !evalEditForm.expected_response.trim()) return;
    setEvalEditSaving(true);
    try {
      await api.put(
        `/agents/${encodeURIComponent(editingAgent.agent_id)}/eval-dataset/${entry_id}`,
        evalEditForm,
      );
      setEvalEntries((prev) =>
        prev.map((e) => (e.id === entry_id ? { ...e, ...evalEditForm } : e))
      );
      setEvalEditingId(null);
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Erro ao salvar.";
      setEvalError(detail);
    } finally {
      setEvalEditSaving(false);
    }
  }

  async function handleEvalDelete(entry_id: string) {
    if (!editingAgent) return;
    setEvalDeletingId(entry_id);
    try {
      await api.delete(
        `/agents/${encodeURIComponent(editingAgent.agent_id)}/eval-dataset/${entry_id}`,
      );
      setEvalEntries((prev) => prev.filter((e) => e.id !== entry_id));
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Erro ao deletar.";
      setEvalError(detail);
    } finally {
      setEvalDeletingId(null);
    }
  }

  // ── Admin certification ───────────────────────────────────────
  async function handlePromote(agent_id: string, next: AgentStatus, action: "promote" | "disable") {
    setPromoting((p) => ({ ...p, [agent_id]: true }));
    setPromoteMsg((m) => ({ ...m, [agent_id]: "" }));
    try {
      if (action === "promote") {
        await api.post(`/agents/${encodeURIComponent(agent_id)}/promote`);
      } else {
        await api.patch(`/agents/${encodeURIComponent(agent_id)}/status`, { new_status: "disabled" });
      }
      loadAgents();
      setPromoteMsg((m) => ({ ...m, [agent_id]: `→ ${STATUS_LABELS[next]}` }));
      setTimeout(() => setPromoteMsg((m) => ({ ...m, [agent_id]: "" })), 3000);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Erro ao atualizar status.";
      setPromoteMsg((m) => ({ ...m, [agent_id]: `Erro: ${msg}` }));
    } finally {
      setPromoting((p) => ({ ...p, [agent_id]: false }));
    }
  }

  // ── Run eval ──────────────────────────────────────────────────
  async function handleRunEval(agent_id: string) {
    setTriggeringEval((r) => ({ ...r, [agent_id]: true }));
    setEvalRunMsg((m) => ({ ...m, [agent_id]: "" }));
    try {
      await api.post(`/agents/${encodeURIComponent(agent_id)}/run-eval`);
      loadAgents();
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Erro ao disparar avaliação.";
      setEvalRunMsg((m) => ({ ...m, [agent_id]: `Erro: ${msg}` }));
    } finally {
      setTriggeringEval((r) => ({ ...r, [agent_id]: false }));
    }
  }

  // ── Request approval (domain team) ───────────────────────────
  async function handleRequestApproval(agent_id: string) {
    setRequesting((r) => ({ ...r, [agent_id]: true }));
    setRequestMsg((m) => ({ ...m, [agent_id]: "" }));
    try {
      // Check eval dataset before allowing approval request
      const evalResp = await api.get<{ entries: EvalEntry[] }>(
        `/agents/${encodeURIComponent(agent_id)}/eval-dataset`,
      );
      if (!evalResp.data.entries || evalResp.data.entries.length === 0) {
        setEvalWarningAgentId(agent_id);
        return;
      }
      await api.post(`/agents/${encodeURIComponent(agent_id)}/request-approval`);
      loadAgents();
      setRequestMsg((m) => ({ ...m, [agent_id]: "Solicitação enviada" }));
      setTimeout(() => setRequestMsg((m) => ({ ...m, [agent_id]: "" })), 3000);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Erro ao solicitar aprovação.";
      setRequestMsg((m) => ({ ...m, [agent_id]: `Erro: ${msg}` }));
    } finally {
      setRequesting((r) => ({ ...r, [agent_id]: false }));
    }
  }

  // ── Admin: approve / reject request ──────────────────────────
  async function handleReviewApproval(agent_id: string, action: "approve" | "reject") {
    setReviewingApproval((r) => ({ ...r, [agent_id]: true }));
    setReviewMsg((m) => ({ ...m, [agent_id]: "" }));
    try {
      if (action === "approve") {
        const resp = await api.post<{ mlflow_experiment_id?: string | null }>(
          `/agents/${encodeURIComponent(agent_id)}/promote`,
        );
        loadAgents();
        const expNote = resp.data.mlflow_experiment_id ? ` (exp: ${resp.data.mlflow_experiment_id})` : "";
        setReviewMsg((m) => ({ ...m, [agent_id]: `→ Promovido para Staging${expNote}` }));
      } else {
        await api.post(`/agents/${encodeURIComponent(agent_id)}/reject-approval`);
        loadAgents();
        setReviewMsg((m) => ({ ...m, [agent_id]: "Solicitação rejeitada" }));
      }
      setTimeout(() => setReviewMsg((m) => ({ ...m, [agent_id]: "" })), 6000);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Erro ao processar aprovação.";
      setReviewMsg((m) => ({ ...m, [agent_id]: `Erro: ${msg}` }));
    } finally {
      setReviewingApproval((r) => ({ ...r, [agent_id]: false }));
    }
  }

  // ── Delete ────────────────────────────────────────────────────
  async function handleDelete(agent: Agent) {
    if (!window.confirm(`Remover o agente "${agent.agent_name}" (${agent.agent_id})? Esta ação não pode ser desfeita.`)) return;
    setDeleting((d) => ({ ...d, [agent.agent_id]: true }));
    try {
      await api.delete(`/agents/${encodeURIComponent(agent.agent_id)}`);
      loadAgents();
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Erro ao remover agente.";
      alert(msg);
    } finally {
      setDeleting((d) => ({ ...d, [agent.agent_id]: false }));
    }
  }

  // ── Chat ──────────────────────────────────────────────────────
  function openChatModal(agent: Agent) {
    setChatModal({ agent });
    setChatMessages([]);
    setChatInput("");
    setChatError("");
  }

  const sendChatMessage = useCallback(async () => {
    if (!chatInput.trim() || !chatModal || chatLoading) return;
    const userMsg = { role: "user" as const, content: chatInput.trim() };
    const newMessages = [...chatMessages, userMsg];
    setChatMessages(newMessages);
    setChatInput("");
    setChatLoading(true);
    setChatError("");
    try {
      const res = await api.post<{ reply: string }>(
        `/agents/${encodeURIComponent(chatModal.agent.agent_id)}/chat`,
        { messages: newMessages },
      );
      setChatMessages((prev) => [...prev, { role: "assistant", content: res.data.reply }]);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Erro ao chamar o agente.";
      setChatError(msg);
    } finally {
      setChatLoading(false);
    }
  }, [chatInput, chatModal, chatLoading, chatMessages]);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [chatMessages, chatLoading]);

  useEffect(() => {
    if (chatModal) setTimeout(() => chatInputRef.current?.focus(), 50);
  }, [chatModal]);

  // ── Render ────────────────────────────────────────────────────
  return (
    <div className="ag-shell">
      {/* ── Header ── */}
      <div className="ag-header">
        <div className="ag-header-left">
          <h1 className="ag-title">Agents</h1>
          <span className="ag-count">{agents.length} registrados</span>
        </div>

        <div className="ag-tab-bar">
          <button
            className={`ag-tab${activeTab === "agents" ? " ag-tab-active" : ""}`}
            onClick={() => setActiveTab("agents")}
          >
            Todos os Agentes
          </button>
          <button
            className={`ag-tab${activeTab === "admin" ? " ag-tab-active" : ""}`}
            onClick={() => setActiveTab("admin")}
          >
            Admin
            {pendingAgents.length > 0 && (
              <span className="ag-tab-badge">{pendingAgents.length}</span>
            )}
          </button>
        </div>

        <div className="ag-header-right">
          {activeTab === "agents" && (
            <>
              <div className="ag-search-wrap">
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" className="ag-search-icon">
                  <circle cx="6.5" cy="6.5" r="5" stroke="currentColor" strokeWidth="1.5"/>
                  <path d="M10.5 10.5L14 14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
                </svg>
                <input
                  className="ag-search"
                  placeholder="Buscar agentes..."
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              </div>
              <select
                className="ag-filter-select"
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value as AgentStatus | "all")}
              >
                <option value="all">Todos os status</option>
                {(Object.keys(STATUS_LABELS) as AgentStatus[]).map((s) => (
                  <option key={s} value={s}>{STATUS_LABELS[s]}</option>
                ))}
              </select>
              <button className="ag-new-btn" onClick={openModal}>
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                  <path d="M8 2v12M2 8h12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
                </svg>
                Novo Agente
              </button>
            </>
          )}
        </div>
      </div>

      {/* ── Body ── */}
      <div className="ag-body">
        {loading && <div className="ag-state">Carregando agentes...</div>}
        {!loading && error && <div className="ag-error-banner">{error}</div>}

        {/* Agents tab */}
        {activeTab === "agents" && !loading && !error && filtered.length === 0 && (
          <div className="ag-state">
            {agents.length === 0
              ? "Nenhum agente registrado. Clique em \"Novo Agente\" para começar."
              : "Nenhum agente encontrado para os filtros aplicados."}
          </div>
        )}

        {activeTab === "agents" && !loading && !error && filtered.length > 0 && (
          <div className="ag-table-wrap">
            <table className="ag-table">
              <thead>
                <tr>
                  <th>Nome</th>
                  <th>Tipo</th>
                  <th>Status</th>
                  <th>Env</th>
                  <th>Model</th>
                  <th>Tools</th>
                  <th>Owner</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((a) => (
                  <tr key={a.agent_id}>
                    <td className="ag-col-name">
                      <div className="ag-col-name-inner">
                        <span className="ag-agent-name">{a.agent_name}</span>
                        <code className="ag-agent-id">{a.agent_id}</code>
                      </div>
                    </td>
                    <td>
                      <span className={`ag-badge type-${a.agent_type}`}>
                        {AGENT_TYPE_LABELS[a.agent_type] ?? a.agent_type}
                      </span>
                    </td>
                    <td>
                      <span className={`ag-status-badge status-${a.status}`}>
                        {STATUS_LABELS[a.status] ?? a.status}
                      </span>
                    </td>
                    <td>
                      <span className={`ag-env-badge env-${a.environment}`}>{a.environment || "—"}</span>
                    </td>
                    <td className="ag-col-model">
                      <span className="ag-model-text">{a.model || "—"}</span>
                    </td>
                    <td>
                      {Array.isArray(a.tools_enabled) && a.tools_enabled.length > 0
                        ? <span className="ag-tools-count">{a.tools_enabled.length} tool{a.tools_enabled.length !== 1 ? "s" : ""}</span>
                        : "—"}
                    </td>
                    <td className="ag-col-owner">{a.owner_principal}</td>
                    <td className="ag-col-row-action">
                      <div className="ag-col-row-action-inner">
                        <button
                          className="ag-row-action-btn ag-row-chat"
                          onClick={() => openChatModal(a)}
                          title={a.serving_endpoint_name ? `Testar via ${a.serving_endpoint_name}` : "Testar agente"}
                        >
                          Testar
                        </button>
                        {a.status === "draft" && (
                          requestMsg[a.agent_id] ? (
                            <span className={
                              requestMsg[a.agent_id].startsWith("Erro")
                                ? "ag-row-msg ag-row-msg-err"
                                : "ag-row-msg ag-row-msg-ok"
                            }>
                              {requestMsg[a.agent_id]}
                            </span>
                          ) : (
                            <button
                              className="ag-row-action-btn ag-row-request-eval"
                              disabled={!!requesting[a.agent_id]}
                              onClick={() => handleRequestApproval(a.agent_id)}
                              title="Solicitar aprovação para staging"
                            >
                              {requesting[a.agent_id] ? "..." : "Solicitar Aprovação"}
                            </button>
                          )
                        )}
                        <button
                          className={`ag-row-action-btn${a.status === "draft" ? " ag-row-edit" : " ag-row-view"}`}
                          onClick={() => openEditModal(a)}
                        >
                          {a.status === "draft" ? "Editar" : "Ver"}
                        </button>
                        <button
                          className="ag-row-action-btn ag-row-dataset"
                          onClick={() => openEditModal(a, "eval")}
                          title="Gerenciar dataset de teste"
                        >
                          Dataset
                        </button>
                        <button
                          className="ag-row-action-btn ag-row-delete"
                          disabled={!!deleting[a.agent_id]}
                          onClick={() => handleDelete(a)}
                          title="Remover registro"
                        >
                          {deleting[a.agent_id] ? "..." : "Remover"}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* Admin tab */}
        {activeTab === "admin" && !loading && !error && (
          <>
            {/* ── Pedidos Pendentes de Aprovação ── */}
            <div className="ag-admin-section">
              <div className="ag-admin-section-header">
                <h2 className="ag-admin-title">
                  Pedidos Pendentes de Aprovação
                  {pendingAgents.length > 0 && (
                    <span className="ag-admin-count-badge">{pendingAgents.length}</span>
                  )}
                </h2>
                <p className="ag-admin-subtitle">
                  Times de domínio solicitaram aprovação para promover estes agentes ao ambiente staging.
                </p>
              </div>
              {pendingAgents.length === 0 ? (
                <div className="ag-state ag-state-inline">Nenhum pedido pendente.</div>
              ) : (
                <div className="ag-table-wrap">
                  <table className="ag-table">
                    <thead>
                      <tr>
                        <th>Nome</th>
                        <th>Tipo</th>
                        <th>Owner</th>
                        <th>Ações</th>
                      </tr>
                    </thead>
                    <tbody>
                      {pendingAgents.map((a) => (
                        <tr key={a.agent_id}>
                          <td className="ag-col-name">
                            <span className="ag-agent-name">{a.agent_name}</span>
                            <code className="ag-agent-id">{a.agent_id}</code>
                          </td>
                          <td>
                            <span className={`ag-badge type-${a.agent_type}`}>
                              {AGENT_TYPE_LABELS[a.agent_type] ?? a.agent_type}
                            </span>
                          </td>
                          <td className="ag-col-owner">{a.owner_principal}</td>
                          <td className="ag-col-actions">
                            {reviewMsg[a.agent_id] ? (
                              <span className={reviewMsg[a.agent_id].startsWith("Erro") ? "ag-promote-msg ag-promote-err" : "ag-promote-msg ag-promote-ok"}>
                                {reviewMsg[a.agent_id]}
                              </span>
                            ) : (
                              <div className="ag-action-btns">
                                <button
                                  className="ag-action-btn ag-action-approve-req"
                                  disabled={!!reviewingApproval[a.agent_id]}
                                  onClick={() => handleReviewApproval(a.agent_id, "approve")}
                                >
                                  {reviewingApproval[a.agent_id] ? "..." : "Promover para Staging"}
                                </button>
                                <button
                                  className="ag-action-btn ag-action-danger"
                                  disabled={!!reviewingApproval[a.agent_id]}
                                  onClick={() => handleReviewApproval(a.agent_id, "reject")}
                                >
                                  {reviewingApproval[a.agent_id] ? "..." : "Rejeitar"}
                                </button>
                              </div>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            {/* ── Certificação de Agentes ── */}
            <div className="ag-admin-section">
              <div className="ag-admin-section-header">
                <h2 className="ag-admin-title">Certificação de Agentes</h2>
                <p className="ag-admin-subtitle">
                  <span className="ag-flow-step evaluating">Em Avaliação</span>
                  {" → "}
                  <span className="ag-flow-step approved">Aprovado</span>
                </p>
              </div>

              {agents.filter((a) => a.status === "evaluating").length === 0 ? (
                <div className="ag-state ag-state-inline">Nenhum agente em avaliação no momento.</div>
              ) : (
                <div className="ag-table-wrap">
                  <table className="ag-table">
                    <thead>
                      <tr>
                        <th>Nome</th>
                        <th>Tipo</th>
                        <th>Status atual</th>
                        <th>Env</th>
                        <th>Owner</th>
                        <th>Ações</th>
                      </tr>
                    </thead>
                    <tbody>
                      {agents
                        .filter((a) => a.status === "evaluating")
                        .map((a) => (
                          <tr key={a.agent_id}>
                            <td className="ag-col-name">
                              <span className="ag-agent-name">{a.agent_name}</span>
                              <code className="ag-agent-id">{a.agent_id}</code>
                            </td>
                            <td>
                              <span className={`ag-badge type-${a.agent_type}`}>
                                {AGENT_TYPE_LABELS[a.agent_type] ?? a.agent_type}
                              </span>
                            </td>
                            <td>
                              <span className={`ag-status-badge status-${a.status}`}>
                                {STATUS_LABELS[a.status] ?? a.status}
                              </span>
                            </td>
                            <td>
                              <span className={`ag-env-badge env-${a.environment}`}>{a.environment || "—"}</span>
                            </td>
                            <td className="ag-col-owner">{a.owner_principal}</td>
                            <td className="ag-col-actions">
                              <div className="ag-action-btns">
                                {/* MLflow experiment link */}
                                {a.mlflow_url && (
                                  <a
                                    href={a.mlflow_url}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="ag-action-btn ag-action-mlflow"
                                    title="Abrir experimento no Databricks MLflow"
                                  >
                                    MLflow ↗
                                  </a>
                                )}
                                {/* Eval run button / status */}
                                {evalRunMsg[a.agent_id] ? (
                                  <span className="ag-promote-msg ag-promote-err">{evalRunMsg[a.agent_id]}</span>
                                ) : a.eval_status === "running" ? (
                                  <span className="ag-eval-running-badge">⏳ Rodando...</span>
                                ) : (
                                  <button
                                    className="ag-action-btn ag-action-run-eval"
                                    disabled={!!triggeringEval[a.agent_id]}
                                    onClick={() => handleRunEval(a.agent_id)}
                                    title="Rodar experimento de avaliação"
                                  >
                                    {triggeringEval[a.agent_id] ? "..." : a.eval_status === "completed" ? "Re-rodar Experimento" : a.eval_status === "failed" ? "Tentar Novamente" : "Rodar Experimento"}
                                  </button>
                                )}
                                {/* Promote button */}
                                {promoteMsg[a.agent_id] ? (
                                  <span className={promoteMsg[a.agent_id].startsWith("Erro") ? "ag-promote-msg ag-promote-err" : "ag-promote-msg ag-promote-ok"}>
                                    {promoteMsg[a.agent_id]}
                                  </span>
                                ) : (
                                  STATUS_TRANSITIONS[a.status]?.map((t) => (
                                    <button
                                      key={t.next}
                                      className={`ag-action-btn ag-action-${t.variant}`}
                                      disabled={!!promoting[a.agent_id]}
                                      onClick={() => handlePromote(a.agent_id, t.next, t.action)}
                                    >
                                      {promoting[a.agent_id] ? "..." : t.label}
                                    </button>
                                  ))
                                )}
                              </div>
                            </td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </>
        )}
      </div>

      {/* ── Create / Edit / View modal ── */}
      {showModal && (() => {
        const isEditMode = editingAgent !== null;
        const isReadOnly = isEditMode && editingAgent.status !== "draft";
        return (
          <div className="ag-overlay" onClick={closeModal}>
            <div className="ag-modal" onClick={(e) => e.stopPropagation()}>
              <div className="ag-modal-header">
                <div className="ag-modal-header-left">
                  <h2 className="ag-modal-title">
                    {!isEditMode ? "Registrar novo agente" : isReadOnly ? "Ver agente" : "Editar agente"}
                  </h2>
                  {isEditMode && (
                    <span className={`ag-status-badge status-${editingAgent.status} ag-modal-status`}>
                      {STATUS_LABELS[editingAgent.status]}
                    </span>
                  )}
                </div>
                <button className="ag-modal-close" onClick={closeModal} aria-label="Fechar">×</button>
              </div>

              {/* Form tabs */}
              <div className="ag-form-tabs">
                <button
                  type="button"
                  className={`ag-form-tab${formTab === "basic" ? " ag-form-tab-active" : ""}`}
                  onClick={() => setFormTab("basic")}
                >
                  1. Básico
                </button>
                <button
                  type="button"
                  className={`ag-form-tab${formTab === "prompt" ? " ag-form-tab-active" : ""}`}
                  onClick={() => setFormTab("prompt")}
                >
                  2. Prompt & Tools
                </button>
                <button
                  type="button"
                  className={`ag-form-tab${formTab === "eval" ? " ag-form-tab-active" : ""}`}
                  onClick={() => setFormTab("eval")}
                >
                  3. Dataset de Teste
                </button>
              </div>

              <form className="ag-modal-body" onSubmit={handleSave}>
                {saveOk    && <div className="ag-save-ok">{saveOk}</div>}
                {saveError && <div className="ag-save-error">{saveError}</div>}

                {/* Tab 1: Basic */}
                {formTab === "basic" && (
                  <div className="ag-form-section">
                    <div className="ag-form-row">
                      <div className="ag-field ag-field-grow">
                        <label className="ag-label">ID do Agente {!isEditMode && <span className="req">*</span>}</label>
                        <input
                          ref={firstInputRef}
                          className="ag-input"
                          placeholder="hr-policy"
                          value={form.agent_id}
                          onChange={(e) => !isEditMode && setForm({ ...form, agent_id: e.target.value })}
                          readOnly={isEditMode}
                          disabled={isReadOnly}
                          required={!isEditMode}
                        />
                        {!isEditMode && <span className="ag-hint">Identificador único, sem espaços (ex: hr-policy)</span>}
                      </div>
                      <div className="ag-field">
                        <label className="ag-label">Tipo {!isReadOnly && <span className="req">*</span>}</label>
                        <select
                          className="ag-input"
                          value={form.agent_type}
                          onChange={(e) => setForm({ ...form, agent_type: e.target.value as AgentType })}
                          disabled={isReadOnly}
                        >
                          {(Object.keys(AGENT_TYPE_LABELS) as AgentType[]).map((k) => (
                            <option key={k} value={k}>{AGENT_TYPE_LABELS[k]}</option>
                          ))}
                        </select>
                      </div>
                    </div>

                    <div className="ag-field">
                      <label className="ag-label">Nome {!isReadOnly && <span className="req">*</span>}</label>
                      <input
                        className="ag-input"
                        placeholder="Agente de Políticas de RH"
                        value={form.agent_name}
                        onChange={(e) => setForm({ ...form, agent_name: e.target.value })}
                        disabled={isReadOnly}
                        required={!isReadOnly}
                      />
                    </div>

                    <div className="ag-field">
                      <label className="ag-label">Descrição</label>
                      <input
                        className="ag-input"
                        placeholder="O que esse agente faz?"
                        value={form.description}
                        onChange={(e) => setForm({ ...form, description: e.target.value })}
                        disabled={isReadOnly}
                      />
                    </div>

                    <div className="ag-field">
                      <label className="ag-label">Owner (e-mail) {!isReadOnly && <span className="req">*</span>}</label>
                      <input
                        className="ag-input"
                        type="email"
                        placeholder="time-rh@empresa.com"
                        value={form.owner_principal}
                        onChange={(e) => setForm({ ...form, owner_principal: e.target.value })}
                        disabled={isReadOnly}
                        required={!isReadOnly}
                      />
                    </div>

                    <div className="ag-field">
                      <label className="ag-label">Model (LLM)</label>
                      {modelsLoading ? (
                        <div className="ag-tools-loading">Carregando endpoints aprovados...</div>
                      ) : approvedModels.length > 0 ? (
                        <select
                          className="ag-input"
                          value={form.model}
                          onChange={(e) => setForm({ ...form, model: e.target.value })}
                          disabled={isReadOnly}
                        >
                          <option value="">— Selecione um endpoint —</option>
                          {approvedModels.map((m) => (
                            <option key={m.name} value={m.name}>
                              {m.name}{m.state !== "READY" ? ` (${m.state})` : ""}
                              {m.model_name ? ` · ${m.model_name}` : ""}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <input
                          className="ag-input"
                          placeholder="databricks-claude-sonnet-4-6"
                          value={form.model}
                          onChange={(e) => setForm({ ...form, model: e.target.value })}
                          disabled={isReadOnly}
                        />
                      )}
                      <span className="ag-hint">
                        {approvedModels.length === 0 && !modelsLoading
                          ? "Nenhum endpoint aprovado em Settings. Informe o nome manualmente."
                          : "Endpoints aprovados em Settings → Models."}
                      </span>
                    </div>

                  </div>
                )}

                {/* Tab 2: Prompt & Tools */}
                {formTab === "prompt" && (
                  <div className="ag-form-section">
                    <div className="ag-field">
                      <label className="ag-label">System Prompt (instructions)</label>
                      <textarea
                        className="ag-textarea"
                        placeholder="Você é um assistente especializado em políticas de RH..."
                        rows={6}
                        value={form.instructions}
                        onChange={(e) => setForm({ ...form, instructions: e.target.value })}
                        disabled={isReadOnly}
                      />
                      <span className="ag-hint">Instrução inline injetada no prompt. Deixe vazio para usar apenas o Prompt Registry.</span>
                    </div>

                    <div className="ag-field">
                      <label className="ag-label">
                        Tools habilitadas
                        {form.tools_enabled.length > 0 && (
                          <span className="ag-tools-selected-count">{form.tools_enabled.length} selecionada{form.tools_enabled.length !== 1 ? "s" : ""}</span>
                        )}
                      </label>
                      {toolsLoading ? (
                        <div className="ag-tools-loading">Carregando tools aprovadas...</div>
                      ) : toolsError ? (
                        <div className="ag-tools-empty">{toolsError}</div>
                      ) : (
                        <div className="ag-tools-grid">
                          {activeTools.map((tool) => (
                            <label
                              key={tool.tool_name}
                              className={`ag-tool-card${form.tools_enabled.includes(tool.tool_name) ? " ag-tool-card-selected" : ""}`}
                            >
                              <input
                                type="checkbox"
                                className="ag-tool-checkbox"
                                checked={form.tools_enabled.includes(tool.tool_name)}
                                onChange={() => !isReadOnly && toggleTool(tool.tool_name)}
                                disabled={isReadOnly}
                              />
                              <div className="ag-tool-card-body">
                                <span className="ag-tool-card-name">{tool.tool_name}</span>
                                <div className="ag-tool-card-badges">
                                  <span className={`ag-tool-kind-badge kind-${tool.kind}`}>{tool.kind}</span>
                                  {tool.environment && tool.environment !== "dev" && (
                                    <span className={`ag-env-badge env-${tool.environment}`}>{tool.environment}</span>
                                  )}
                                </div>
                                {tool.description && (
                                  <span className="ag-tool-card-desc">{tool.description}</span>
                                )}
                              </div>
                            </label>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                )}

                {/* Tab 3: Dataset de Teste */}
                {formTab === "eval" && (
                  <div className="ag-form-section">
                    {!isEditMode ? (
                      <div className="ag-eval-empty">
                        Crie o agente primeiro para adicionar entradas ao dataset de teste.
                      </div>
                    ) : (<>
                    <div className="ag-eval-header">
                      <span className="ag-eval-hint">
                        Perguntas e respostas esperadas para avaliar o agente com MLflow Eval.
                      </span>
                      {!evalAdding && (
                        <button
                          type="button"
                          className="ag-eval-add-btn"
                          onClick={() => { setEvalAdding(true); setEvalError(""); }}
                        >
                          + Adicionar entrada
                        </button>
                      )}
                    </div>

                    {evalError && <div className="ag-save-error">{evalError}</div>}

                    {evalAdding && (
                      <div className="ag-eval-inline-form">
                        <div className="ag-eval-inline-field">
                          <label className="ag-label">Pergunta <span className="req">*</span></label>
                          <textarea
                            className="ag-textarea ag-eval-textarea"
                            rows={3}
                            placeholder="Ex: Como faço para solicitar férias?"
                            value={evalAddForm.request}
                            onChange={(e) => setEvalAddForm({ ...evalAddForm, request: e.target.value })}
                          />
                        </div>
                        <div className="ag-eval-inline-field">
                          <label className="ag-label">Resposta esperada <span className="req">*</span></label>
                          <textarea
                            className="ag-textarea ag-eval-textarea"
                            rows={3}
                            placeholder="Ex: Acesse o portal de RH e clique em Férias..."
                            value={evalAddForm.expected_response}
                            onChange={(e) => setEvalAddForm({ ...evalAddForm, expected_response: e.target.value })}
                          />
                        </div>
                        <div className="ag-eval-inline-actions">
                          <button
                            type="button"
                            className="ag-cancel-btn"
                            onClick={() => { setEvalAdding(false); setEvalAddForm({ request: "", expected_response: "" }); }}
                            disabled={evalAddSaving}
                          >
                            Cancelar
                          </button>
                          <button
                            type="button"
                            className="ag-submit-btn"
                            onClick={handleEvalAdd}
                            disabled={evalAddSaving || !evalAddForm.request.trim() || !evalAddForm.expected_response.trim()}
                          >
                            {evalAddSaving ? "Salvando..." : "Salvar entrada"}
                          </button>
                        </div>
                      </div>
                    )}

                    {evalLoading ? (
                      <div className="ag-tools-loading">Carregando dataset...</div>
                    ) : evalEntries.length === 0 && !evalAdding ? (
                      <div className="ag-eval-empty">
                        Nenhuma entrada ainda. Clique em "Adicionar entrada" para criar a primeira.
                      </div>
                    ) : (
                      <div className="ag-eval-table">
                        {evalEntries.map((entry, idx) => (
                          <div key={entry.id} className="ag-eval-row">
                            <div className="ag-eval-row-num">{idx + 1}</div>
                            {evalEditingId === entry.id ? (
                              <div className="ag-eval-row-edit">
                                <textarea
                                  className="ag-textarea ag-eval-textarea"
                                  rows={2}
                                  value={evalEditForm.request}
                                  onChange={(e) => setEvalEditForm({ ...evalEditForm, request: e.target.value })}
                                />
                                <textarea
                                  className="ag-textarea ag-eval-textarea"
                                  rows={2}
                                  value={evalEditForm.expected_response}
                                  onChange={(e) => setEvalEditForm({ ...evalEditForm, expected_response: e.target.value })}
                                />
                                <div className="ag-eval-inline-actions">
                                  <button
                                    type="button"
                                    className="ag-cancel-btn"
                                    onClick={() => setEvalEditingId(null)}
                                    disabled={evalEditSaving}
                                  >
                                    Cancelar
                                  </button>
                                  <button
                                    type="button"
                                    className="ag-submit-btn"
                                    onClick={() => handleEvalUpdate(entry.id)}
                                    disabled={evalEditSaving || !evalEditForm.request.trim() || !evalEditForm.expected_response.trim()}
                                  >
                                    {evalEditSaving ? "Salvando..." : "Salvar"}
                                  </button>
                                </div>
                              </div>
                            ) : (
                              <div className="ag-eval-row-content">
                                <div className="ag-eval-cell">
                                  <span className="ag-eval-cell-label">Pergunta</span>
                                  <span className="ag-eval-cell-text">{entry.request}</span>
                                </div>
                                <div className="ag-eval-cell">
                                  <span className="ag-eval-cell-label">Resposta esperada</span>
                                  <span className="ag-eval-cell-text">{entry.expected_response}</span>
                                </div>
                                <div className="ag-eval-row-actions">
                                  <button
                                    type="button"
                                    className="ag-eval-edit-btn"
                                    onClick={() => {
                                      setEvalEditingId(entry.id);
                                      setEvalEditForm({ request: entry.request, expected_response: entry.expected_response });
                                      setEvalError("");
                                    }}
                                  >
                                    Editar
                                  </button>
                                  <button
                                    type="button"
                                    className="ag-eval-del-btn"
                                    onClick={() => handleEvalDelete(entry.id)}
                                    disabled={evalDeletingId === entry.id}
                                  >
                                    {evalDeletingId === entry.id ? "..." : "Excluir"}
                                  </button>
                                </div>
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                    </>)}
                  </div>
                )}

                {/* Footer */}
                <div className="ag-modal-footer">
                  <div className="ag-footer-nav">
                    {formTab !== "basic" && (
                      <button type="button" className="ag-nav-btn" onClick={() => setFormTab(formTab === "eval" ? "prompt" : "basic")}>
                        ← Anterior
                      </button>
                    )}
                    {formTab === "basic" && (
                      <button type="button" className="ag-nav-btn" onClick={() => setFormTab("prompt")}>
                        Próximo →
                      </button>
                    )}
                    {formTab === "prompt" && (
                      <button type="button" className="ag-nav-btn" onClick={() => setFormTab("eval")}>
                        Próximo →
                      </button>
                    )}
                  </div>
                  <div className="ag-footer-actions">
                    <button type="button" className="ag-cancel-btn" onClick={closeModal} disabled={saving}>
                      {isReadOnly ? "Fechar" : "Cancelar"}
                    </button>
                    {!isReadOnly && formTab !== "eval" && (
                      <button
                        type="submit"
                        className="ag-submit-btn"
                        disabled={saving || !form.agent_id.trim() || !form.agent_name.trim() || !form.owner_principal.trim()}
                      >
                        {saving
                          ? (isEditMode ? "Salvando..." : "Criando...")
                          : (isEditMode ? "Salvar alterações" : "Criar Agente")}
                      </button>
                    )}
                  </div>
                </div>
              </form>
            </div>
          </div>
        );
      })()}

      {/* ── Eval warning modal ── */}
      {evalWarningAgentId && (
        <div className="ag-overlay" onClick={() => setEvalWarningAgentId(null)}>
          <div className="ag-modal ag-eval-warn-modal" onClick={(e) => e.stopPropagation()}>
            <div className="ag-modal-header">
              <h2 className="ag-modal-title">Dataset de Teste Vazio</h2>
              <button className="ag-modal-close" onClick={() => setEvalWarningAgentId(null)}>✕</button>
            </div>
            <div className="ag-eval-warn-body">
              <p className="ag-eval-warn-text">
                Preencha o dataset de teste antes de solicitar aprovação.
              </p>
              <p className="ag-eval-warn-hint">
                Adicione pelo menos uma pergunta e resposta esperada na aba "Dataset de Teste" do agente.
              </p>
            </div>
            <div className="ag-eval-warn-actions">
              <button
                className="ag-btn-secondary"
                onClick={() => setEvalWarningAgentId(null)}
              >
                Fechar
              </button>
              <button
                className="ag-btn-primary"
                onClick={() => {
                  const agent = agents.find((a) => a.agent_id === evalWarningAgentId);
                  setEvalWarningAgentId(null);
                  if (agent) openEditModal(agent, "eval");
                }}
              >
                Preencher Dataset
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Chat modal ── */}
      {chatModal && (
        <div className="ag-overlay" onClick={() => setChatModal(null)}>
          <div className="ag-modal ag-chat-modal" onClick={(e) => e.stopPropagation()}>
            <div className="ag-modal-header">
              <div className="ag-modal-header-left">
                <h2 className="ag-modal-title">{chatModal.agent.agent_name}</h2>
                <span className={`ag-env-badge env-${chatModal.agent.environment}`}>
                  {chatModal.agent.environment || "dev"}
                </span>
                {chatModal.agent.serving_endpoint_name && (
                  <code className="ag-chat-model-label">{chatModal.agent.serving_endpoint_name}</code>
                )}
              </div>
              <button className="ag-modal-close" onClick={() => setChatModal(null)} aria-label="Fechar">×</button>
            </div>

            <div className="ag-chat-messages">
              {chatMessages.length === 0 && !chatLoading && (
                <div className="ag-chat-empty">
                  Envie uma mensagem para testar o agente
                </div>
              )}
              {chatMessages.map((msg, i) => (
                <div key={i} className={`ag-chat-msg ag-chat-msg-${msg.role}`}>
                  <span className="ag-chat-role-label">
                    {msg.role === "user" ? "Você" : chatModal.agent.agent_name}
                  </span>
                  <div className="ag-chat-bubble">{msg.content}</div>
                </div>
              ))}
              {chatLoading && (
                <div className="ag-chat-typing">
                  <span className="ag-chat-dots">•••</span>
                </div>
              )}
              <div ref={chatEndRef} />
            </div>

            {chatError && (
              <div className="ag-chat-error">{chatError}</div>
            )}

            <div className="ag-chat-input-wrap">
              <textarea
                ref={chatInputRef}
                className="ag-chat-input"
                placeholder="Digite sua mensagem..."
                rows={1}
                value={chatInput}
                onChange={(e) => setChatInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    void sendChatMessage();
                  }
                }}
                disabled={chatLoading}
              />
              <button
                className="ag-chat-send"
                onClick={() => void sendChatMessage()}
                disabled={chatLoading || !chatInput.trim()}
              >
                {chatLoading ? "..." : "Enviar"}
              </button>
            </div>
          </div>
        </div>
      )}

    </div>
  );
}
