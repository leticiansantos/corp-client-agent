import { useEffect, useRef, useState } from "react";
import api from "../services/api";
import "./Settings.css";

// ── Types ──────────────────────────────────────────────────────
type Env = "dev" | "staging" | "prod";
type ModelApprovalStatus = "approved" | "rejected" | "pending";

interface ModelEntry {
  name: string;
  env: Env;
  state: string;
  model_name: string;
  creator: string;
  approval_status: ModelApprovalStatus;
  notes: string;
  updated_at: string | null;
}

interface DomainEnvConfig {
  env: Env;
  workspace_url: string;
  token: string;
  notes: string;
  updated_at: string | null;
}

interface Domain {
  domain: string;
  envs: DomainEnvConfig[];
}

interface EndpointStatus {
  domain: string;
  env: Env;
  endpoint_name: string;
  endpoint_url: string;
  state: "READY" | "NOT_READY" | "NOT_FOUND" | "NOT_CONFIGURED" | "ERROR";
  deploying: boolean;
  deploy_step: string;
  deploy_step_index: number;
  deploy_error?: string;
  deploy_run_url?: string;
  error?: string;
}

const DEPLOY_STEPS = [
  "Criar catalog",
  "Criar schema",
  "Criar volume para libs",
  "Build WHL do corp_agent_framework",
  "Upload WHL para Volume",
  "Verificar endpoint existente",
  "Upload notebook de deploy",
  "Submeter job de deploy",
  "Aguardar conclusão do deploy",
];

const ENV_META: Record<Env, { label: string; desc: string }> = {
  dev:     { label: "Dev",     desc: "Desenvolvimento" },
  staging: { label: "Staging", desc: "Pré-produção" },
  prod:    { label: "Prod",    desc: "Produção" },
};

const ENVS: Env[] = ["dev", "staging", "prod"];
const DOMAIN_RE = /^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$/;

function epKey(domain: string, env: Env): string {
  return `${domain}::${env}`;
}

function detectCloud(url: string): { label: string; key: string } | null {
  if (!url) return null;
  if (url.includes("azuredatabricks.net")) return { label: "Azure", key: "azure" };
  if (url.includes("gcp.databricks.com"))  return { label: "GCP",   key: "gcp"   };
  if (url.includes("cloud.databricks.com")) return { label: "AWS",   key: "aws"   };
  return null;
}

const EMPTY_DOMAIN_ENV = (env: Env): DomainEnvConfig => ({
  env,
  workspace_url: "",
  token: "",
  notes: "",
  updated_at: null,
});

