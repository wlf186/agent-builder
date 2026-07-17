"use client";

import { createContext, useContext, useSyncExternalStore, ReactNode } from "react";
import { Locale, t, TranslationKey, getLocaleName } from "./i18n";

interface LocaleContextType {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: (key: TranslationKey) => string;
  getLocaleName: (locale: Locale) => string;
}

const LocaleContext = createContext<LocaleContextType | undefined>(undefined);
const LOCALE_CHANGE_EVENT = "agent-builder-locale-change";

function readStoredLocale(): Locale {
  if (typeof window === "undefined") return "zh";
  const saved = localStorage.getItem("locale");
  return saved === "en" || saved === "zh" ? saved : "zh";
}

function subscribeToLocale(onStoreChange: () => void): () => void {
  window.addEventListener("storage", onStoreChange);
  window.addEventListener(LOCALE_CHANGE_EVENT, onStoreChange);
  return () => {
    window.removeEventListener("storage", onStoreChange);
    window.removeEventListener(LOCALE_CHANGE_EVENT, onStoreChange);
  };
}

export function LocaleProvider({ children }: { children: ReactNode }) {
  const locale = useSyncExternalStore<Locale>(subscribeToLocale, readStoredLocale, () => "zh");

  // 保存语言设置到 localStorage
  const handleSetLocale = (newLocale: Locale) => {
    localStorage.setItem("locale", newLocale);
    window.dispatchEvent(new Event(LOCALE_CHANGE_EVENT));
  };

  return (
    <LocaleContext.Provider
      value={{
        locale,
        setLocale: handleSetLocale,
        t: (key: TranslationKey) => t(locale, key),
        getLocaleName,
      }}
    >
      {children}
    </LocaleContext.Provider>
  );
}

export function useLocale() {
  const context = useContext(LocaleContext);
  if (!context) {
    throw new Error("useLocale must be used within a LocaleProvider");
  }
  return context;
}
