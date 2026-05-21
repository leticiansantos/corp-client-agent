import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import ProtectedRoute from "./components/ProtectedRoute";
import Layout from "./components/Layout";
import { DomainProvider } from "./contexts/DomainContext";
import { AdminProvider, useAdmin } from "./contexts/AdminContext";
import Guardrails from "./pages/Guardrails";
import Home from "./pages/Home";
import Tools from "./pages/Tools";
import Agents from "./pages/Agents";
import Settings from "./pages/Settings";
import Run from "./pages/Run";
// import Login from "./pages/Login";       // add when auth is implemented
// import Callback from "./pages/Callback"; // add for PKCE callback

function AdminRoute({ children }: { children: React.ReactNode }) {
  const { isAdmin } = useAdmin();
  return isAdmin ? <>{children}</> : <Navigate to="/" replace />;
}

export default function App() {
  return (
    <BrowserRouter>
      <AdminProvider>
        <DomainProvider>
          <Routes>
            {/* <Route path="/login" element={<Login />} /> */}
            {/* <Route path="/callback" element={<Callback />} /> */}
            <Route
              element={
                <ProtectedRoute>
                  <Layout />
                </ProtectedRoute>
              }
            >
              <Route index element={<Home />} />
              <Route path="tools" element={<Tools />} />
              <Route path="agents" element={<Agents />} />
              <Route path="guardrails" element={<Guardrails />} />
              <Route path="run" element={<Run />} />
              <Route path="settings" element={<AdminRoute><Settings /></AdminRoute>} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </DomainProvider>
      </AdminProvider>
    </BrowserRouter>
  );
}
