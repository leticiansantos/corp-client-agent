import { useEffect, useRef, useState } from "react";
import api from "../services/api";
import "./Settings.css";

// ── Types ──────────────────────────────────────────────────────
type Env = "dev" | "staging" | "prod";

interface WorkspaceEnvConfig {
  env: Env;
  workspace_url: string;
  catalog: string;
  schema_name: string;
  warehouse_id: string;
  token: string;
  notes: string;
  updated_at: string | null;
}

interface FrameworkEndpointStatus {
  env: Env;
  endpoint_name: string;
  endpoint_url: string;
  state: "READY" | "NOT_READY" | "NOT_FOUND" | "NOT_CONFIGURED" | "ERROR";
  deploying: boolean;
  deploy_step: string;
  deploy_step_index: number;
  deploy_error?: string;
  error?: string;
}

const DEPLOY_STEPS = [
  "Criar catalog",
  "Criar schema",
  "Criar tabela tools_config",
  "Criar tabela agents_config",
  "Criar volume para libs",
  "Build WHL do corp_agent_framework",
  "Upload WHL para Volume",
  "Verificar endpoint existente",
  "Upload notebook de deploy",
  "Submeter job de deploy",
  "Aguardar conclusão do deploy",
];

const ENV_META: Record<Env, { label: string; cloud: string; desc: string }> = {
  dev:     { label: "Dev",     cloud: "AWS",   desc: "Ambiente de desenvolvimento e sandbox" },
  staging: { label: "Staging", cloud: "GCP",   desc: "Ambiente de pré-produção e validação" },
  prod:    { label: "Prod",    cloud: "Azure", desc: "Ambiente de produção corporativo" },
};

const ENVS: Env[] = ["dev", "staging", "prod"];

const EMPTY_ENV = (env: Env): WorkspaceEnvConfig => ({
  env,
  workspace_url: "",
  catalog: "corp_agent_framework",
  schema_name: "agents",
  warehouse_id: "",
  token: "",
  notes: "",
  updated_at: null,
});

