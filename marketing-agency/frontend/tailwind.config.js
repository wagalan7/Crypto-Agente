/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#f0f4ff',
          100: '#e0e9ff',
          500: '#4f6ef7',
          600: '#3b5bdb',
          700: '#2c45b8',
          900: '#1a2a7a',
        },
      },
    },
  },
  plugins: [],
}
