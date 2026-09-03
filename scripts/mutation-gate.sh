#!/usr/bin/env bash
# Mutation gate — CLAUDE.md § "Mutation gate — el suelo del 60%".
#
# La cobertura dice si una línea SE EJECUTÓ; la mutación dice si algún test se
# habría enterado de que esa línea estaba mal. Un test sin un solo assert da 100%
# de cobertura y 0% de mutación: por eso esta puerta existe aparte del gate de
# cobertura, y no en su lugar.
#
#   scripts/mutation-gate.sh            # corre y aplica el umbral (60 por defecto)
#   MUTATION_THRESHOLD=70 scripts/…     # sube el trinquete
#   scripts/mutation-gate.sh --report   # solo imprime el último resultado
#
# Requiere mutmut:  pip install -e '.[dev]'
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

THRESHOLD="${MUTATION_THRESHOLD:-93}"   # medido 2026-09-03 en CI: 243 muertos / 18 vivos = 93.1%

# El scope son los módulos PUROS (docs/TESTING.md § 3). Mutar las cáscaras de I/O
# produce sobre todo timeouts, y un timeout se apunta como "killed": el número
# sale precioso y no significa nada. Estrechar el scope es la palanca correcta
# cuando esto se hace lento; bajar el umbral no lo es.
SCOPE=(
  'config_saver.lib.utils.path_expander.*'
  'config_saver.lib.tar_compressor.tar_compressor.xǁTarCompressorǁ_normalize_path*'
  'config_saver.lib.tar_compressor.tar_decompressor.xǁTarDecompressorǁ_validate*'
)

# Sin cgroup, una corrida dimensionó su pool por el número de cores y tumbó la
# máquina — y la corrida hambrienta puntuó 139 de 142 mutantes como "killed"
# porque expiraron. Ese es el peor fallo posible: parece un resultado excelente.
if command -v systemd-run >/dev/null 2>&1; then
  RUN=(systemd-run --user --scope --quiet -p MemoryHigh=5G -p MemoryMax=6G -p MemorySwapMax=0 --)
else
  RUN=()
fi

command -v mutmut >/dev/null 2>&1 || {
  echo "error: mutmut no está instalado — pip install -e '.[dev]'" >&2; exit 127; }

if [ "${1:-}" != "--report" ]; then
  echo ">> mutación sobre la lógica pura (umbral ${THRESHOLD}%)…"
  rm -rf mutants .mutmut-cache
  "${RUN[@]}" mutmut run --max-children 4 "${SCOPE[@]}" || true   # mutmut sale 0 con supervivientes; el veredicto lo da `results`
fi

# El veredicto se calcula sobre killed vs survived y NO cuenta los timeouts como
# muertos, justo por lo de arriba: si la corrida se ahoga, el gate tiene que
# notarse ahogado, no aprobar con matrícula.
# El veredicto se calcula sobre killed vs survived y NO cuenta los timeouts como
# muertos, justo por lo de arriba: si la corrida se ahoga, el gate tiene que
# notarse ahogado, no aprobar con matrícula.
#
# mutmut 3.x lista cada mutante con un EMOJI, no con `nombre: estado`:
#   🎉 killed   🙁 survived   🫥 no tests   ⏰ timeout   🤔 suspicious   🔇 skipped
# Parsear "nombre: estado" (el formato de mutmut 2.x) no encuentra NADA aquí, que
# es como la primera versión de este script puntuó 0/0 sobre una corrida de 261
# mutantes. Se aceptan las dos formas para que un cambio de versión no vuelva a
# hacer que la puerta puntúe el vacío.
mutmut results | THRESHOLD="$THRESHOLD" python3 -c '
import re, sys, os
EMOJI = {"\U0001F389": "killed", "\U0001F641": "survived", "\U0001FAE5": "no tests",
         "\u23F0": "timeout", "\U0001F914": "suspicious", "\U0001F507": "skipped"}
statuses = {}
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    key = EMOJI.get(line[0])
    if key is None:
        m = re.search(r":\s*(killed|survived|timeout|suspicious|skipped|no tests)\b", line)
        key = m.group(1) if m else None
    if key:
        statuses[key] = statuses.get(key, 0) + 1
killed, survived = statuses.get("killed", 0), statuses.get("survived", 0)
graded = killed + survived
thr = float(os.environ["THRESHOLD"])
print("   " + (", ".join(f"{k}={v}" for k, v in sorted(statuses.items())) or "(sin mutantes)"))
if graded == 0:
    print("!! mutmut no puntuó ni un mutante: la corrida está rota, o esta versión de", file=sys.stderr)
    print("   mutmut imprime los resultados en un formato que este script no entiende.", file=sys.stderr)
    print("   Compruébalo a mano con `mutmut results` antes de tocar los tests.", file=sys.stderr)
    sys.exit(1)
score = 100.0 * killed / graded
print(f"   score = {killed}/{graded} = {score:.1f}%  (umbral {thr:.0f}%)")
if score < thr:
    print(f"""
!! El score de mutación ha caído por debajo del {thr:.0f}%.
   Un mutante vivo es código CUBIERTO PERO SIN VERIFICAR: la línea se ejecuta y
   ningún assert mira el resultado. Míralos con:
       mutmut results ; mutmut show "<nombre del mutante>"
   Se arregla afirmando sobre el valor concreto escrito a mano — no recalculándolo
   con la misma expresión que el código, que se mueve con la mutación y le da la razón.
   NO bajes el umbral para pasar: es exactamente lo que esta puerta impide.""", file=sys.stderr)
    sys.exit(1)
'
