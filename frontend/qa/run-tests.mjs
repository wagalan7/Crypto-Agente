// Runner de QA do Lote 04 — SÓ dependências já instaladas (esbuild do Vite +
// node:test). Não instala nada, não baixa pacote, não escreve em dist.
//
//   cd frontend && node qa/run-tests.mjs
//
// Empacota os testes (TS/TSX) num diretório temporário exclusivo e roda o
// runner nativo do Node. Saída: exit 0 = tudo verde.
import { execFileSync } from 'node:child_process'
import { mkdirSync, readdirSync, rmSync } from 'node:fs'
import { join, resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const QA = dirname(fileURLToPath(import.meta.url))
const FRONTEND = resolve(QA, '..')
const ESBUILD = resolve(FRONTEND, 'node_modules/.bin/esbuild')
// Saída dentro de node_modules/.cache (já ignorado pelo git e nunca é `dist`):
// precisa estar sob o pacote para o Node resolver react/react-dom instalados.
const OUT = resolve(FRONTEND, 'node_modules/.cache/cw-qa-lote04')
rmSync(OUT, { recursive: true, force: true })
mkdirSync(OUT, { recursive: true })

const testes = readdirSync(join(QA, 'tests'))
  .filter(f => /\.test\.tsx?$/.test(f))
  .map(f => join(QA, 'tests', f))

if (testes.length === 0) {
  console.error('nenhum teste encontrado em qa/tests')
  process.exit(1)
}

console.log(`[qa] empacotando ${testes.length} arquivo(s) de teste em ${OUT}`)
execFileSync(ESBUILD, [
  ...testes,
  '--bundle',
  '--platform=node',
  '--format=esm',
  '--target=node20',
  '--jsx=automatic',
  // react / react-dom / lucide ficam EXTERNOS e são resolvidos do node_modules
  // já instalado (react-dom/server é CJS e usa require de builtins).
  '--packages=external',
  `--outdir=${OUT}`,
  '--out-extension:.js=.mjs',
  '--log-level=warning',
  // `import.meta.env` do Vite não existe no Node: o QA usa um host local
  // sentinela, nunca produção.
  '--define:import.meta.env={"VITE_API_URL":"http://127.0.0.1:5199","VITE_OBSERVATION_API_URL":""}',
  // Raiz do código-fonte para os testes de contrato (o bundle roda fora de src).
  `--define:__QA_SRC__=${JSON.stringify(resolve(FRONTEND, 'src'))}`,
], { stdio: 'inherit', cwd: FRONTEND })

const bundles = readdirSync(OUT).filter(f => f.endsWith('.mjs')).map(f => join(OUT, f))
console.log(`[qa] rodando node --test (${bundles.length} bundle(s))`)
execFileSync(process.execPath, ['--test', ...bundles], { stdio: 'inherit', cwd: FRONTEND })
