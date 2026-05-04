import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import ProtectedRoute from "./components/ProtectedRoute";
import Layout from "./components/Layout";
import Home from "./pages/Home";
import Tools from "./pages/Tools";
import Agents from "./pages/Agents";
import Settings from "./pages/Settings";
import Run from "./pages/Run";
// import Login from "./pages/Login";       // add when auth is implemented
// import Callback from "./pages/Callback"; // add for PKCE callback

export default function App() {
  return (
    <BrowserRouter>
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
          <Route path="run" element={<Run />} />
          <Route path="settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
