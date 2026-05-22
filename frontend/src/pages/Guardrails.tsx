import { useEffect, useRef, useState } from "react";
import api from "../services/api";
import { useDomain } from "../contexts/DomainContext";
import "./Guardrails.css";

// ── Types ──────────────────────────────────────────────────────
type GuardrailStage = "input" | "output" | "tool";
type GuardrailAction = "block" | "sanitize" | "warn" | "log";

interface Guardrail {
  stage: GuardrailStage;
  name: string;
  action: GuardrailAction;
  params: Record<string, unknown>;
  enabled: boolean;
  priority: number;
  description: string;
  is_default: boolean;
}

interface NewGuardrailForm {
  stage: GuardrailStage;
  name: string;
  action: GuardrailAction;
  params: string;        // raw JSON string in the form
  description: string;
  enabled: boolean;
  priority: number;
}

const EMPTY_FORM: NewGuardrailForm = {
  stage:       "input",
  name:        "",
  action:      "block",
  params:      "{}",
  description: "",
  enabled:     true,
  priority:    0,
};

const STAGE_LABELS: Record<GuardrailStage, string> = {
  input:  "Input",
  output: "Output",
  tool:   "Tool",
};

const ACTION_LABELS: Record<GuardrailAction, string> = {
  block:    "Block",
  sanitize: "Sanitize",
  warn:     "Warn",
  log:      "Log",
};

const ACTION_OPTIONS: GuardrailAction[] = ["block", "sanitize", "warn", "log"];
const STAGE_OPTIONS: GuardrailStage[]   = ["input", "output", "tool"];

