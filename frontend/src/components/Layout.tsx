import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useDomain } from "../contexts/DomainContext";
import { useAdmin } from "../contexts/AdminContext";
import "./Layout.css";

const DBX_LOGO = "https://cdn.brandfetch.io/idSUrLOWbH/w/400/h/98/theme/dark/logo.png";

const BASE_NAV_ITEMS = [
  { label: "Tools",      to: "/tools" },
  { label: "Agents",     to: "/agents" },
  { label: "Guardrails", to: "/guardrails" },
  { label: "Run",        to: "/run" },
];

export default function Layout() {
  const { domain, setDomain, domains, domainsLoading } = useDomain();
  const { isAdmin, toggleAdmin } = useAdmin();
  const navigate = useNavigate();

  function handleAdminToggle() {
    const turningOff = isAdmin;
    toggleAdmin();
    if (turningOff) {
      // If currently on settings, redirect away since it'll become inaccessible
      if (window.location.pathname === "/settings") {
        navigate("/", { replace: true });
      }
    }
  }

  return (
    <div className="app-shell">
      <header className={`navbar${isAdmin ? " navbar-admin" : ""}`}>
        <div className="navbar-left">
          <NavLink to="/" className="navbar-logo" aria-label="Databricks home">
            <img src={DBX_LOGO} alt="Databricks" className="navbar-logo-img" />
          </NavLink>

          <span className="navbar-app-name">Corp Agent</span>

          <nav className="navbar-links">
            {BASE_NAV_ITEMS.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  `navbar-link${isActive ? " active" : ""}`
                }
              >
                {item.label}
              </NavLink>
            ))}
            {isAdmin && (
              <NavLink
                to="/settings"
                className={({ isActive }) =>
                  `navbar-link navbar-link-admin${isActive ? " active" : ""}`
                }
              >
                Settings
              </NavLink>
            )}
          </nav>
        </div>

        <div className="navbar-right">
          <button
            className={`navbar-admin-toggle${isAdmin ? " is-admin" : ""}`}
            onClick={handleAdminToggle}
            title={isAdmin ? "Desativar modo Admin" : "Ativar modo Admin"}
            type="button"
          >
            <span className="navbar-admin-toggle-track">
              <span className="navbar-admin-toggle-thumb" />
            </span>
            <span className="navbar-admin-toggle-label">Admin</span>
          </button>

          <div className="navbar-domain-wrap">
            <span className="navbar-domain-label">Domínio</span>
            <select
              className="navbar-domain-select"
              value={domain}
              onChange={(e) => setDomain(e.target.value)}
              disabled={domainsLoading || domains.length === 0}
              title="Selecionar domínio ativo"
            >
              <option value="">
                {domainsLoading
                  ? "Carregando..."
                  : domains.length === 0
                  ? "Nenhum domínio"
                  : "— todos —"}
              </option>
              {domains.map((d) => (
                <option key={d} value={d}>{d}</option>
              ))}
            </select>
          </div>
        </div>
      </header>

      <main className="app-content">
        <Outlet />
      </main>
    </div>
  );
}
