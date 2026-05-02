import { NavLink, Outlet } from "react-router-dom";
import "./Layout.css";

const DBX_LOGO = "https://cdn.brandfetch.io/idSUrLOWbH/w/400/h/98/theme/dark/logo.png";

const NAV_ITEMS = [
  { label: "Tools",    to: "/tools" },
  { label: "Agents",   to: "/agents" },
  { label: "Settings", to: "/settings" },
];

export default function Layout() {
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
          {/* User info / auth actions mount here */}
        </div>
      </header>

      <main className="app-content">
        <Outlet />
      </main>
    </div>
  );
}
