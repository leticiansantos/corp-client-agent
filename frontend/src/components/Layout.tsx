import { NavLink, Outlet } from "react-router-dom";
import { useDomain } from "../contexts/DomainContext";
import "./Layout.css";

const DBX_LOGO = "https://cdn.brandfetch.io/idSUrLOWbH/w/400/h/98/theme/dark/logo.png";

const NAV_ITEMS = [
  { label: "Tools",    to: "/tools" },
  { label: "Agents",   to: "/agents" },
  { label: "Run",      to: "/run" },
  { label: "Settings", to: "/settings" },
];

export default function Layout() {
  const { domain, setDomain, domains, domainsLoading } = useDomain();

  return (
    <div className="app-shell">
      <header className="navbar">
        <div className="navbar-left">
          <NavLink to="/" className="navbar-logo" aria-label="Databricks home">
            <img src={DBX_LOGO} alt="Databricks" className="navbar-logo-img" />
          </NavLink>

          <span className="navbar-app-name">Corp Agent</span>

          <nav className="navbar-links">
            {NAV_ITEMS.map((item) => (
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
          </nav>
        </div>

        <div className="navbar-right">
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