// ── Tutorial ───────────────────────────────────────────────────
function Tutorial() {
  const [open, setOpen] = useState(false);
  return (
    <div className="gr-tutorial">
      <button className="gr-tutorial-toggle" onClick={() => setOpen((v) => !v)}>
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
          <circle cx="8" cy="8" r="7" stroke="currentColor" strokeWidth="1.5"/>
          <path d="M6.5 6C6.5 5.17 7.17 4.5 8 4.5s1.5.67 1.5 1.5c0 .7-.4 1.2-1 1.55C8.1 7.84 8 8.2 8 8.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"/>
          <circle cx="8" cy="11" r=".7" fill="currentColor"/>
        </svg>
        Como usar Guardrails
        <svg className={`gr-tutorial-chevron${open ? " open" : ""}`} width="12" height="12" viewBox="0 0 16 16" fill="none">
          <path d="M4 6l4 4 4-4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
      </button>

      {open && (
        <div className="gr-tutorial-body">
          <div className="gr-tut-grid">

            {/* Conceito */}
            <div className="gr-tut-card">
              <div className="gr-tut-card-title">O que são Guardrails?</div>
              <p className="gr-tut-p">
                Guardrails são filtros automáticos que protegem os agentes do seu domínio. Eles inspecionam
                mensagens <strong>antes</strong> de chegarem ao LLM (stage <em>input</em>),
                respostas <strong>antes</strong> de serem devolvidas ao usuário (stage <em>output</em>),
                e chamadas de tools (stage <em>tool</em>).
              </p>
              <p className="gr-tut-p">
                Os 3 defaults pré-configurados são aplicados a <strong>todos os agentes</strong> do domínio e
                não podem ser removidos. Guardrails customizados são adicionados sobre eles.
              </p>
            </div>

            {/* Stages */}
            <div className="gr-tut-card">
              <div className="gr-tut-card-title">Stages de execução</div>
              <div className="gr-tut-items">
                <div className="gr-tut-item">
                  <span className="gr-stage-badge gr-stage-input">Input</span>
                  <span className="gr-tut-item-desc">Avalia a mensagem do usuário antes de enviar ao modelo. Bloqueia antes do LLM processar.</span>
                </div>
                <div className="gr-tut-item">
                  <span className="gr-stage-badge gr-stage-output">Output</span>
                  <span className="gr-tut-item-desc">Avalia a resposta do modelo antes de devolver ao usuário. Permite redação de PII.</span>
                </div>
                <div className="gr-tut-item">
                  <span className="gr-stage-badge gr-stage-tool">Tool</span>
                  <span className="gr-tut-item-desc">Avalia chamadas de tools (nome, argumentos) antes de executá-las.</span>
                </div>
              </div>
            </div>

            {/* Ações */}
            <div className="gr-tut-card">
              <div className="gr-tut-card-title">Ações disponíveis</div>
              <div className="gr-tut-items">
                <div className="gr-tut-item">
                  <span className="gr-action-badge gr-action-block">block</span>
                  <span className="gr-tut-item-desc">Rejeita a mensagem e retorna erro ao usuário. Mais restritivo.</span>
                </div>
                <div className="gr-tut-item">
                  <span className="gr-action-badge gr-action-sanitize">sanitize</span>
                  <span className="gr-tut-item-desc">Redige/substitui conteúdo problemático antes de passar adiante.</span>
                </div>
                <div className="gr-tut-item">
                  <span className="gr-action-badge gr-action-warn">warn</span>
                  <span className="gr-tut-item-desc">Deixa passar mas adiciona aviso no trace do agente.</span>
                </div>
                <div className="gr-tut-item">
                  <span className="gr-action-badge gr-action-log">log</span>
                  <span className="gr-tut-item-desc">Apenas registra o evento sem interferir na execução.</span>
                </div>
              </div>
            </div>

            {/* Criar */}
            <div className="gr-tut-card">
              <div className="gr-tut-card-title">Como criar um guardrail</div>
              <ol className="gr-tut-steps">
                <li>Clique em <strong>+ Novo Guardrail</strong> no canto superior direito.</li>
                <li>Use um dos <strong>guardrails genéricos</strong> abaixo — funcionam apenas via params, sem código extra.</li>
                <li>Defina o <strong>stage</strong> (input / output / tool) e a <strong>ação</strong> a tomar.</li>
                <li>Preencha <strong>Params</strong> com a configuração do guardrail (ver exemplos abaixo).</li>
                <li>Use <strong>Prioridade</strong> para controlar a ordem — menor número executa primeiro.</li>
              </ol>
            </div>

            {/* Guardrails genéricos */}
            <div className="gr-tut-card" style={{gridColumn: "1 / -1"}}>
              <div className="gr-tut-card-title">Guardrails genéricos (configuráveis via interface)</div>
              <div className="gr-tut-items">
                <div className="gr-tut-item">
                  <span className="gr-action-badge gr-action-sanitize" style={{whiteSpace:"nowrap"}}>pattern_redact</span>
                  <span className="gr-tut-item-desc">
                    Redige conteúdo que bate com regex. Stage: <em>output</em>, Ação: <em>sanitize</em>.<br/>
                    Params: <code>{"{"}"pattern": "R\\\\$\\\\s*[\\\\d.,]+", "replacement": "[SALARIO_REDACTED]"{"}"}</code>
                  </span>
                </div>
                <div className="gr-tut-item">
                  <span className="gr-action-badge gr-action-block" style={{whiteSpace:"nowrap"}}>pattern_block</span>
                  <span className="gr-tut-item-desc">
                    Bloqueia se o conteúdo bater com regex. Stage: <em>input</em>, Ação: <em>block</em>.<br/>
                    Params: <code>{"{"}"pattern": "\\\\d{"{"}3{"}"}-\\\\d{"{"}2{"}"}-\\\\d{"{"}4{"}"}", "reason": "SSN não permitido"{"}"}</code>
                  </span>
                </div>
                <div className="gr-tut-item">
                  <span className="gr-action-badge gr-action-block" style={{whiteSpace:"nowrap"}}>keyword_block</span>
                  <span className="gr-tut-item-desc">
                    Bloqueia se o conteúdo contiver qualquer keyword da lista. Stage: <em>input</em>, Ação: <em>block</em>.<br/>
                    Params: <code>{"{"}"keywords": ["senha", "token"], "match_partial": false{"}"}</code>
                  </span>
                </div>
                <div className="gr-tut-item">
                  <span className="gr-action-badge gr-action-sanitize" style={{whiteSpace:"nowrap"}}>keyword_redact</span>
                  <span className="gr-tut-item-desc">
                    Substitui keywords no output por placeholder. Stage: <em>output</em>, Ação: <em>sanitize</em>.<br/>
                    Params: <code>{"{"}"keywords": ["salario", "remuneracao"], "replacement": "[VALOR_REDACTED]"{"}"}</code>
                  </span>
                </div>
              </div>
            </div>

          </div>
        </div>
      )}
    </div>
  );
}

