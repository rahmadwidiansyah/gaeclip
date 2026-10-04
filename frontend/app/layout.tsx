import type { Metadata } from "next";
import "./globals.css";
import { SettingsProvider, ToastProvider } from "./lib/store";
import { Shell } from "./components/shell";

export const metadata: Metadata = {
  title: "Gaeclip | Shorts Studio",
  description: "Generate vertical clips from YouTube videos.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="id">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Roboto+Flex:opsz,wght@8..144,100..900&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>
        <SettingsProvider>
          <ToastProvider>
            <Shell>{children}</Shell>
          </ToastProvider>
        </SettingsProvider>
      </body>
    </html>
  );
}
