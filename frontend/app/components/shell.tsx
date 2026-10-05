"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { useSettings } from "../lib/store";
import { Icon } from "./ui";

const NAV = [
  { href: "/", label: "Dashboard", icon: "dashboard", exact: true },
  { href: "/jobs", label: "Jobs", icon: "jobs", exact: false },
  { href: "/clips", label: "Clips", icon: "clips", exact: false },
  { href: "/settings", label: "Settings", icon: "settings", exact: true },
];

function isActive(pathname: string, href: string, exact: boolean): boolean {
  if (exact) return pathname === href;
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { theme, setTheme } = useSettings();
  const [collapsed, setCollapsed] = useState(false);

  const cycleTheme = () => {
    setTheme(theme === "system" ? "light" : theme === "light" ? "dark" : "system");
  };

  return (
    <div className={`app${collapsed ? " nav-collapsed" : ""}`}>
      <aside className="sidebar" aria-label="Navigasi utama">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <img src="/icon.png" alt="" width={32} height={32} />
          </span>
          {!collapsed && (
            <span className="brand-text">
              <strong>Gaeclip</strong>
              <small>Shorts Studio</small>
            </span>
          )}
        </div>
        <nav className="nav">
          {NAV.map((item) => {
            const active = isActive(pathname, item.href, item.exact);
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`nav-item${active ? " nav-active" : ""}`}
                aria-current={active ? "page" : undefined}
                title={collapsed ? item.label : undefined}
              >
                <Icon name={item.icon} size={20} />
                {!collapsed && <span>{item.label}</span>}
              </Link>
            );
          })}
        </nav>
        <div className="nav-foot">
          <button
            type="button"
            className="btn btn-icon btn-ghost"
            onClick={cycleTheme}
            title={`Tema: ${theme} (klik untuk ganti)`}
            aria-label="Ganti tema"
          >
            <Icon name={theme === "dark" ? "moon" : theme === "light" ? "sun" : "monitor"} size={18} />
          </button>
          <button
            type="button"
            className="btn btn-icon btn-ghost"
            onClick={() => setCollapsed((c) => !c)}
            title={collapsed ? "Bentangkan sidebar" : "Ciutkan sidebar"}
            aria-label={collapsed ? "Bentangkan sidebar" : "Ciutkan sidebar"}
            aria-expanded={!collapsed}
          >
            <Icon name={collapsed ? "chevR" : "chevL"} size={18} />
          </button>
        </div>
      </aside>
      <div className="main">{children}</div>
    </div>
  );
}
