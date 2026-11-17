import "./globals.css";
import type { ReactNode } from "react";
import type { Viewport } from "next";
import { Inter } from "next/font/google";
import { cn } from "@/lib/utils";
import { SUPABASE_URL } from "@/lib/config";

const sans = Inter({ subsets: ["latin"], variable: "--font-inter", display: "swap" });

export const metadata = { title: "MemeDesk — live (paper)", description: "Desk memecoin Solana, paper trading, in tempo reale" };
export const viewport: Viewport = { themeColor: "#F3F4F6", colorScheme: "light", width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="it" className={cn("font-sans antialiased", sans.variable)}>
      <head>
        {/* the first data (REST snapshot + Realtime socket) comes from Supabase: open the connection while JS loads */}
        <link rel="preconnect" href={SUPABASE_URL} crossOrigin="anonymous" />
      </head>
      <body className="min-h-dvh bg-background text-foreground">{children}</body>
    </html>
  );
}
