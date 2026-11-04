import type { Config } from "tailwindcss";

/**
 * Tailwind config (Chatwoot design-parity port).
 *
 * Two color systems (see app/globals.css header):
 *   - shadcn semantic colors read HSL vars: `hsl(var(--primary))` →
 *     `bg-background`, `text-foreground`, `border-border`, …
 *   - Chatwoot `n-*` scales read RGB vars w/ alpha support:
 *     `rgb(var(--slate-9) / <alpha-value>)` → `bg-n-blue-9`, `text-n-slate-11`.
 * Components must use these utilities and avoid hard-coded hex / `slate-XXX`.
 */

/** `rgb(var(--name) / <alpha-value>)` so Tailwind opacity modifiers work. */
const rgbVar = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;
/** A full 1–12 Radix scale backed by `--{name}-{step}` RGB vars. */
const nScale = (name: string) =>
  Object.fromEntries(
    Array.from({ length: 12 }, (_, i) => [
      `${i + 1}`,
      rgbVar(`${name}-${i + 1}`),
    ]),
  );

const config: Config = {
  darkMode: ["class"],
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    container: {
      center: true,
      padding: "2rem",
      screens: { "2xl": "1400px" },
    },
    extend: {
      colors: {
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        warning: {
          DEFAULT: "hsl(var(--warning))",
          foreground: "hsl(var(--warning-foreground))",
        },
        success: {
          DEFAULT: "hsl(var(--success))",
          foreground: "hsl(var(--success-foreground))",
        },
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        // Chatwoot "next" scales — first-class n-* classes.
        "n-slate": nScale("slate"),
        "n-blue": nScale("blue"),
        "n-ruby": nScale("ruby"),
        "n-amber": nScale("amber"),
        "n-teal": nScale("teal"),
        "n-iris": nScale("iris"),
        "n-gray": nScale("gray"),
        "n-violet": nScale("violet"),
        "n-brand": rgbVar("blue-9"),
        "n-background": rgbVar("background-color"),
        "n-surface-2": rgbVar("surface-2"),
        "n-solid-2": rgbVar("solid-2"),
        "n-weak": rgbVar("border-weak"),
        "n-strong": rgbVar("border-strong"),
        "n-container": rgbVar("border-container"),
      },
      fontFamily: {
        sans: [
          "var(--font-inter)",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
      },
      fontWeight: {
        420: "420",
        440: "440",
        460: "460",
        520: "520",
        620: "620",
      },
      borderRadius: {
        xl: "calc(var(--radius) + 4px)",
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      keyframes: {
        "accordion-down": {
          from: { height: "0" },
          to: { height: "var(--radix-accordion-content-height)" },
        },
        "accordion-up": {
          from: { height: "var(--radix-accordion-content-height)" },
          to: { height: "0" },
        },
      },
      animation: {
        "accordion-down": "accordion-down 0.2s ease-out",
        "accordion-up": "accordion-up 0.2s ease-out",
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
};

export default config;
