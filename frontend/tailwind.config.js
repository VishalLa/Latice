/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: [
    './index.html',
    './src/**/*.{vue,js,ts,jsx,tsx}',
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'],
      },
      colors: {
        // Brand / logo marks
        brand: {
          indigo: '#444CE7',
          purple: '#B07BF0',
          blue: '#8098F9',
          glow: '#4E52F8',
          cyan: '#82F9F9',
        },
        // Dark mode surfaces (from Desktop-1 screenshot)
        ink: {
          900: '#0F172A', // deepest navy — header / logo plate
          800: '#1C2E53', // hero gradient base
          700: '#243456',
          muted: '#666666', // dark-mode secondary text / nav links
        },
        // Light mode surfaces (from LIGHT MODE screenshot)
        mist: {
          50: '#F8F8F8',  // page background
          100: '#E8F8F8', // card tint
          200: '#D8E8F8', // card tint alt
          accent: '#75AEE5', // light-mode link/accent
        },
      },
      backgroundImage: {
        'hero-dark': 'linear-gradient(180deg, #0F172A 0%, #1C2E53 55%, #1C2E53 100%)',
        'hero-glow': 'radial-gradient(60% 80% at 50% 0%, rgba(130,249,249,0.35) 0%, rgba(176,123,240,0.18) 35%, rgba(15,23,42,0) 70%)',
        'beam': 'linear-gradient(180deg, rgba(255,255,255,0.95) 0%, rgba(130,249,249,0.5) 40%, rgba(68,76,231,0) 100%)',
      },
      borderRadius: {
        card: '14px',
        pill: '999px',
      },
      boxShadow: {
        glow: '0 0 60px 10px rgba(130,249,249,0.15)',
      },
    },
  },
  plugins: [],
}
