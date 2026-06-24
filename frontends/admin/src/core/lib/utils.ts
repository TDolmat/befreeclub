import { type ClassValue, clsx } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * Normalize for fuzzy search: lowercase + strip diacritics so "kon" matches
 * "koń", "lacki" matches "łącki". NFD splits most accents into combining
 * marks; ł/Ł don't decompose so they're mapped explicitly.
 */
export function foldText(s: string): string {
  return s
    .normalize('NFD')
    .replace(/\p{Mn}/gu, '')
    .replace(/ł/g, 'l')
    .replace(/Ł/g, 'l')
    .toLowerCase()
    .trim();
}

export function textMatches(haystack: string | null | undefined, foldedQuery: string): boolean {
  if (!foldedQuery) return true;
  if (!haystack) return false;
  return foldText(haystack).includes(foldedQuery);
}
