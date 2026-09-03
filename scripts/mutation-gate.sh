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

LOG="$(mktemp "${TMPDIR:-/tmp}/config-saver-mutation.XXXXXX.log")"
trap 'rm -f "$LOG"' EXIT

if [ "${1:-}" = "--report" ]; then
  mutmut results
  exit 0
fi

echo ">> mutación sobre la lógica pura (umbral ${THRESHOLD}%)…"
rm -rf mutants .mutmut-cache
# `mutmut run` sale 0 aunque sobrevivan mutantes: el veredicto NO puede ser su
# código de salida. Y tampoco puede venir de `mutmut results`, que en esta versión
# lista SOLO los supervivientes — leerlo como si fuera el recuento completo da
# "0 muertos / 18 vivos = 0%" sobre una corrida que mató 243. El recuento bueno es
# el marcador que `mutmut run` imprime al terminar:
#     261/2533  🎉 243 🫥 0  ⏰ 0  🤔 0  🙁 18  🔇 0  🧙 0
"${RUN[@]}" mutmut run --max-children 4 "${SCOPE[@]}" 2>&1 | tee "$LOG" | tail -3 || true

# El veredicto se calcula sobre killed vs survived y NO cuenta los timeouts como
# muertos: si la corrida se ahoga, el gate tiene que notarse ahogado, no aprobar
# con matrícula. (Aquí ya pasó una vez: una corrida hambrienta puntuó 139 de 142
# mutantes como "killed" porque expiraron.)
THRESHOLD="$THRESHOLD" python3 - "$LOG" <<'PYEOF'
import re, sys, os

text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
# Última línea de marcador que imprime `mutmut run`. Se lee por PAREJAS emoji-número,
# no por posición, para que añadir un estado nuevo no descoloque el recuento.
EMOJI = {"🎉": "killed", "🫥": "no tests", "⏰": "timeout",
         "🤔": "suspicious", "🙁": "survived", "🔇": "skipped",
         "🧙": "skipped (mutmut)"}
tallies = [l for l in text.splitlines() if "🎉" in l and re.search(r"\d+/\d+", l)]
if not tallies:
    print("!! mutmut no imprimió ningún marcador de resultados: la corrida está rota,", file=sys.stderr)
    print("   o esta versión imprime en un formato que este script no entiende.", file=sys.stderr)
    print("   Compruébalo a mano con `mutmut run` antes de tocar los tests.", file=sys.stderr)
    sys.exit(1)
counts = {}
for emoji, num in re.findall(r"([🎉🫥⏰🤔🙁🔇🧙])\s*(\d+)", tallies[-1]):
    counts[EMOJI[emoji]] = int(num)
killed, survived = counts.get("killed", 0), counts.get("survived", 0)
graded = killed + survived
thr = float(os.environ["THRESHOLD"])
print("   " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()) if v))
if graded == 0:
    print("!! ni un mutante puntuado: corrida rota, no limpia.", file=sys.stderr)
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
PYEOF