// ── Component ──────────────────────────────────────────────────
export default function Settings() {
  // Domains
  const [domains, setDomains]     = useState<Domain[]>([]);
  const [expanded, setExpanded]   = useState<Set<string>>(new Set());
  const [loading, setLoading]     = useState(true);
  const [loadError, setLoadError] = useState("");

  // New domain
  const [showNewDomain, setShowNewDomain]   = useState(false);
  const [newDomainName, setNewDomainName]   = useState("");
  const [newDomainError, setNewDomainError] = useState("");

  // Saving per env
  const [saving, setSaving]   = useState<Record<string, boolean>>({});
  const [saveMsg, setSaveMsg] = useState<Record<string, string>>({});

  // Endpoint status
  const [epStatus, setEpStatus]       = useState<Record<string, EndpointStatus>>({});
  const [epLoading, setEpLoading]     = useState(true);
  const [epError, setEpError]         = useState("");
  const [epDeploying, setEpDeploying] = useState<Record<string, boolean>>({});
  const epPollingRef                  = useRef<ReturnType<typeof setInterval> | null>(null);

  // Models approval
  const [models, setModels]               = useState<ModelEntry[]>([]);
  const [modelsLoading, setModelsLoading] = useState(true);
  const [modelsError, setModelsError]     = useState("");
  const [approving, setApproving]         = useState<Record<string, boolean>>({});
  const [approveMsg, setApproveMsg]       = useState<Record<string, string>>({});
  const [selected, setSelected]           = useState<Set<string>>(new Set());
  const [bulkApproving, setBulkApproving] = useState(false);
  const [modelsPage, setModelsPage]       = useState(1);
  const PAGE_SIZE = 10;

  // ── Domains load ─────────────────────────────────────────────
  useEffect(() => {
    api
      .get<{ domains: Domain[] }>("/settings/domains")
      .then((r) => setDomains(r.data.domains))
      .catch((err) => {
        const detail = err?.response?.data?.detail ?? err?.message ?? "Erro desconhecido";
        setLoadError(`Erro ao carregar domínios: ${detail}`);
      })
      .finally(() => setLoading(false));
  }, []);

  // ── Framework endpoints ──────────────────────────────────────
  function _applyEpResponse(endpoints: EndpointStatus[]) {
    const byKey: Record<string, EndpointStatus> = {};
    for (const ep of endpoints) byKey[epKey(ep.domain, ep.env)] = ep;
    setEpStatus(byKey);
    setEpDeploying({});
    return endpoints.some((ep) => ep.deploying);
  }

  function loadFrameworkEndpoints() {
    setEpLoading(true);
    setEpError("");
    api
      .get<{ endpoints: EndpointStatus[] }>("/settings/framework-endpoints")
      .then((r) => {
        const anyDeploying = _applyEpResponse(r.data.endpoints);
        if (anyDeploying) _startEpPolling(1_000); else _stopEpPolling();
      })
      .catch((err) => {
        const detail = err?.response?.data?.detail ?? err?.message ?? "Erro desconhecido";
        setEpError(`Erro ao carregar endpoints: ${detail}`);
      })
      .finally(() => setEpLoading(false));
  }

  function _startEpPolling(interval = 3_000) {
    // Restart polling if a faster interval is requested
    if (epPollingRef.current !== null) {
      clearInterval(epPollingRef.current);
      epPollingRef.current = null;
    }
    epPollingRef.current = setInterval(() => {
      api
        .get<{ endpoints: EndpointStatus[] }>("/settings/framework-endpoints")
        .then((r) => {
          const anyDeploying = _applyEpResponse(r.data.endpoints);
          if (!anyDeploying) _stopEpPolling();
        })
        .catch(() => {/* keep polling */});
    }, interval);
  }

  function _stopEpPolling() {
    if (epPollingRef.current !== null) { clearInterval(epPollingRef.current); epPollingRef.current = null; }
  }

  useEffect(() => { loadFrameworkEndpoints(); return () => _stopEpPolling(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function handleEnvDeploy(domain: string, env: Env) {
    const key = epKey(domain, env);
    setEpDeploying((p) => ({ ...p, [key]: true }));
    try {
      await api.post(`/settings/domains/${encodeURIComponent(domain)}/envs/${env}/deploy`);
      _startEpPolling(1_000);
    } catch (err: unknown) {
      const status = (err as { response?: { status?: number } })?.response?.status;
      if (status === 409) {
        // Already deploying — just start polling to reflect the running job
        _startEpPolling(1_000);
      } else {
        const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Erro ao iniciar deploy.";
        alert(detail);
        setEpDeploying((p) => ({ ...p, [key]: false }));
      }
    }
  }

  async function handleDeployAll(domain: string) {
    for (const env of ENVS) await handleEnvDeploy(domain, env);
  }

  // ── Domain CRUD ──────────────────────────────────────────────
  function toggleExpand(domain: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(domain) ? next.delete(domain) : next.add(domain);
      return next;
    });
  }

  function handleAddDomain() {
    const name = newDomainName.trim().toLowerCase();
    if (!DOMAIN_RE.test(name)) {
      setNewDomainError("Use apenas letras minúsculas, números e hífens (sem hífens no início/fim).");
      return;
    }
    if (domains.some((d) => d.domain === name)) {
      setNewDomainError("Domínio já existe.");
      return;
    }
    setDomains((prev) => [...prev, { domain: name, envs: ENVS.map(EMPTY_DOMAIN_ENV) }]);
    setExpanded((prev) => new Set([...prev, name]));
    setNewDomainName("");
    setNewDomainError("");
    setShowNewDomain(false);
  }

  async function handleDeleteDomain(domain: string) {
    if (!confirm(`Remover domínio "${domain}" e todas suas configurações?`)) return;
    try {
      await api.delete(`/settings/domains/${encodeURIComponent(domain)}`);
      setDomains((prev) => prev.filter((d) => d.domain !== domain));
      setExpanded((prev) => { const n = new Set(prev); n.delete(domain); return n; });
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Erro ao remover domínio.";
      alert(detail);
    }
  }

  // ── Env config field update ──────────────────────────────────
  function updateEnvField(domain: string, env: Env, field: keyof DomainEnvConfig, value: string) {
    setDomains((prev) =>
      prev.map((d) =>
        d.domain !== domain ? d : {
          ...d,
          envs: d.envs.map((e) => e.env !== env ? e : { ...e, [field]: value }),
        }
      )
    );
  }

  async function handleSaveEnv(domain: string, env: Env) {
    const key = epKey(domain, env);
    const envCfg = domains.find((d) => d.domain === domain)?.envs.find((e) => e.env === env);
    if (!envCfg) return;
    setSaving((p) => ({ ...p, [key]: true }));
    setSaveMsg((p) => ({ ...p, [key]: "" }));
    try {
      await api.put(`/settings/domains/${encodeURIComponent(domain)}/envs/${env}`, envCfg);
      setSaveMsg((p) => ({ ...p, [key]: "ok" }));
      setTimeout(() => setSaveMsg((p) => ({ ...p, [key]: "" })), 3000);
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Erro ao salvar.";
      setSaveMsg((p) => ({ ...p, [key]: `error:${msg}` }));
    } finally {
      setSaving((p) => ({ ...p, [key]: false }));
    }
  }

  // ── Model approvals ──────────────────────────────────────────
  function loadModels() {
    setModelsLoading(true);
    setModelsError("");
    api
      .get<{ models: ModelEntry[] }>("/settings/models")
      .then((r) => { setModels(r.data.models); setModelsPage(1); })
      .catch((err) => {
        const detail = err?.response?.data?.detail ?? err?.message ?? "Erro desconhecido";
        setModelsError(`Erro ao carregar modelos: ${detail}`);
      })
      .finally(() => setModelsLoading(false));
  }

  useEffect(loadModels, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function handleModelStatus(modelName: string, newStatus: ModelApprovalStatus) {
    setApproving((p) => ({ ...p, [modelName]: true }));
    setApproveMsg((m) => ({ ...m, [modelName]: "" }));
    try {
      await api.patch(`/settings/models/${encodeURIComponent(modelName)}/status`, { new_status: newStatus });
      setModels((prev) => prev.map((m) => m.name === modelName ? { ...m, approval_status: newStatus } : m));
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Erro ao atualizar status.";
      setApproveMsg((m) => ({ ...m, [modelName]: msg }));
    } finally {
      setApproving((p) => ({ ...p, [modelName]: false }));
    }
  }

  function toggleSelect(name: string) {
    setSelected((prev) => { const next = new Set(prev); next.has(name) ? next.delete(name) : next.add(name); return next; });
  }

  function toggleSelectAll() {
    setSelected(selected.size === models.length ? new Set() : new Set(models.map((m) => m.name)));
  }

  async function handleBulkStatus(newStatus: ModelApprovalStatus) {
    setBulkApproving(true);
    await Promise.allSettled(
      Array.from(selected).map((name) =>
        api.patch(`/settings/models/${encodeURIComponent(name)}/status`, { new_status: newStatus })
          .then(() => setModels((prev) => prev.map((m) => m.name === name ? { ...m, approval_status: newStatus } : m)))
      )
    );
    setSelected(new Set());
    setBulkApproving(false);
  }

  // ── Render ───────────────────────────────────────────────────
  return (
    <div className="st-shell">
      <div className="st-header">
        <div className="st-header-left">
          <h1 className="st-title">Configurações</h1>
          <span className="st-subtitle">Domínios e ambientes de execução do corp_agent_framework</span>
        </div>
      </div>

      <div className="st-body">

        {/* ── Domains ──────────────────────────────────────── */}
        <div className="st-section">
          <div className="st-fw-header">
            <div>
              <h2 className="st-section-title">Domínios</h2>
              <p className="st-section-desc">
                Cada domínio possui 3 ambientes com seu próprio workspace Databricks. O endpoint gerado
                segue o padrão{" "}
                <code className="st-inline-code">corp-config-driven-agent-{"{dominio}"}-{"{env}"}</code>.
              </p>
            </div>
            <button
              className="st-save-btn"
              type="button"
              onClick={() => { setShowNewDomain(true); setNewDomainName(""); setNewDomainError(""); }}
            >
              + Novo Domínio
            </button>
          </div>

          {epError && <div className="st-error-banner">{epError}</div>}
          {loading && <div className="st-state">Carregando domínios...</div>}
          {!loading && loadError && <div className="st-error-banner">{loadError}</div>}

          {/* New domain row */}
          {showNewDomain && (
            <div className="st-new-domain-row">
              <input
                className="st-input"
                style={{ maxWidth: 260 }}
                placeholder="nome-do-dominio"
                value={newDomainName}
                autoFocus
                onChange={(e) => { setNewDomainName(e.target.value.toLowerCase()); setNewDomainError(""); }}
                onKeyDown={(e) => { if (e.key === "Enter") handleAddDomain(); if (e.key === "Escape") setShowNewDomain(false); }}
              />
              <button className="st-save-btn" type="button" onClick={handleAddDomain}>Adicionar</button>
              <button className="st-refresh-btn" type="button" onClick={() => setShowNewDomain(false)}>Cancelar</button>
              {newDomainError && <span className="st-save-error">{newDomainError}</span>}
            </div>
          )}

          {!loading && !loadError && domains.length === 0 && !showNewDomain && (
            <div className="st-state">Nenhum domínio configurado. Clique em "+ Novo Domínio" para começar.</div>
          )}

          <div className="st-domain-list">
            {domains.map((d) => {
              const isExpanded = expanded.has(d.domain);
              const dots = ENVS.map((env) => {
                const key = epKey(d.domain, env);
                const ep = epStatus[key];
                if (epDeploying[key] || ep?.deploying) return "deploying";
                if (!ep) return "unknown";
                if (ep.state === "READY") return "ready";
                if (ep.state === "ERROR") return "error";
                return "notfound";
              });

              return (
                <div key={d.domain} className="st-domain-card">
                  {/* Domain header row — entirely clickable to expand/collapse */}
                  <button
                    className="st-domain-header"
                    type="button"
                    onClick={() => toggleExpand(d.domain)}
                  >
                    <span className="st-expand-btn" aria-hidden>
                      {isExpanded ? "▾" : "▸"}
                    </span>

                    <span className="st-domain-name">{d.domain}</span>

                    <div className="st-domain-env-dots">
                      {ENVS.map((env, i) => (
                        <span
                          key={env}
                          className={`st-dot st-dot-${dots[i]}`}
                          title={`${env}: ${dots[i]}`}
                        />
                      ))}
                    </div>

                    <button
                      className="st-deploy-all-btn"
                      type="button"
                      disabled={epLoading}
                      onClick={(e) => { e.stopPropagation(); handleDeployAll(d.domain); }}
                    >
                      Deploy Todos
                    </button>

                    <button
                      className="st-delete-btn"
                      type="button"
                      title="Remover domínio"
                      onClick={(e) => { e.stopPropagation(); handleDeleteDomain(d.domain); }}
                    >
                      ✕
                    </button>
                  </button>

                  {/* Expanded: 3 env cards side by side */}
                  {isExpanded && (
                    <div className="st-domain-envs">
                      {ENVS.map((env) => {
                        const key = epKey(d.domain, env);
                        const envCfg = d.envs.find((e) => e.env === env) ?? EMPTY_DOMAIN_ENV(env);
                        const ep = epStatus[key];
                        const isDeploying = !!epDeploying[key] || !!ep?.deploying;
                        const cloud = detectCloud(envCfg.workspace_url);
                        const msgVal = saveMsg[key] ?? "";
                        const isOk  = msgVal === "ok";
                        const isErr = msgVal.startsWith("error:");
                        const endpointName = `corp-config-driven-agent-${d.domain}-${env}`;

                        return (
                          <div key={env} className={`st-env-card st-env-${env}`}>
                            <div className="st-env-card-header">
                              <span className={`st-env-badge st-env-badge-${env}`}>{ENV_META[env].label}</span>
                              {cloud && (
                                <span className={`st-cloud-badge st-cloud-${cloud.key}`}>{cloud.label}</span>
                              )}
                              <span className={`st-fw-badge st-fw-badge-${
                                isDeploying                      ? "deploying"
                                : ep?.state === "READY"          ? "ready"
                                : ep?.state === "NOT_READY"      ? "notready"
                                : ep?.state === "NOT_FOUND"      ? "notfound"
                                : ep?.state === "NOT_CONFIGURED" ? "notconfigured"
                                : ep?.state === "ERROR"          ? "error"
                                : "unknown"
                              }`}>
                                {isDeploying ? "Deployando..."
                                  : ep?.state === "READY"          ? "Ready"
                                  : ep?.state === "NOT_READY"      ? "Not Ready"
                                  : ep?.state === "NOT_FOUND"      ? "Não criado"
                                  : ep?.state === "NOT_CONFIGURED" ? "Não configurado"
                                  : ep?.state === "ERROR"          ? "Erro"
                                  : epLoading ? <span className="st-badge-spinner" />
                                  : "—"}
                              </span>
                            </div>

                            <div className="st-fw-field">
                              <span className="st-label">Endpoint</span>
                              <code className="st-fw-code">{endpointName}</code>
                            </div>

                            {ep?.endpoint_url && (
                              <div className="st-fw-field">
                                <span className="st-label">URL de invocação</span>
                                <a href={ep.endpoint_url} target="_blank" rel="noreferrer" className="st-fw-url" title={ep.endpoint_url}>
                                  {ep.endpoint_url}
                                </a>
                              </div>
                            )}

                            <div className="st-fields">
                              <div className="st-field">
                                <label className="st-label">Workspace URL</label>
                                <input
                                  className="st-input"
                                  placeholder="https://adb-xxxx.azuredatabricks.net"
                                  value={envCfg.workspace_url}
                                  onChange={(e) => updateEnvField(d.domain, env, "workspace_url", e.target.value)}
                                />
                              </div>
                              <div className="st-field">
                                <label className="st-label">Token (PAT)</label>
                                <input
                                  className="st-input st-input-token"
                                  type="password"
                                  placeholder="dapi••••••••••••••••••••••••••••••••"
                                  value={envCfg.token}
                                  onChange={(e) => updateEnvField(d.domain, env, "token", e.target.value)}
                                />
                              </div>
                            </div>

                            {/* Deploy steps while deploying */}
                            {isDeploying && (
                              <div className="st-fw-steps">
                                {DEPLOY_STEPS.map((label, idx) => {
                                  const currentIdx = ep?.deploy_step_index ?? 0;
                                  const s = idx < currentIdx ? "done" : idx === currentIdx ? "current" : "pending";
                                  const liveDetail = s === "current" && ep?.deploy_step ? ep.deploy_step : null;
                                  return (
                                    <div key={idx} className={`st-fw-step st-fw-step--${s}`}>
                                      <span className="st-fw-step-icon">
                                        {s === "done" ? "✓" : s === "current"
                                          ? <span className="st-fw-step-spinner" />
                                          : <span className="st-fw-step-dot" />}
                                      </span>
                                      <span className="st-fw-step-body">
                                        <span className="st-fw-step-label">{label}</span>
                                        {liveDetail && (
                                          <span className="st-fw-step-detail">{liveDetail}</span>
                                        )}
                                      </span>
                                    </div>
                                  );
                                })}
                                {ep?.deploy_run_url && (
                                  <a
                                    className="st-fw-job-link"
                                    href={ep.deploy_run_url}
                                    target="_blank"
                                    rel="noreferrer"
                                  >
                                    Ver job no Databricks ↗
                                  </a>
                                )}
                              </div>
                            )}

                            {ep?.deploy_error && !isDeploying && (() => {
                              const isCatalogErr = ep.deploy_error!.includes("Falha ao criar catalog");
                              return (
                                <div className="st-fw-error-block">
                                  <span className="st-fw-error-msg">{ep.deploy_error}</span>
                                  {isCatalogErr && (
                                    <div className="st-fw-catalog-hint">
                                      <p>
                                        O catalog <code>corp_agent_framework</code> precisa ser criado manualmente
                                        no workspace com um <strong>MANAGED LOCATION</strong> (ADLS Gen2).
                                        Execute o SQL abaixo no{" "}
                                        {envCfg.workspace_url
                                          ? <a href={`${envCfg.workspace_url.replace(/\/$/, "")}/sql/editor`} target="_blank" rel="noreferrer">SQL Editor do workspace</a>
                                          : "SQL Editor do workspace"}
                                        :
                                      </p>
                                      <pre className="st-fw-catalog-sql">{`CREATE CATALOG IF NOT EXISTS corp_agent_framework\nMANAGED LOCATION 'abfss://<container>@<storage>.dfs.core.windows.net/corp_agent_framework';`}</pre>
                                      <p className="st-fw-catalog-hint-note">Substitua <code>&lt;container&gt;</code> e <code>&lt;storage&gt;</code> pelo ADLS Gen2 vinculado ao metastore. Depois clique em "Tentar novamente".</p>
                                    </div>
                                  )}
                                </div>
                              );
                            })()}

                            <div className="st-env-actions">
                              <button
                                className="st-save-btn"
                                type="button"
                                disabled={!!saving[key]}
                                onClick={() => handleSaveEnv(d.domain, env)}
                              >
                                {saving[key] ? "Salvando..." : "Salvar"}
                              </button>
                              {!isDeploying && (
                                <button
                                  className="st-fw-deploy-btn"
                                  type="button"
                                  disabled={epLoading}
                                  onClick={() => handleEnvDeploy(d.domain, env)}
                                >
                                  {ep?.state === "READY" ? "Re-deploy" : ep?.deploy_error ? "Tentar novamente" : "Deploy"}
                                </button>
                              )}
                              {isOk  && <span className="st-save-ok">Salvo</span>}
                              {isErr && <span className="st-save-error">{msgVal.slice(6)}</span>}
                            </div>

                            {envCfg.updated_at && (
                              <span className="st-updated-at">
                                Atualizado em {new Date(envCfg.updated_at).toLocaleString("pt-BR")}
                              </span>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* ── Modelos Disponíveis ───────────────────────── */}
        <div className="st-section">
          <div className="st-models-header">
            <div>
              <h2 className="st-section-title">Modelos Disponíveis</h2>
              <p className="st-section-desc">
                Apenas modelos <strong>aprovados</strong> aparecem no formulário de criação de agentes.
                Os endpoints são lidos em tempo real dos workspaces configurados.
              </p>
            </div>
            <button className="st-refresh-btn" onClick={loadModels} disabled={modelsLoading} type="button">
              {modelsLoading ? "Atualizando..." : "Atualizar"}
            </button>
          </div>

          {modelsLoading && <div className="st-state">Carregando modelos...</div>}
          {!modelsLoading && modelsError && <div className="st-error-banner">{modelsError}</div>}
          {!modelsLoading && !modelsError && models.length === 0 && (
            <div className="st-state">Nenhum endpoint de model serving encontrado nos workspaces configurados.</div>
          )}
          {!modelsLoading && !modelsError && models.length > 0 && (() => {
            const totalPages = Math.ceil(models.length / PAGE_SIZE);
            const pageModels = models.slice((modelsPage - 1) * PAGE_SIZE, modelsPage * PAGE_SIZE);
            return (
              <>
                {selected.size > 0 && (
                  <div className="st-bulk-bar">
                    <span className="st-bulk-count">{selected.size} selecionado{selected.size !== 1 ? "s" : ""}</span>
                    <button className="st-action-btn st-action-approve" disabled={bulkApproving} onClick={() => handleBulkStatus("approved")}>
                      {bulkApproving ? "..." : "Aprovar selecionados"}
                    </button>
                    <button className="st-action-btn st-action-reject" disabled={bulkApproving} onClick={() => handleBulkStatus("rejected")}>
                      {bulkApproving ? "..." : "Bloquear selecionados"}
                    </button>
                    <button className="st-bulk-clear" onClick={() => setSelected(new Set())}>Limpar seleção</button>
                  </div>
                )}
                <div className="st-models-table-wrap">
                  <table className="st-models-table">
                    <thead>
                      <tr>
                        <th className="st-col-check">
                          <input type="checkbox" checked={selected.size === models.length && models.length > 0} onChange={toggleSelectAll} />
                        </th>
                        <th>Endpoint</th>
                        <th>Ambiente</th>
                        <th>Modelo base</th>
                        <th>Estado</th>
                        <th>Aprovação</th>
                        <th>Ações</th>
                      </tr>
                    </thead>
                    <tbody>
                      {pageModels.map((m) => (
                        <tr key={m.name} className={selected.has(m.name) ? "st-row-selected" : ""}>
                          <td className="st-col-check">
                            <input type="checkbox" checked={selected.has(m.name)} onChange={() => toggleSelect(m.name)} />
                          </td>
                          <td className="st-model-name">{m.name}</td>
                          <td>
                            <span className={`st-env-badge st-env-badge-${m.env}`}>{m.env}</span>
                          </td>
                          <td className="st-model-base">{m.model_name || "—"}</td>
                          <td>
                            <span className={`st-ep-state st-ep-${m.state === "READY" ? "ready" : "notready"}`}>
                              {m.state}
                            </span>
                          </td>
                          <td>
                            <span className={`st-approval-badge st-approval-${m.approval_status}`}>
                              {m.approval_status === "approved" ? "Aprovado"
                                : m.approval_status === "rejected" ? "Bloqueado"
                                : "Pendente"}
                            </span>
                          </td>
                          <td className="st-model-actions">
                            {approveMsg[m.name] ? (
                              <span className="st-approve-err">{approveMsg[m.name]}</span>
                            ) : (
                              <>
                                {m.approval_status !== "approved" && (
                                  <button className="st-action-btn st-action-approve" disabled={!!approving[m.name]} onClick={() => handleModelStatus(m.name, "approved")}>
                                    {approving[m.name] ? "..." : "Aprovar"}
                                  </button>
                                )}
                                {m.approval_status !== "rejected" && (
                                  <button className="st-action-btn st-action-reject" disabled={!!approving[m.name]} onClick={() => handleModelStatus(m.name, "rejected")}>
                                    {approving[m.name] ? "..." : "Bloquear"}
                                  </button>
                                )}
                                {m.approval_status !== "pending" && (
                                  <button className="st-action-btn st-action-pending" disabled={!!approving[m.name]} onClick={() => handleModelStatus(m.name, "pending")}>
                                    {approving[m.name] ? "..." : "Resetar"}
                                  </button>
                                )}
                              </>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {totalPages > 1 && (
                  <div className="st-pagination">
                    <button className="st-page-btn" disabled={modelsPage === 1} onClick={() => setModelsPage((p) => p - 1)}>‹ Anterior</button>
                    <span className="st-page-info">
                      Página {modelsPage} de {totalPages}
                      <span className="st-page-total"> · {models.length} modelos</span>
                    </span>
                    <button className="st-page-btn" disabled={modelsPage === totalPages} onClick={() => setModelsPage((p) => p + 1)}>Próxima ›</button>
                  </div>
                )}
              </>
            );
          })()}
        </div>
      </div>
    </div>
  );
}
