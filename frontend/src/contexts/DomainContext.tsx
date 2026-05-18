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

export function DomainProvider({ children }: { children: ReactNode }) {
  const [domain, setDomain] = useState("");
  const [domains, setDomains] = useState<string[]>([]);
  const [domainsLoading, setDomainsLoading] = useState(true);

  useEffect(() => {
    setDomainsLoading(true);
    api
      .get<{ domains: { domain: string }[] }>("/settings/domains")
      .then((r) => {
        const list = r.data.domains.map((d) => d.domain);
        setDomains(list);
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
