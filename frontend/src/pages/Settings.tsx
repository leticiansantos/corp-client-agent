import { useEffect, useState } from "react";
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
  const [configs, setConfigs]     = useState<WorkspaceEnvConfig[]>(ENVS.map(EMPTY_ENV));
  const [loading, setLoading]     = useState(true);
  const [loadError, setLoadError] = useState("");
  const [saving, setSaving]       = useState(false);
  const [saveOk, setSaveOk]       = useState("");
  const [saveError, setSaveError] = useState("");

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

  return (
    <div className="st-shell">
      {/* Header */}
      <div className="st-header">
        <div className="st-header-left">
          <h1 className="st-title">Configurações</h1>
          <span className="st-subtitle">Workspaces Databricks por ambiente de execução</span>
        </div>
      </div>

      <div className="st-body">
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
      </div>
    </div>
  );
}
