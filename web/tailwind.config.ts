import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#07060a",
          900: "#0b0a0f",
          800: "#13111a",
          700: "#1a1722",
          600: "#231e2c",
          500: "#2e2838",
        },
        bone: {
          50: "#f7f5f1",
          100: "#ece9e2",
          200: "#d6d2c9",
          300: "#b9b3a8",
          400: "#8c867c",
        },
        maroon: {
          50: "#fdecef",
          100: "#fbd3da",
          200: "#f3a3b0",
          300: "#e36e83",
          400: "#c63d57",
          500: "#9a1c2e",
          600: "#7a1424",
          700: "#5b0e1a",
          800: "#3d0a13",
          900: "#22060b",
        },
      },
      fontFamily: {
        sans: ["var(--font-sans)", "ui-sans-serif", "system-ui"],
        display: ["var(--font-display)", "ui-sans-serif", "system-ui"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
      },
      letterSpacing: {
        brutal: "-0.04em",
        wider: "0.08em",
        widest: "0.18em",
      },
      keyframes: {
        flicker: {
          "0%,19%,21%,23%,25%,54%,56%,100%": { opacity: "1", filter: "drop-shadow(0 0 6px rgba(255,255,255,0.5))" },
          "20%,24%,55%": { opacity: "0.55", filter: "drop-shadow(0 0 0 rgba(255,255,255,0))" },
        },
        scan: {
          "0%": { transform: "translateY(-100%)" },
          "100%": { transform: "translateY(100vh)" },
        },
        bolt: {
          "0%": { strokeDashoffset: "1000", opacity: "0" },
          "10%": { opacity: "1" },
          "60%": { strokeDashoffset: "0", opacity: "1" },
          "70%,100%": { opacity: "0" },
        },
        pulse_ring: {
          "0%": { transform: "scale(0.95)", opacity: "0.6" },
          "70%": { transform: "scale(1.6)", opacity: "0" },
          "100%": { opacity: "0" },
        },
        marquee: {
          "0%": { transform: "translateX(0)" },
          "100%": { transform: "translateX(-50%)" },
        },
      },
      animation: {
        flicker: "flicker 4.5s linear infinite",
        scan: "scan 6s linear infinite",
        bolt: "bolt 1.6s ease-out forwards",
        "pulse-ring": "pulse_ring 2.4s cubic-bezier(0.4,0,0.6,1) infinite",
        marquee: "marquee 38s linear infinite",
      },
      backgroundImage: {
        "noise":
          "url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0.5 0'/></filter><rect width='100%' height='100%' filter='url(%23n)' opacity='0.55'/></svg>\")",
        "storm":
          "radial-gradient(60% 40% at 50% 0%, rgba(154,28,46,0.18) 0%, rgba(7,6,10,0) 70%), radial-gradient(40% 30% at 80% 100%, rgba(154,28,46,0.10) 0%, rgba(7,6,10,0) 70%)",
      },
    },
  },
  plugins: [],
};

export default config;
