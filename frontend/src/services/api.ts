import axios from "axios";
// import { getToken } from "../auth";

const api = axios.create({ baseURL: "/api" });

/**
 * Auth interceptor stub — uncomment when authentication is implemented.
 *
 * api.interceptors.request.use((config) => {
 *   const token = getToken();
 *   if (token) config.headers.Authorization = `Bearer ${token}`;
 *   return config;
 * });
 */

export default api;
