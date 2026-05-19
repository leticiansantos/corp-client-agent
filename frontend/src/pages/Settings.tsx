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
  warehouse_id: string;
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
  warehouse_id: "",
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
  const [creatingDomain, setCreatingDomain] = useState(false);
  const [deletingDomain, setDeletingDomain] = useState<string | null>(null);

  // Saving per env
  const [saving, setSaving]   = useState<Record<string, boolean>>({});
  const [saveMsg, setSaveMsg] = useState<Record<string, string>>({});

  // Endpoint status
  const [epStatus, setEpStatus]       = useState<Record<string, EndpointStatus>>({});
  const [epLoading, setEpLoading]     = useState(true);
  const [epError, setEpError]         = useState("");
  const [epDeploying, setEpDeploying] = useState<Record<string, boolean>>({});
  const epPollingRef                  = useRef<ReturnType<typeof setInterval> | null>(null);

  // Per-domain models
  const DM_PAGE_SIZE = 8;
  const [domainModels, setDomainModels]               = useState<Record<string, ModelEntry[]>>({});
  const [domainModelsLoading, setDomainModelsLoading] = useState<Record<string, boolean>>({});
  const [domainModelsError, setDomainModelsError]     = useState<Record<string, string>>({});
  const [domainApproving, setDomainApproving]         = useState<Record<string, boolean>>({});
  const [domainPage, setDomainPage]                   = useState<Record<string, number>>({});
  const [domainSelected, setDomainSelected]           = useState<Record<string, Set<string>>>({});
  const [domainBulkApproving, setDomainBulkApproving] = useState<Record<string, boolean>>({});

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

  // ── Per-domain models ─────────────────────────────────────────
  function loadDomainModels(domain: string) {
    setDomainModelsLoading((p) => ({ ...p, [domain]: true }));
    setDomainModelsError((p) => ({ ...p, [domain]: "" }));
    api
      .get<{ models: ModelEntry[] }>(`/settings/domains/${encodeURIComponent(domain)}/models`)
      .then((r) => setDomainModels((p) => ({ ...p, [domain]: r.data.models })))
      .catch((err) => {
        const detail = err?.response?.data?.detail ?? err?.message ?? "Erro desconhecido";
        setDomainModelsError((p) => ({ ...p, [domain]: detail }));
      })
      .finally(() => setDomainModelsLoading((p) => ({ ...p, [domain]: false })));
  }

  async function handleDomainModelStatus(
    domain: string,
    modelName: string,
    newStatus: ModelApprovalStatus,
  ) {
    const key = `${domain}::${modelName}`;
    setDomainApproving((p) => ({ ...p, [key]: true }));
    try {
      await api.patch(
        `/settings/domains/${encodeURIComponent(domain)}/models/${encodeURIComponent(modelName)}/status`,
        { new_status: newStatus },
      );
      setDomainModels((prev) => ({
        ...prev,
        [domain]: (prev[domain] ?? []).map((m) =>
          m.name === modelName ? { ...m, approval_status: newStatus } : m,
        ),
      }));
    } catch (err: unknown) {
      console.error("Erro ao atualizar status do modelo:", err);
    } finally {
      setDomainApproving((p) => ({ ...p, [key]: false }));
    }
  }

  function dmToggleSelect(domain: string, name: string) {
    setDomainSelected((prev) => {
      const cur = new Set(prev[domain] ?? []);
      cur.has(name) ? cur.delete(name) : cur.add(name);
      return { ...prev, [domain]: cur };
    });
  }

  function dmToggleSelectAll(domain: string) {
    const all = domainModels[domain] ?? [];
    const cur = domainSelected[domain] ?? new Set();
    setDomainSelected((prev) => ({
      ...prev,
      [domain]: cur.size === all.length ? new Set() : new Set(all.map((m) => m.name)),
    }));
  }

  async function dmBulkStatus(domain: string, newStatus: ModelApprovalStatus) {
    const sel = domainSelected[domain] ?? new Set();
    if (!sel.size) return;
    setDomainBulkApproving((p) => ({ ...p, [domain]: true }));
    await Promise.allSettled(
      Array.from(sel).map((name) =>
        api.patch(
          `/settings/domains/${encodeURIComponent(domain)}/models/${encodeURIComponent(name)}/status`,
          { new_status: newStatus },
        ).then(() =>
          setDomainModels((prev) => ({
            ...prev,
            [domain]: (prev[domain] ?? []).map((m) =>
              m.name === name ? { ...m, approval_status: newStatus } : m,
            ),
          }))
        )
      )
    );
    setDomainSelected((prev) => ({ ...prev, [domain]: new Set() }));
    setDomainBulkApproving((p) => ({ ...p, [domain]: false }));
  }

  // ── Domain CRUD ──────────────────────────────────────────────
  function toggleExpand(domain: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(domain)) {
        next.delete(domain);
      } else {
        next.add(domain);
        // Lazy-load models the first time the domain is expanded
        if (!domainModels[domain]) loadDomainModels(domain);
      }
      return next;
    });
  }

  async function handleAddDomain() {
    const name = newDomainName.trim().toLowerCase();
    if (!DOMAIN_RE.test(name)) {
      setNewDomainError("Use apenas letras minúsculas, números e hífens (sem hífens no início/fim).");
      return;
    }
    if (domains.some((d) => d.domain === name)) {
      setNewDomainError("Domínio já existe.");
      return;
    }
    setCreatingDomain(true);
    try {
      await api.post(`/settings/domains/${encodeURIComponent(name)}`);
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Erro ao criar domínio.";
      setNewDomainError(detail);
      return;
    } finally {
      setCreatingDomain(false);
    }
    setDomains((prev) => [...prev, { domain: name, envs: ENVS.map(EMPTY_DOMAIN_ENV) }]);
    setExpanded((prev) => new Set([...prev, name]));
    setNewDomainName("");
    setNewDomainError("");
    setShowNewDomain(false);
  }

  async function handleDeleteDomain(domain: string) {
    if (!confirm(`Remover domínio "${domain}" e todas suas configurações?`)) return;
    setDeletingDomain(domain);
    try {
      await api.delete(`/settings/domains/${encodeURIComponent(domain)}`);
      setDomains((prev) => prev.filter((d) => d.domain !== domain));
      setExpanded((prev) => { const n = new Set(prev); n.delete(domain); return n; });
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Erro ao remover domínio.";
      alert(detail);
    } finally {
      setDeletingDomain(null);
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
              <button className="st-save-btn" type="button" onClick={handleAddDomain} disabled={creatingDomain}>
                {creatingDomain ? "Criando..." : "Adicionar"}
              </button>
              <button className="st-refresh-btn" type="button" onClick={() => setShowNewDomain(false)} disabled={creatingDomain}>Cancelar</button>
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
                      disabled={deletingDomain === d.domain}
                      onClick={(e) => { e.stopPropagation(); handleDeleteDomain(d.domain); }}
                    >
                      {deletingDomain === d.domain ? "..." : "✕"}
                    </button>
                  </button>

                  {/* Expanded: 3 env cards side by side */}
                  {isExpanded && (
                    <>
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
                              <div className="st-field">
                                <label className="st-label">Warehouse ID</label>
                                <input
                                  className="st-input"
                                  placeholder="abc123def456"
                                  value={envCfg.warehouse_id}
                                  onChange={(e) => updateEnvField(d.domain, env, "warehouse_id", e.target.value)}
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

                    {/* Per-domain models */}
                    {(() => {
                      const allModels  = domainModels[d.domain] ?? [];
                      const page       = domainPage[d.domain] ?? 1;
                      const totalPages = Math.ceil(allModels.length / DM_PAGE_SIZE);
                      const pageModels = allModels.slice((page - 1) * DM_PAGE_SIZE, page * DM_PAGE_SIZE);
                      const sel        = domainSelected[d.domain] ?? new Set<string>();
                      const bulkBusy   = !!domainBulkApproving[d.domain];
                      return (
                        <div className="st-domain-models">
                          <div className="st-dm-header">
                            <span className="st-dm-title">Modelos do domínio</span>
                            <button
                              className="st-refresh-btn"
                              type="button"
                              disabled={!!domainModelsLoading[d.domain]}
                              onClick={() => loadDomainModels(d.domain)}
                            >
                              {domainModelsLoading[d.domain] ? "Atualizando..." : "Atualizar"}
                            </button>
                          </div>

                          {domainModelsLoading[d.domain] && (
                            <div className="st-dm-state">Carregando modelos...</div>
                          )}
                          {domainModelsError[d.domain] && (
                            <div className="st-dm-error">{domainModelsError[d.domain]}</div>
                          )}
                          {!domainModelsLoading[d.domain] && !domainModelsError[d.domain] && allModels.length === 0 && (
                            <div className="st-dm-state">Nenhum modelo encontrado para este domínio.</div>
                          )}

                          {!domainModelsLoading[d.domain] && allModels.length > 0 && (
                            <>
                              {sel.size > 0 && (
                                <div className="st-bulk-bar">
                                  <span className="st-bulk-count">{sel.size} selecionado{sel.size !== 1 ? "s" : ""}</span>
                                  <button className="st-action-btn st-action-approve" disabled={bulkBusy} onClick={() => dmBulkStatus(d.domain, "approved")}>
                                    {bulkBusy ? "..." : "Aprovar"}
                                  </button>
                                  <button className="st-action-btn st-action-reject" disabled={bulkBusy} onClick={() => dmBulkStatus(d.domain, "rejected")}>
                                    {bulkBusy ? "..." : "Bloquear"}
                                  </button>
                                  <button className="st-bulk-clear" onClick={() => setDomainSelected((p) => ({ ...p, [d.domain]: new Set() }))}>Limpar</button>
                                </div>
                              )}

                              <table className="st-dm-table">
                                <thead>
                                  <tr>
                                    <th className="st-col-check">
                                      <input
                                        type="checkbox"
                                        checked={sel.size === allModels.length && allModels.length > 0}
                                        onChange={() => dmToggleSelectAll(d.domain)}
                                      />
                                    </th>
                                    <th>Endpoint</th>
                                    <th>Estado</th>
                                    <th>Aprovação</th>
                                    <th>Ações</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {pageModels.map((m) => {
                                    const dmKey = `${d.domain}::${m.name}`;
                                    return (
                                      <tr key={m.name} className={sel.has(m.name) ? "st-row-selected" : ""}>
                                        <td className="st-col-check">
                                          <input type="checkbox" checked={sel.has(m.name)} onChange={() => dmToggleSelect(d.domain, m.name)} />
                                        </td>
                                        <td className="st-dm-name">{m.name}</td>
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
                                        <td className="st-dm-actions">
                                          {m.approval_status !== "approved" && (
                                            <button className="st-action-btn st-action-approve" disabled={!!domainApproving[dmKey]} onClick={() => handleDomainModelStatus(d.domain, m.name, "approved")}>
                                              {domainApproving[dmKey] ? "..." : "Aprovar"}
                                            </button>
                                          )}
                                          {m.approval_status !== "rejected" && (
                                            <button className="st-action-btn st-action-reject" disabled={!!domainApproving[dmKey]} onClick={() => handleDomainModelStatus(d.domain, m.name, "rejected")}>
                                              {domainApproving[dmKey] ? "..." : "Bloquear"}
                                            </button>
                                          )}
                                          {m.approval_status !== "pending" && (
                                            <button className="st-action-btn st-action-pending" disabled={!!domainApproving[dmKey]} onClick={() => handleDomainModelStatus(d.domain, m.name, "pending")}>
                                              {domainApproving[dmKey] ? "..." : "Resetar"}
                                            </button>
                                          )}
                                        </td>
                                      </tr>
                                    );
                                  })}
                                </tbody>
                              </table>

                              {totalPages > 1 && (
                                <div className="st-pagination">
                                  <button className="st-page-btn" disabled={page === 1} onClick={() => setDomainPage((p) => ({ ...p, [d.domain]: page - 1 }))}>‹ Anterior</button>
                                  <span className="st-page-info">
                                    Página {page} de {totalPages}
                                    <span className="st-page-total"> · {allModels.length} modelos</span>
                                  </span>
                                  <button className="st-page-btn" disabled={page === totalPages} onClick={() => setDomainPage((p) => ({ ...p, [d.domain]: page + 1 }))}>Próxima ›</button>
                                </div>
                              )}
                            </>
                          )}
                        </div>
                      );
                    })()}
                    </>
                  )}
                </div>
              );
            })}
          </div>
        </div>

      </div>
    </div>
  );
}