// ── Component ──────────────────────────────────────────────────
export default function Settings() {
  // Workspace config
  const [configs, setConfigs]     = useState<WorkspaceEnvConfig[]>(ENVS.map(EMPTY_ENV));
  const [loading, setLoading]     = useState(true);
  const [loadError, setLoadError] = useState("");
  const [saving, setSaving]       = useState(false);
  const [saveOk, setSaveOk]       = useState("");
  const [saveError, setSaveError] = useState("");

  // Framework endpoints
  const [epStatus, setEpStatus] = useState<Record<Env, FrameworkEndpointStatus | null>>({
    dev: null, staging: null, prod: null,
  });
  const [epLoading, setEpLoading]     = useState(true);
  const [epError, setEpError]         = useState("");
  const [epDeploying, setEpDeploying] = useState<Record<string, boolean>>({});
  const epPollingRef                  = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── Workspace config load ────────────────────────────────────
  useEffect(() => {
    api
      .get<{ envs: WorkspaceEnvConfig[] }>("/settings/workspaces")
      .then((r) => {
        const byEnv: Record<string, WorkspaceEnvConfig> = {};
        for (const cfg of r.data.envs) byEnv[cfg.env] = cfg;
        setConfigs(ENVS.map((env) => byEnv[env] ?? EMPTY_ENV(env)));
      })
      .catch((err) => {
        const detail = err?.response?.data?.detail ?? err?.message ?? "Erro desconhecido";
        setLoadError(`Erro ao carregar configurações: ${detail}`);
      })
      .finally(() => setLoading(false));
  }, []);

  // ── Framework endpoints ──────────────────────────────────────
  function _applyEpResponse(endpoints: FrameworkEndpointStatus[]) {
    const byEnv: Record<string, FrameworkEndpointStatus> = {};
    for (const ep of endpoints) byEnv[ep.env] = ep;
    setEpStatus({
      dev:     (byEnv["dev"]     as FrameworkEndpointStatus) ?? null,
      staging: (byEnv["staging"] as FrameworkEndpointStatus) ?? null,
      prod:    (byEnv["prod"]    as FrameworkEndpointStatus) ?? null,
    });
    setEpDeploying({});
    return endpoints.some((ep) => ep.deploying);
  }

  function loadFrameworkEndpoints() {
    setEpLoading(true);
    setEpError("");
    api
      .get<{ endpoints: FrameworkEndpointStatus[] }>("/settings/framework-endpoints")
      .then((r) => {
        const anyDeploying = _applyEpResponse(r.data.endpoints);
        if (anyDeploying) _startEpPolling(); else _stopEpPolling();
      })
      .catch((err) => {
        const detail = err?.response?.data?.detail ?? err?.message ?? "Erro desconhecido";
        setEpError(`Erro ao carregar endpoints: ${detail}`);
      })
      .finally(() => setEpLoading(false));
  }

  function _startEpPolling() {
    if (epPollingRef.current !== null) return;
    epPollingRef.current = setInterval(() => {
      api
        .get<{ endpoints: FrameworkEndpointStatus[] }>("/settings/framework-endpoints")
        .then((r) => {
          const anyDeploying = _applyEpResponse(r.data.endpoints);
          if (!anyDeploying) _stopEpPolling();
        })
        .catch(() => {/* keep polling */});
    }, 10_000);
  }

  function _stopEpPolling() {
    if (epPollingRef.current !== null) {
      clearInterval(epPollingRef.current);
      epPollingRef.current = null;
    }
  }

  useEffect(() => {
    loadFrameworkEndpoints();
    return () => _stopEpPolling();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleFrameworkDeploy(env: Env) {
    setEpDeploying((p) => ({ ...p, [env]: true }));
    try {
      await api.post(`/settings/framework-endpoints/${env}/deploy`);
      _startEpPolling();
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Erro ao iniciar deploy.";
      alert(detail);
      setEpDeploying((p) => ({ ...p, [env]: false }));
    }
  }

  // ── Workspace field update ───────────────────────────────────
  function updateField(env: Env, field: keyof WorkspaceEnvConfig, value: string) {
    setConfigs((prev) =>
      prev.map((c) => (c.env === env ? { ...c, [field]: value } : c))
    );
  }

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setSaveOk("");
    setSaveError("");
    try {
      await api.put("/settings/workspaces", { envs: configs });
      setSaveOk("Configurações salvas com sucesso.");
      setTimeout(() => setSaveOk(""), 3000);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Erro ao salvar configurações.";
      setSaveError(msg);
    } finally {
      setSaving(false);
    }
  }

  // ── Render ───────────────────────────────────────────────────
  return (
    <div className="st-shell">
      <div className="st-header">
        <div className="st-header-left">
          <h1 className="st-title">Configurações</h1>
          <span className="st-subtitle">Workspaces Databricks por ambiente de execução</span>
        </div>
      </div>

      <div className="st-body">
        {/* ── Workspace config ─────────────────────────────── */}
        {loading && <div className="st-state">Carregando configurações...</div>}
        {!loading && loadError && <div className="st-error-banner">{loadError}</div>}

        {!loading && !loadError && (
          <form onSubmit={handleSave}>
            <div className="st-section">
              <h2 className="st-section-title">Ambientes de Workspace</h2>
              <p className="st-section-desc">
                Configure o workspace Databricks, catálogo, schema e token de acesso para cada
                ambiente. As configurações são usadas pelo SDK do corp_agent_framework para
                registro de tools e agents.
              </p>

              <div className="st-env-grid">
                {configs.map((cfg) => {
                  const meta = ENV_META[cfg.env as Env];
                  return (
                    <div key={cfg.env} className={`st-env-card st-env-${cfg.env}`}>
                      <div className="st-env-card-header">
                        <span className={`st-env-badge st-env-badge-${cfg.env}`}>
                          {meta.label}
                        </span>
                        <span className={`st-cloud-badge st-cloud-${meta.cloud.toLowerCase()}`}>
                          {meta.cloud}
                        </span>
                        <span className="st-env-card-desc">{meta.desc}</span>
                      </div>

                      <div className="st-fields">
                        <div className="st-field">
                          <label className="st-label">Workspace URL</label>
                          <input
                            className="st-input"
                            placeholder="https://adb-xxxx.azuredatabricks.net"
                            value={cfg.workspace_url}
                            onChange={(e) =>
                              updateField(cfg.env as Env, "workspace_url", e.target.value)
                            }
                          />
                        </div>

                        <div className="st-field-row">
                          <div className="st-field">
                            <label className="st-label">Catalog</label>
                            <input
                              className="st-input"
                              placeholder="corp_agent_framework"
                              value={cfg.catalog}
                              onChange={(e) =>
                                updateField(cfg.env as Env, "catalog", e.target.value)
                              }
                            />
                          </div>
                          <div className="st-field">
                            <label className="st-label">Schema</label>
                            <input
                              className="st-input"
                              placeholder="agents"
                              value={cfg.schema_name}
                              onChange={(e) =>
                                updateField(cfg.env as Env, "schema_name", e.target.value)
                              }
                            />
                          </div>
                        </div>

                        <div className="st-field">
                          <label className="st-label">Warehouse ID</label>
                          <input
                            className="st-input"
                            placeholder="abc123def456"
                            value={cfg.warehouse_id}
                            onChange={(e) =>
                              updateField(cfg.env as Env, "warehouse_id", e.target.value)
                            }
                          />
                          <span className="st-hint">
                            SQL Warehouse para execução de statements nesse ambiente
                          </span>
                        </div>

                        <div className="st-field">
                          <label className="st-label">Token (PAT)</label>
                          <input
                            className="st-input st-input-token"
                            type="password"
                            placeholder="dapi••••••••••••••••••••••••••••••••"
                            value={cfg.token}
                            onChange={(e) =>
                              updateField(cfg.env as Env, "token", e.target.value)
                            }
                          />
                          <span className="st-hint">
                            Personal Access Token do workspace. Se vazio, usa a variável de ambiente do servidor.
                          </span>
                        </div>

                        <div className="st-field">
                          <label className="st-label">Notas</label>
                          <textarea
                            className="st-textarea"
                            rows={2}
                            placeholder="Observações sobre esse ambiente..."
                            value={cfg.notes}
                            onChange={(e) =>
                              updateField(cfg.env as Env, "notes", e.target.value)
                            }
                          />
                        </div>

                        {cfg.updated_at && (
                          <span className="st-updated-at">
                            Atualizado em{" "}
                            {new Date(cfg.updated_at).toLocaleString("pt-BR")}
                          </span>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>

              <div className="st-footer">
                {saveOk    && <span className="st-save-ok">{saveOk}</span>}
                {saveError && <span className="st-save-error">{saveError}</span>}
                <button type="submit" className="st-save-btn" disabled={saving}>
                  {saving ? "Salvando..." : "Salvar configurações"}
                </button>
              </div>
            </div>
          </form>
        )}

        {/* ── Corp Agent Framework endpoints ───────────────── */}
        <div className="st-section">
          <div className="st-fw-header">
            <div>
              <h2 className="st-section-title">Corp Agent Framework</h2>
              <p className="st-section-desc">
                Endpoint do corp_agent_framework por ambiente. Faça o deploy para criar ou
                atualizar o serving endpoint <code className="st-inline-code">corp-config-driven-agent-{"{env}"}</code> no
                workspace correspondente.
              </p>
            </div>
            <button
              className="st-refresh-btn"
              onClick={loadFrameworkEndpoints}
              disabled={epLoading}
              type="button"
            >
              {epLoading ? "Atualizando..." : "Atualizar"}
            </button>
          </div>

          {epLoading && !Object.values(epStatus).some(Boolean) && (
            <div className="st-state">Carregando status dos endpoints...</div>
          )}
          {epError && <div className="st-error-banner">{epError}</div>}

          <div className="st-env-grid">
            {ENVS.map((env) => {
              const meta = ENV_META[env];
              const ep   = epStatus[env];
              const isDeploying    = !!epDeploying[env] || !!ep?.deploying;
              const notConfigured  = ep?.state === "NOT_CONFIGURED";

              return (
                <div key={env} className={`st-fw-card st-env-${env}`}>
                  {/* Card header */}
                  <div className="st-env-card-header">
                    <span className={`st-env-badge st-env-badge-${env}`}>{meta.label}</span>
                    <span className={`st-cloud-badge st-cloud-${meta.cloud.toLowerCase()}`}>
                      {meta.cloud}
                    </span>
                  </div>

                  {/* Endpoint name */}
                  <div className="st-fw-field">
                    <span className="st-label">Endpoint</span>
                    <code className="st-fw-code">corp-config-driven-agent-{env}</code>
                  </div>

                  {/* Invocation URL */}
                  <div className="st-fw-field">
                    <span className="st-label">URL de invocação</span>
                    {ep?.endpoint_url ? (
                      <a
                        href={ep.endpoint_url}
                        target="_blank"
                        rel="noreferrer"
                        className="st-fw-url"
                        title={ep.endpoint_url}
                      >
                        {ep.endpoint_url}
                      </a>
                    ) : (
                      <span className="st-fw-url-empty">
                        {notConfigured
                          ? "Workspace não configurado"
                          : ep
                          ? "Endpoint não criado"
                          : "—"}
                      </span>
                    )}
                  </div>

                  {/* Status badge */}
                  <div className="st-fw-status-row">
                    <span className="st-label">Status</span>
                    <span className={`st-fw-badge st-fw-badge-${
                      isDeploying              ? "deploying"
                      : ep?.state === "READY"          ? "ready"
                      : ep?.state === "NOT_READY"      ? "notready"
                      : ep?.state === "NOT_FOUND"      ? "notfound"
                      : ep?.state === "NOT_CONFIGURED" ? "notconfigured"
                      : ep?.state === "ERROR"          ? "error"
                      : "unknown"
                    }`}>
                      {isDeploying              ? "Deployando..."
                        : ep?.state === "READY"          ? "Ready"
                        : ep?.state === "NOT_READY"      ? "Not Ready"
                        : ep?.state === "NOT_FOUND"      ? "Não criado"
                        : ep?.state === "NOT_CONFIGURED" ? "Não configurado"
                        : ep?.state === "ERROR"          ? "Erro"
                        : "—"}
                    </span>
                  </div>

                  {/* Error detail */}
                  {ep?.state === "ERROR" && ep.error && (
                    <span className="st-fw-error-msg">{ep.error}</span>
                  )}

                  {/* Deploy steps — shown only while deploying */}
                  {isDeploying && (
                    <div className="st-fw-steps">
                      {DEPLOY_STEPS.map((label, idx) => {
                        const currentIdx = ep?.deploy_step_index ?? 0;
                        const s = idx < currentIdx ? "done" : idx === currentIdx ? "current" : "pending";
                        return (
                          <div key={idx} className={`st-fw-step st-fw-step--${s}`}>
                            <span className="st-fw-step-icon">
                              {s === "done" ? "✓" : s === "current" ? <span className="st-fw-step-spinner" /> : <span className="st-fw-step-dot" />}
                            </span>
                            <span className="st-fw-step-label">{label}</span>
                          </div>
                        );
                      })}
                    </div>
                  )}

                  {/* Deploy error */}
                  {!isDeploying && ep?.deploy_error && (
                    <span className="st-fw-error-msg">{ep.deploy_error}</span>
                  )}

                  {/* Action */}
                  {notConfigured ? (
                    <span className="st-fw-not-configured">
                      Configure o Workspace URL e Token acima para habilitar o deploy.
                    </span>
                  ) : !isDeploying && (
                    <button
                      className="st-fw-deploy-btn"
                      type="button"
                      onClick={() => handleFrameworkDeploy(env)}
                    >
                      {ep?.state === "READY" ? "Re-deploy" : ep?.deploy_error ? "Tentar novamente" : "Deploy"}
                    </button>
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
