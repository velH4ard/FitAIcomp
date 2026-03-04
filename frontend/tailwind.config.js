import daisyui from "daisyui";

/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./main.js", "./ui.js", "./api.js"],
  theme: {
    extend: {
      fontFamily: {
        body:    ["DM Sans", "system-ui", "sans-serif"],
        serif:   ["Fraunces", "Georgia", "serif"],
        sans:    ["DM Sans", "system-ui", "sans-serif"],
      },
      colors: {
        cream:      "#f7f4ef",
        "cream-deep": "#eee9e0",
        sage: {
          DEFAULT: "#7a9e7e",
          light:   "#b5ceb8",
          dark:    "#4d7253",
        },
        clay: {
          DEFAULT: "#c4836a",
          light:   "#e8c4b4",
        },
        oat:  "#d4c4a8",
        bark: "#5c4a35",
      },
      borderRadius: {
        card: "22px",
        pill: "999px",
        md:   "14px",
      },
      backdropBlur: {
        glass: "18px",
      },
      boxShadow: {
        card: "0 2px 14px rgba(92,74,53,0.08)",
        soft: "0 4px 24px rgba(92,74,53,0.10)",
      },
    },
  },
  plugins: [daisyui],
  daisyui: {
    themes: [
      {
        fitailight: {
          primary:          "#7a9e7e",
          "primary-focus":  "#4d7253",
          "primary-content": "#ffffff",
          secondary:        "#c4836a",
          accent:           "#d4c4a8",
          neutral:          "#5c4a35",
          "base-100":       "#f7f4ef",
          "base-200":       "#eee9e0",
          "base-300":       "#d4c4a8",
          "base-content":   "#5c4a35",
          info:             "#7a9e7e",
          success:          "#4d7253",
          warning:          "#c4836a",
          error:            "#b85a4a",
        },
      },
    ],
  },
};