// ── Component ──────────────────────────────────────────────────
export default function Guardrails() {
  const { domain } = useDomain();

  const [guardrails, setGuardrails] = useState<Guardrail[]>([]);
  const [builtinNames, setBuiltinNames] = useState<string[]>([]);
  const [loading, setLoading]         = useState(true);
  const [error, setError]             = useState("");

  const [toggling, setToggling]       = useState<Record<string, boolean>>({});
  const [deleting, setDeleting]       = useState<Record<string, boolean>>({});

  const [showModal, setShowModal]     = useState(false);
  const [editTarget, setEditTarget]   = useState<Guardrail | null>(null);
  const [form, setForm]               = useState<NewGuardrailForm>(EMPTY_FORM);
  const [saving, setSaving]           = useState(false);
  const [saveError, setSaveError]     = useState("");
  const [saveOk, setSaveOk]           = useState("");
  const [paramsError, setParamsError] = useState("");

  const firstInputRef = useRef<HTMLInputElement>(null);

  function rowKey(g: Guardrail) {
    return `${g.stage}::${g.name}`;
  }

  // Load guardrails
  useEffect(() => {
    if (!domain) {
      setGuardrails([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    setError("");
    api
      .get<{ guardrails: Guardrail[]; builtin_names: string[] }>("/guardrails", { params: { domain } })
      .then((res) => {
        setGuardrails(res.data.guardrails);
        setBuiltinNames(res.data.builtin_names);
      })
      .catch((err) => {
        const detail = err?.response?.data?.detail ?? err?.message ?? "Erro desconhecido";
        setError(`Erro ao carregar guardrails: ${detail}`);
      })
      .finally(() => setLoading(false));
  }, [domain]);

  // Focus first input when modal opens
  useEffect(() => {
    if (showModal) setTimeout(() => firstInputRef.current?.focus(), 50);
  }, [showModal]);

  // ── Toggle enabled ────────────────────────────────────────────
  async function handleToggle(g: Guardrail) {
    const key = rowKey(g);
    setToggling((t) => ({ ...t, [key]: true }));
    try {
      await api.patch(
        `/guardrails/${encodeURIComponent(g.stage)}/${encodeURIComponent(g.name)}`,
        { enabled: !g.enabled },
        { params: { domain } },
      );
      setGuardrails((prev) =>
        prev.map((r) => rowKey(r) === key ? { ...r, enabled: !r.enabled } : r)
      );
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Erro.";
      alert(msg);
    } finally {
      setToggling((t) => ({ ...t, [key]: false }));
    }
  }

  // ── Delete ────────────────────────────────────────────────────
  async function handleDelete(g: Guardrail) {
    if (!window.confirm(`Remover guardrail "${g.name}" (${g.stage})?`)) return;
    const key = rowKey(g);
    setDeleting((d) => ({ ...d, [key]: true }));
    try {
      await api.delete(
        `/guardrails/${encodeURIComponent(g.stage)}/${encodeURIComponent(g.name)}`,
        { params: { domain } },
      );
      setGuardrails((prev) => prev.filter((r) => rowKey(r) !== key));
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Erro.";
      alert(msg);
    } finally {
      setDeleting((d) => ({ ...d, [key]: false }));
    }
  }

  // ── Edit guardrail ────────────────────────────────────────────
  function openEditModal(g: Guardrail) {
    setEditTarget(g);
    setForm({
      stage:       g.stage,
      name:        g.name,
      action:      g.action,
      params:      JSON.stringify(g.params, null, 2),
      description: g.description,
      enabled:     g.enabled,
      priority:    g.priority,
    });
    setSaveError("");
    setSaveOk("");
    setParamsError("");
    setShowModal(true);
  }

  async function handleUpdate(e: React.FormEvent) {
    e.preventDefault();
    if (!editTarget) return;
    setParamsError("");
    const parsedParams = validateParams(form.params);
    if (parsedParams === null) {
      setParamsError("JSON inválido. Use o formato {\"chave\": \"valor\"}");
      return;
    }
    setSaving(true);
    setSaveError("");
    setSaveOk("");
    try {
      await api.put(
        `/guardrails/${encodeURIComponent(editTarget.stage)}/${encodeURIComponent(editTarget.name)}`,
        {
          action:      form.action,
          params:      parsedParams,
          description: form.description.trim(),
          enabled:     form.enabled,
          priority:    form.priority,
        },
        { params: { domain } },
      );
      const res = await api.get<{ guardrails: Guardrail[] }>("/guardrails", { params: { domain } });
      setGuardrails(res.data.guardrails);
      setSaveOk(`Guardrail "${editTarget.name}" atualizado.`);
      setTimeout(() => { setShowModal(false); setSaveOk(""); setEditTarget(null); }, 1400);
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Erro ao salvar guardrail.";
      setSaveError(msg);
    } finally {
      setSaving(false);
    }
  }

  // ── Create guardrail ──────────────────────────────────────────
  function validateParams(raw: string): Record<string, unknown> | null {
    try {
      const parsed = JSON.parse(raw || "{}");
      if (typeof parsed !== "object" || Array.isArray(parsed)) return null;
      return parsed;
    } catch {
      return null;
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setParamsError("");
    const parsedParams = validateParams(form.params);
    if (parsedParams === null) {
      setParamsError("JSON inválido. Use o formato {\"chave\": \"valor\"}");
      return;
    }
    setSaving(true);
    setSaveError("");
    setSaveOk("");
    try {
      await api.post(
        "/guardrails",
        {
          stage:       form.stage,
          name:        form.name.trim(),
          action:      form.action,
          params:      parsedParams,
          description: form.description.trim(),
          enabled:     form.enabled,
          priority:    form.priority,
        },
        { params: { domain } },
      );
      const res = await api.get<{ guardrails: Guardrail[] }>("/guardrails", { params: { domain } });
      setGuardrails(res.data.guardrails);
      setSaveOk(`Guardrail "${form.name}" criado.`);
      setForm(EMPTY_FORM);
      setTimeout(() => { setShowModal(false); setSaveOk(""); }, 1600);
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Erro ao criar guardrail.";
      setSaveError(msg);
    } finally {
      setSaving(false);
    }
  }

  function openModal() {
    setEditTarget(null);
    setForm(EMPTY_FORM);
    setSaveError("");
    setSaveOk("");
    setParamsError("");
    setShowModal(true);
  }

  function closeModal() {
    if (saving) return;
    setShowModal(false);
    setEditTarget(null);
  }

  // ── Group by stage ────────────────────────────────────────────
  const byStage: Record<string, Guardrail[]> = { input: [], output: [], tool: [] };
  for (const g of guardrails) {
    (byStage[g.stage] ??= []).push(g);
  }

  const customCount = guardrails.filter((g) => !g.is_default).length;

  // ── Render ────────────────────────────────────────────────────
  return (
    <div className="gr-shell">
      {/* ── Header ── */}
      <div className="gr-header">
        <div className="gr-header-left">
          <h1 className="gr-title">Guardrails</h1>
          <span className="gr-count">
            {guardrails.length} total · {customCount} customizados
          </span>
        </div>
        <div className="gr-header-right">
          <button className="gr-new-btn" onClick={openModal} disabled={!domain}>
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
              <path d="M8 2v12M2 8h12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
            </svg>
            Novo Guardrail
          </button>
        </div>
      </div>

      {/* ── Info box ── */}
      <div className="gr-info-box">
        <svg className="gr-info-icon" width="16" height="16" viewBox="0 0 16 16" fill="none">
          <circle cx="8" cy="8" r="7" stroke="currentColor" strokeWidth="1.5"/>
          <path d="M8 7v5M8 5v.01" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
        </svg>
        <div className="gr-info-text">
          <strong>Guardrails</strong> são interceptores que avaliam mensagens antes de chegarem ao LLM (input)
          e respostas antes de serem devolvidas ao usuário (output). Defaults são aplicados a todos os agentes
          do domínio e não podem ser removidos. Guardrails customizados são adicionados sobre os defaults.
        </div>
      </div>

      {/* ── Tutorial ── */}
      <Tutorial />

      {/* ── Body ── */}
      <div className="gr-body">
        {!domain && (
          <div className="gr-state">Selecione um domínio no menu superior para ver os guardrails.</div>
        )}

        {domain && loading && <div className="gr-state">Carregando guardrails...</div>}

        {domain && !loading && error && (
          <div className="gr-error-banner">{error}</div>
        )}

        {domain && !loading && !error && (
          <>
            {STAGE_OPTIONS.map((stage) => {
              const rows = byStage[stage] || [];
              return (
                <div key={stage} className="gr-section">
                  <div className="gr-section-header">
                    <span className={`gr-stage-badge gr-stage-${stage}`}>{STAGE_LABELS[stage]}</span>
                    <span className="gr-section-count">{rows.length} guardrails</span>
                  </div>

                  {rows.length === 0 ? (
                    <div className="gr-section-empty">Nenhum guardrail configurado para este estágio.</div>
                  ) : (
                    <div className="gr-table-wrap">
                      <table className="gr-table">
                        <thead>
                          <tr>
                            <th>Nome</th>
                            <th>Ação</th>
                            <th>Prioridade</th>
                            <th>Descrição</th>
                            <th>Tipo</th>
                            <th>Status</th>
                            <th>Ações</th>
                          </tr>
                        </thead>
                        <tbody>
                          {rows.map((g) => {
                            const key = rowKey(g);
                            const isLoading = toggling[key] || deleting[key];
                            return (
                              <tr key={key} className={!g.enabled ? "gr-row-disabled" : ""}>
                                <td>
                                  <span className="gr-name">{g.name}</span>
                                </td>
                                <td>
                                  <span className={`gr-action-badge gr-action-${g.action}`}>
                                    {ACTION_LABELS[g.action] ?? g.action}
                                  </span>
                                </td>
                                <td className="gr-col-priority">{g.priority}</td>
                                <td className="gr-col-desc">
                                  <span className="gr-desc">{g.description || "—"}</span>
                                </td>
                                <td>
                                  {g.is_default ? (
                                    <span className="gr-type-badge gr-type-default">Default</span>
                                  ) : (
                                    <span className="gr-type-badge gr-type-custom">Custom</span>
                                  )}
                                </td>
                                <td>
                                  {g.enabled ? (
                                    <span className="gr-status-badge gr-status-enabled">Ativo</span>
                                  ) : (
                                    <span className="gr-status-badge gr-status-disabled">Desativado</span>
                                  )}
                                </td>
                                <td className="gr-col-actions">
                                  {g.is_default ? (
                                    <span className="gr-readonly-hint">Não editável</span>
                                  ) : (
                                    <div className="gr-action-btns">
                                      <button
                                        className="gr-edit-btn"
                                        disabled={isLoading}
                                        onClick={() => openEditModal(g)}
                                        title="Editar guardrail"
                                      >
                                        Editar
                                      </button>
                                      <button
                                        className={`gr-toggle-btn ${g.enabled ? "gr-toggle-disable" : "gr-toggle-enable"}`}
                                        disabled={isLoading}
                                        onClick={() => handleToggle(g)}
                                      >
                                        {toggling[key] ? "..." : g.enabled ? "Desativar" : "Ativar"}
                                      </button>
                                      <button
                                        className="gr-delete-btn"
                                        disabled={isLoading}
                                        onClick={() => handleDelete(g)}
                                      >
                                        {deleting[key] ? "..." : "Remover"}
                                      </button>
                                    </div>
                                  )}
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              );
            })}
          </>
        )}
      </div>

      {/* ── Guardrail modal (create / edit) ── */}
      {showModal && (
        <div className="gr-overlay" onClick={closeModal}>
          <div className="gr-modal" onClick={(e) => e.stopPropagation()}>
            <div className="gr-modal-header">
              <h2 className="gr-modal-title">
                {editTarget ? `Editar: ${editTarget.name}` : "Novo Guardrail"}
              </h2>
              <button className="gr-modal-close" onClick={closeModal} aria-label="Fechar">×</button>
            </div>

            <form className="gr-modal-body" onSubmit={editTarget ? handleUpdate : handleCreate}>
              {saveOk    && <div className="gr-save-ok">{saveOk}</div>}
              {saveError && <div className="gr-save-error">{saveError}</div>}

              <div className="gr-form-row">
                <div className="gr-field gr-field-grow">
                  <label className="gr-label">Nome {!editTarget && <span className="req">*</span>}</label>
                  {editTarget ? (
                    <div className="gr-input gr-input-readonly">{form.name}</div>
                  ) : (
                    <>
                      <input
                        ref={firstInputRef}
                        className="gr-input"
                        placeholder="ex: topic_filter"
                        list="gr-builtin-names"
                        value={form.name}
                        onChange={(e) => setForm({ ...form, name: e.target.value })}
                        required
                      />
                      <datalist id="gr-builtin-names">
                        {builtinNames.map((n) => <option key={n} value={n} />)}
                      </datalist>
                      <span className="gr-field-hint">
                        Deve corresponder a um guardrail registrado no runtime.
                      </span>
                    </>
                  )}
                </div>

                <div className="gr-field">
                  <label className="gr-label">Estágio {!editTarget && <span className="req">*</span>}</label>
                  {editTarget ? (
                    <div className="gr-input gr-input-readonly">
                      <span className={`gr-stage-badge gr-stage-${form.stage}`}>{STAGE_LABELS[form.stage]}</span>
                    </div>
                  ) : (
                    <select
                      className="gr-input"
                      value={form.stage}
                      onChange={(e) => setForm({ ...form, stage: e.target.value as GuardrailStage })}
                    >
                      {STAGE_OPTIONS.map((s) => (
                        <option key={s} value={s}>{STAGE_LABELS[s]}</option>
                      ))}
                    </select>
                  )}
                </div>
              </div>

              <div className="gr-form-row">
                <div className="gr-field">
                  <label className="gr-label">Ação <span className="req">*</span></label>
                  <select
                    ref={editTarget ? (firstInputRef as unknown as React.RefObject<HTMLSelectElement>) : undefined}
                    className="gr-input"
                    value={form.action}
                    onChange={(e) => setForm({ ...form, action: e.target.value as GuardrailAction })}
                  >
                    {ACTION_OPTIONS.map((a) => (
                      <option key={a} value={a}>{ACTION_LABELS[a]}</option>
                    ))}
                  </select>
                </div>

                <div className="gr-field">
                  <label className="gr-label">Prioridade</label>
                  <input
                    className="gr-input"
                    type="number"
                    min={0}
                    max={100}
                    value={form.priority}
                    onChange={(e) => setForm({ ...form, priority: parseInt(e.target.value) || 0 })}
                  />
                </div>
              </div>

              <div className="gr-field">
                <label className="gr-label">Params (JSON)</label>
                <textarea
                  className={`gr-input gr-textarea${paramsError ? " gr-input-error" : ""}`}
                  placeholder='{"allowed_topics": ["rh", "beneficios"]}'
                  value={form.params}
                  onChange={(e) => { setForm({ ...form, params: e.target.value }); setParamsError(""); }}
                  rows={4}
                />
                {paramsError && <span className="gr-field-error">{paramsError}</span>}
                <span className="gr-field-hint">
                  Objeto JSON com parâmetros para o guardrail (ex: <code>{"allowed_topics"}</code> para topic_filter).
                </span>
              </div>

              <div className="gr-field">
                <label className="gr-label">Descrição</label>
                <input
                  className="gr-input"
                  placeholder="O que este guardrail faz?"
                  value={form.description}
                  onChange={(e) => setForm({ ...form, description: e.target.value })}
                />
              </div>

              <div className="gr-field gr-field-inline">
                <label className="gr-label gr-label-inline">
                  <input
                    type="checkbox"
                    className="gr-checkbox"
                    checked={form.enabled}
                    onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
                  />
                  Ativo
                </label>
              </div>
            </form>

            <div className="gr-modal-footer">
              <button type="button" className="gr-cancel-btn" onClick={closeModal} disabled={saving}>
                Cancelar
              </button>
              <button
                type="submit"
                form=""
                className="gr-submit-btn"
                disabled={saving || (!editTarget && !form.name.trim())}
                onClick={editTarget ? handleUpdate : handleCreate}
              >
                {saving
                  ? (editTarget ? "Salvando..." : "Criando...")
                  : (editTarget ? "Salvar alterações" : "Criar guardrail")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
