// Real TSX rendering with synthetic state; no browser, fetch, or dependency install.
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const Module = require('node:module')
const component = path.resolve(__dirname, '../src/components/InsightsPanel.tsx')
const localRequire = Module.createRequire(component)
const ts = localRequire('typescript')
const React = localRequire('react')
const { renderToStaticMarkup } = localRequire('react-dom/server')
// Vite's build-time env is absent in this Node-only test; effects are disabled.
const source = fs.readFileSync(component, 'utf8').replace('import.meta.env.VITE_API_URL', 'undefined')
const output = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText

global.fetch = () => { throw new Error('Network forbidden in R11B2 rendering test') }

function render(data) {
  let stateIndex = 0
  const hooks = {
    ...React,
    useState(initial) { return [stateIndex++ === 0 ? data : initial, () => {}] },
    useEffect() {},
    useCallback(fn) { return fn },
  }
  const compiled = new Module(component)
  compiled.filename = component
  compiled.require = name => name === 'react' ? hooks : localRequire(name)
  compiled._compile(output, `${component}.cjs`)
  return renderToStaticMarkup(React.createElement(compiled.exports.default, { onClose() {} }))
}

const base = { enabled: true, total_trades: 30, baseline_win_rate: 50, min_sample: 5,
  winning_combos: [], losing_combos: [] }
const missing = render({ ...base,
  overall: { win_rate_pct: null, total_r: null, avg_r: null },
  by_tier: { A: { trades: 30, wins: 30, losses: 0, win_rate: null, total_r: null, avg_r: null } },
})
assert.equal((missing.match(/—/g) || []).length, 5)
assert.ok(!missing.includes('NaN') && !missing.includes('Infinity'))
assert.ok(!missing.includes('+0.00R'))

const valid = render({ ...base,
  overall: { win_rate_pct: 0, total_r: 0, avg_r: -0.5 },
  by_tier: { A: { trades: 30, wins: 0, losses: 30, win_rate: 0, total_r: 0, avg_r: 0 } },
})
for (const text of ['0.0%', '+0.0R', '-0.50R', '0%', '+0.00R']) assert.ok(valid.includes(text), text)
assert.ok(!valid.includes('—'))
console.log('R11B2_INSIGHTS_RENDER_OK: null, zero and signed finite values')
