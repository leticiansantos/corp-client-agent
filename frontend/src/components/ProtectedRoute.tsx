import type { ReactNode } from "react";
// import { Navigate } from "react-router-dom";
// import { isAuthenticated } from "../auth";

interface Props {
  children: ReactNode;
}

/**
 * Auth stub — currently renders children unconditionally.
 *
 * To enable protection, uncomment the imports above and replace
 * the return statement with:
 *
 *   return isAuthenticated() ? <>{children}</> : <Navigate to="/login" replace />;
 */
export default function ProtectedRoute({ children }: Props) {
  return <>{children}</>;
}
