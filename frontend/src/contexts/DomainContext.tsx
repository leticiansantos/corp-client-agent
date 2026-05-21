import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import api from "../services/api";

interface DomainContextValue {
  domain: string;
  setDomain: (d: string) => void;
  domains: string[];
  domainsLoading: boolean;
}

const DomainContext = createContext<DomainContextValue>({
  domain: "",
  setDomain: () => {},
  domains: [],
  domainsLoading: true,
});

const CACHE_KEY = "corp_agent_domains";

function readCache(): string[] {
  try {
    const raw = sessionStorage.getItem(CACHE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function writeCache(list: string[]) {
  try {
    sessionStorage.setItem(CACHE_KEY, JSON.stringify(list));
  } catch {
    // ignore
  }
}

export function DomainProvider({ children }: { children: ReactNode }) {
  const cached = readCache();
  const [domain, setDomain] = useState(cached.length === 1 ? cached[0] : "");
  const [domains, setDomains] = useState<string[]>(cached);
  const [domainsLoading, setDomainsLoading] = useState(cached.length === 0);

  useEffect(() => {
    // Only show spinner if we have nothing cached yet
    if (domains.length === 0) setDomainsLoading(true);
    api
      .get<{ domains: { domain: string }[] }>("/settings/domains")
      .then((r) => {
        const list = r.data.domains.map((d) => d.domain);
        setDomains(list);
        writeCache(list);
        // Auto-select when only one domain exists
        if (list.length === 1) setDomain(list[0]);
      })
      .catch(() => {})
      .finally(() => setDomainsLoading(false));
  }, []);

  return (
    <DomainContext.Provider value={{ domain, setDomain, domains, domainsLoading }}>
      {children}
    </DomainContext.Provider>
  );
}

export function useDomain() {
  return useContext(DomainContext);
}
