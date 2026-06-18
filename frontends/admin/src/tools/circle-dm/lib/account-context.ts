const LS_KEY = 'circle-dm:active-account-id';

export function getActiveAccountId(): number | null {
  const raw = localStorage.getItem(LS_KEY);
  if (!raw) return null;
  const id = Number.parseInt(raw, 10);
  return Number.isInteger(id) ? id : null;
}

export function setActiveAccountId(id: number | null): void {
  if (id === null) localStorage.removeItem(LS_KEY);
  else localStorage.setItem(LS_KEY, String(id));
  window.dispatchEvent(new Event('circle-dm:account-changed'));
}

export function onActiveAccountChange(handler: () => void): () => void {
  const wrapped = () => handler();
  window.addEventListener('circle-dm:account-changed', wrapped);
  return () => window.removeEventListener('circle-dm:account-changed', wrapped);
}
