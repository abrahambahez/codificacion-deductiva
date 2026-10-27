#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Codificación asistida por Claude de las entrevistas anonimizadas con el libro de `03 Codigos`.

El flujo tiene tres pasos y nada toca las copias de trabajo hasta el tercero:

1. Proponer: Claude Code headless lee cada entrevista completa (todas sus partes) con el libro de
   códigos y `CONTEXTO.md`, y deja una nota de revisión por audio en `04 Codificación`, con una
   propuesta por callout y una casilla para aprobarla.
2. Revisar: marcas las casillas de las propuestas que aceptas. Puedes editar el indicador del título
   y el eje; la ruta Dimensión › Categoría se recalcula al aplicar.
3. Aplicar: cada fragmento aceptado queda como párrafo propio en la copia de trabajo de `05 Datos` y
   termina en ` [[Dimensión]] [[Categoría]] [[Indicador]] #eje/… %%ia: Indicador · Ejes — justificación%%`,
   de modo que los retroenlaces de cada nivel del libro muestran sus fragmentos.

Revisión, copia de trabajo y transcripción anonimizada se emparejan por el `audio_id` de su
frontmatter, nunca por nombre. El comentario `%%ia: …%%` marca lo que insertó el script: `--revertir`
quita los enlaces y etiquetas derivados de esas entradas y las líneas en blanco que ya no separan
ningún párrafo codificado. Los enlaces que escribas a mano no se tocan.

Solo salen hacia Claude las entrevistas de `02 Anonimizadas` con `anonimizada: true`.

Uso:
    uv run Scripts/codificar.py                   # propone para las entrevistas con audios sin revisión
    uv run Scripts/codificar.py "Entrevista B"    # solo las rutas que contengan ese texto
    uv run Scripts/codificar.py --model opus      # elige el modelo de Claude Code
    uv run Scripts/codificar.py --force ...       # vuelve a proponer y sobrescribe las revisiones
    uv run Scripts/codificar.py --aplicar         # inserta las propuestas aprobadas
    uv run Scripts/codificar.py --revertir [...]  # quita lo que insertó el script
    uv run Scripts/codificar.py --dry-run ...     # muestra qué haría sin llamar a Claude ni escribir
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path

VAULT = Path(__file__).resolve().parent.parent
ANONIMIZADAS = VAULT / "02 Anonimizadas"
CODIGOS = VAULT / "03 Codigos"
REVISION = VAULT / "04 Codificación"
DATOS = VAULT / "05 Datos"
CONTEXTO = VAULT / "CONTEXTO.md"
PREFIJO_REVISION = "Codificación - "

MODEL = "claude-sonnet-5"
EFFORT = "high"
SECCION = 150
MAX_EXTENSION = 12
EJES = ["Antes", "Ahora", "Recorrido"]
SEP = " › "

RE_TS = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]")
RE_IA = re.compile(r"^(.*?) %%ia: (.*)%%$")
RE_LINK = re.compile(r"\[\[([^\]|#]+)")
RE_TITULO = re.compile(r"^> \[!note\] Indicador: \*?(.+?)\*?\s*$")
RE_CITA = re.compile(r"^> > .*\[(\d{2}:\d{2}:\d{2})\]–\[(\d{2}:\d{2}:\d{2})\] %%L(\d+)–L(\d+)%%\s*$")
RE_EJE = re.compile(r"^> - Eje: (.*)$")
RE_JUSTIFICACION = re.compile(r"^> - \*Justificación:\* (.*)$")
RE_APROBAR = re.compile(r"^> - \[( |x|X)\] Aprobar")


def leer_frontmatter(path: Path) -> dict[str, str]:
    """Pares `clave: valor` del frontmatter, sin interpretar YAML."""
    lineas = path.read_text(encoding="utf-8").splitlines()
    if not lineas or lineas[0] != "---":
        return {}
    datos = {}
    for linea in lineas[1:]:
        if linea == "---":
            break
        clave, _, valor = linea.partition(":")
        datos[clave.strip()] = valor.strip().strip('"')
    return datos


def por_audio_id(rutas) -> dict[str, Path]:
    return {audio_id: p for p in rutas if (audio_id := leer_frontmatter(p).get("audio_id"))}


def quitar_frontmatter(texto: str) -> str:
    m = re.match(r"---\n.*?\n---\n?", texto, re.S)
    return texto[m.end():] if m else texto



@dataclass
class Libro:
    texto: str
    rutas: dict[str, tuple[str, str]]

    def enlaces(self, indicador: str) -> list[str]:
        dimension, categoria = self.rutas[indicador]
        return [f"[[{dimension}]]", f"[[{categoria}]]", f"[[{indicador}]]"]


def cargar_libro() -> Libro:
    """Lee las notas de codificación: su texto para Claude y la jerarquía indicador → (dimensión, categoría)."""
    textos, rutas = [], {}
    for path in sorted(CODIGOS.glob("*.md")):
        if leer_frontmatter(path).get("tipo") != "codificación":
            continue
        texto = quitar_frontmatter(path.read_text(encoding="utf-8")).strip()
        textos.append(f'<dimension nombre="{path.stem}">\n{texto}\n</dimension>')
        categoria, en_indicadores = None, False
        for linea in texto.splitlines():
            if linea.startswith("## "):
                categoria, en_indicadores = linea[3:].strip(), False
            elif linea.startswith("### "):
                en_indicadores = linea.strip() == "### Indicadores"
            elif en_indicadores and (m := re.match(r"- \[\[(.+?)\]\]:", linea)):
                rutas[m.group(1).strip()] = (path.stem, categoria)
    if not rutas:
        sys.exit(f"No hay indicadores en las notas de codificación de {CODIGOS.relative_to(VAULT)}")
    return Libro("\n\n".join(textos), rutas)



@dataclass
class Entrada:
    indicador: str
    ejes: list[str]
    justificacion: str

    def comentario(self) -> str:
        return f"{self.indicador} · {', '.join(self.ejes)} — {self.justificacion}"


def limpiar(texto: str) -> str:
    """Deja el texto apto para ir dentro de `%%ia: …%%` (una línea, sin `%` ni `|`)."""
    return re.sub(r"\s+", " ", texto.replace("%", " por ciento").replace("|", "/")).strip()


def derivados(entradas: list[Entrada], libro: Libro) -> list[str]:
    """Enlaces y etiquetas que generan las entradas, sin repetir y en orden."""
    enlaces = [e for entrada in entradas for e in libro.enlaces(entrada.indicador)]
    etiquetas = [f"#eje/{eje.lower()}" for entrada in entradas for eje in entrada.ejes]
    return list(dict.fromkeys(enlaces + etiquetas))


def separar_ia(linea: str, libro: Libro) -> tuple[str, list[Entrada]]:
    """Separa una línea en su texto base y las entradas de su sufijo `[[…]] #eje/… %%ia: …%%`."""
    m = RE_IA.match(linea)
    if not m:
        return linea, []
    base, entradas = m.group(1), []
    for parte in m.group(2).split(" | "):
        cabeza, _, justificacion = parte.partition(" — ")
        indicador, _, ejes = cabeza.partition(" · ")
        entradas.append(Entrada(indicador, ejes.split(", "), justificacion))
    if desconocidos := [e.indicador for e in entradas if e.indicador not in libro.rutas]:
        sys.exit(f"Indicadores aplicados que ya no están en el libro: {', '.join(desconocidos)}\n  {linea}")
    sufijo = " " + " ".join(derivados(entradas, libro))
    if not base.endswith(sufijo):
        sys.exit(f"No reconozco el sufijo derivado de la línea:\n  {linea}")
    return base.removesuffix(sufijo), entradas


def unir_ia(base: str, entradas: list[Entrada], libro: Libro) -> str:
    if not entradas:
        return base
    return f"{base} {' '.join(derivados(entradas, libro))} %%ia: {' | '.join(e.comentario() for e in entradas)}%%"


@dataclass
class Nota:
    path: Path
    lineas: list[str]
    inicio_cuerpo: int

    @classmethod
    def leer(cls, path: Path) -> Nota:
        lineas = path.read_text(encoding="utf-8").splitlines()
        inicio = 0
        if lineas and lineas[0] == "---":
            cierre = next((i for i in range(1, len(lineas)) if lineas[i] == "---"), None)
            if cierre is not None:
                inicio = cierre + 1
        return cls(path, lineas, inicio)

    def escribir(self) -> None:
        self.path.write_text("\n".join(self.lineas) + "\n", encoding="utf-8")

    def indices(self) -> list[int]:
        """Índices de las líneas con timestamp; la línea L(k) es indices()[k-1]."""
        return [i for i in range(self.inicio_cuerpo, len(self.lineas)) if RE_TS.match(self.lineas[i])]

    def parrafo(self, i: int) -> tuple[int, int]:
        a = b = i
        while a - 1 >= self.inicio_cuerpo and self.lineas[a - 1].strip():
            a -= 1
        while b + 1 < len(self.lineas) and self.lineas[b + 1].strip():
            b += 1
        return a, b

    def codificado(self, a: int, b: int) -> bool:
        return any(RE_LINK.search(self.lineas[i]) for i in range(a, b + 1))



@dataclass
class Propuesta:
    inicio: int
    fin: int
    ts_inicio: str
    ts_fin: str
    entrada: Entrada
    aceptada: bool = False


@dataclass
class Parte:
    path: Path
    audio_id: str
    textos: list[str]


INSTRUCCIONES = """\
Eres asistente de codificación cualitativa deductiva. Tu tarea es proponer fragmentos de las \
transcripciones de una entrevista y asignarles indicadores de un libro de códigos cerrado. Quien \
investiga revisará cada propuesta antes de aplicarla.

El libro tiene tres niveles: Dimensión › Categoría › Indicador. Codificas siempre con un indicador; \
la dimensión y la categoría se deducen de él. Cada categoría trae su definición operativa, cuándo \
aplicarla, cuándo no aplicarla (con las categorías cercanas, también de otras dimensiones) y sus \
indicadores con una descripción.

Cómo codificar:
- La unidad es un fragmento con sentido completo: una o varias líneas consecutivas de una misma \
parte (normalmente entre 1 y 8). Las líneas vienen de un reconocimiento de voz que corta frases a la \
mitad, así que empieza y termina el fragmento donde empieza y termina la idea, no donde cortan las \
líneas.
- Las transcripciones no marcan quién habla. Codifica lo que la persona entrevistada dice, vive o \
piensa, incluido lo que quien entrevista relata en tercera persona sobre ella. No codifiques las \
preguntas ni el contexto que aporta quien entrevista (lugar, fecha, cómo llegaron).
- Aplica un indicador solo si el fragmento cumple con el "Cuándo aplicar" de su categoría y con la \
descripción del indicador. Respeta las fronteras de "Cuándo no aplicar", incluida la codificación \
doble cuando la categoría la prevé. Si un fragmento no encaja con claridad, no lo codifiques: es \
mejor quedarse corto que inflar la codificación.
- Antes de elegir un indicador, lee el "Cuándo no aplicar" de su categoría y de las categorías que \
enlaza: decide qué dice el fragmento, quién interviene y de dónde viene lo que vive. Cuando la \
regla dice "aplica también" o "aplica ambas", devuelve el fragmento una vez por cada indicador, con el mismo rango.
- Cada fragmento lleva uno o más ejes temporales:
  - Antes: cómo era su situación antes de involucrarse en la práctica que se estudia.
  - Ahora: cómo lo vive en el presente.
  - Recorrido: cómo llegó, qué pasó en el camino, pausas o cambios. Un episodio que ocurrió ya \
dentro de su trayectoria en la práctica es Recorrido aunque se cuente en pasado.
- La justificación es una sola oración en español que señala qué del fragmento sostiene el indicador.

Los objetivos de la investigación que siguen orientan tu lectura, pero no son códigos: no busques \
fragmentos sobre los temas de los objetivos como tales, sino lo que el libro de códigos define.

<contexto>
{contexto}
</contexto>

Libro de códigos:

{libro}
"""


def esquema(libro: Libro, partes: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "fragmentos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "parte": {"type": "integer", "minimum": 1, "maximum": partes},
                        "inicio": {"type": "integer", "description": "número de la primera línea (L)"},
                        "fin": {"type": "integer", "description": "número de la última línea (L)"},
                        "indicador": {"type": "string", "enum": sorted(libro.rutas)},
                        "eje": {
                            "type": "array",
                            "items": {"type": "string", "enum": EJES},
                            "minItems": 1,
                            "uniqueItems": True,
                        },
                        "justificacion": {"type": "string"},
                    },
                    "required": ["parte", "inicio", "fin", "indicador", "eje", "justificacion"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["fragmentos"],
        "additionalProperties": False,
    }


def mensaje(partes: list[Parte]) -> str:
    bloques = []
    for n, parte in enumerate(partes, 1):
        numerado = "\n".join(f"L{k} {t}" for k, t in enumerate(parte.textos, 1))
        bloques.append(f'<parte numero="{n}" etiqueta="{parte.path.stem}">\n{numerado}\n</parte>')
    return "\n\n".join(bloques)


class Sesion:
    """Una sesión de Claude Code headless por entrevista, sin herramientas y fuera del vault."""

    def __init__(self, directorio: Path, sistema: str, schema: dict, model: str):
        self.directorio = directorio
        self.id = str(uuid.uuid4())
        self.iniciada = False
        (directorio / "sistema.md").write_text(sistema, encoding="utf-8")
        self.base = [
            "claude", "-p", "--tools", "", "--model", model, "--effort", EFFORT,
            "--output-format", "json", "--json-schema", json.dumps(schema, ensure_ascii=False),
            "--system-prompt-file", str(directorio / "sistema.md"),
        ]

    def pedir(self, texto: str) -> list[dict] | None:
        """Devuelve los fragmentos, o None si la respuesta se cortó antes de completar el esquema."""
        sesion = ["--resume", self.id] if self.iniciada else ["--session-id", self.id]
        res = subprocess.run(
            self.base + sesion, input=texto, capture_output=True, text=True, cwd=self.directorio,
        )
        self.iniciada = True
        try:
            datos = json.loads(res.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"claude terminó con código {res.returncode}: {res.stderr.strip() or res.stdout.strip()}")
        if "structured_output" in datos:
            return datos["structured_output"]["fragmentos"]
        if datos.get("api_error_status"):
            raise RuntimeError(f"error de la API ({datos['api_error_status']}): {datos.get('result')}")
        print(f"    AVISO: respuesta cortada ({datos.get('subtype')}, {datos.get('stop_reason')})", file=sys.stderr)
        return None


def secciones(partes: list[Parte]) -> list[tuple[str, str]]:
    """Pares (rango, tarea) para volver a pedir la entrevista por secciones dentro de la misma sesión."""
    tareas = []
    for n, parte in enumerate(partes, 1):
        total = len(parte.textos)
        for desde in range(1, total + 1, SECCION):
            hasta = min(desde + SECCION - 1, total)
            tareas.append((
                f"parte {n} L{desde}–L{hasta}",
                f"La respuesta anterior se cortó. Propón solo los fragmentos de la parte {n} que empiezan "
                f"entre L{desde} y L{hasta}. Usa toda la entrevista como contexto. Un fragmento puede "
                f"terminar después de L{hasta} si la idea continúa, hasta L{min(hasta + MAX_EXTENSION, total)} "
                f"como máximo.",
            ))
    return tareas


def proponer(partes: list[Parte], libro: Libro, model: str) -> list[list[Propuesta]]:
    """Una lista de propuestas por parte, en el orden de `partes`."""
    sistema = INSTRUCCIONES.format(contexto=CONTEXTO.read_text(encoding="utf-8").strip(), libro=libro.texto)
    tarea = "Propón los fragmentos codificables de todas las partes de esta entrevista.\n\n" + mensaje(partes)
    with tempfile.TemporaryDirectory(prefix="codificar-") as tmp:
        sesion = Sesion(Path(tmp), sistema, esquema(libro, len(partes)), model)
        fragmentos = sesion.pedir(tarea)
        if fragmentos is None:
            fragmentos = []
            for rango, texto in secciones(partes):
                print(f"    {rango}", flush=True)
                seccion = sesion.pedir(texto)
                if seccion is None:
                    raise RuntimeError(f"la sección {rango} también se cortó; la entrevista sigue pendiente")
                fragmentos += seccion

    propuestas: list[list[Propuesta]] = [[] for _ in partes]
    vistas: set[tuple[int, int, int, str]] = set()
    for f in fragmentos:
        n, ini, fin = f["parte"], f["inicio"], f["fin"]
        clave = (n, ini, fin, f["indicador"])
        if not (1 <= n <= len(partes) and 1 <= ini <= fin <= len(partes[n - 1].textos)):
            print(f"    descartado (fuera de rango): parte {n} L{ini}–L{fin} {f['indicador']}", file=sys.stderr)
            continue
        if clave in vistas:
            continue
        vistas.add(clave)
        textos = partes[n - 1].textos
        propuestas[n - 1].append(Propuesta(
            ini, fin,
            RE_TS.match(textos[ini - 1]).group(1), RE_TS.match(textos[fin - 1]).group(1),
            Entrada(f["indicador"], [e for e in EJES if e in f["eje"]], limpiar(f["justificacion"])),
        ))
    for lista in propuestas:
        lista.sort(key=lambda p: (p.inicio, p.fin, p.entrada.indicador))
    return propuestas



def escribir_revision(parte: Parte, propuestas: list[Propuesta], libro: Libro, model: str, destino: Path) -> None:
    salida = [
        "---",
        "tipo: revisión de codificación",
        f"audio_id: {parte.audio_id}",
        f"modelo: {model}",
        f"generado: {date.today().isoformat()}",
        "---",
        "",
        "Marca la casilla de las propuestas que aceptas y ejecuta `uv run Scripts/codificar.py --aplicar`. "
        "Puedes editar el indicador del título y el eje.",
    ]
    for p in propuestas:
        cita = " ".join(RE_TS.sub("", parte.textos[k - 1]).strip() for k in range(p.inicio, p.fin + 1))
        dimension, categoria = libro.rutas[p.entrada.indicador]
        salida += [
            "",
            f"> [!note] Indicador: *{p.entrada.indicador}*",
            f"> > {cita} [{p.ts_inicio}]–[{p.ts_fin}] %%L{p.inicio}–L{p.fin}%%",
            f"> - {dimension}{SEP}{categoria}",
            f"> - Eje: {', '.join(p.entrada.ejes)}",
            f"> - *Justificación:* {p.entrada.justificacion}",
            "> - [ ] Aprobar",
        ]
    destino.parent.mkdir(exist_ok=True)
    destino.write_text("\n".join(salida) + "\n", encoding="utf-8")


def leer_revision(path: Path) -> list[Propuesta]:
    propuestas: list[Propuesta] = []
    for linea in path.read_text(encoding="utf-8").splitlines():
        if m := RE_TITULO.match(linea):
            propuestas.append(Propuesta(0, 0, "", "", Entrada(m.group(1).strip(), [], "")))
        elif not propuestas:
            continue
        elif m := RE_CITA.match(linea):
            p = propuestas[-1]
            p.ts_inicio, p.ts_fin, p.inicio, p.fin = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
        elif m := RE_EJE.match(linea):
            propuestas[-1].entrada.ejes = [e.strip().capitalize() for e in m.group(1).split(",") if e.strip()]
        elif m := RE_JUSTIFICACION.match(linea):
            propuestas[-1].entrada.justificacion = limpiar(m.group(1))
        elif m := RE_APROBAR.match(linea):
            propuestas[-1].aceptada = m.group(1).lower() == "x"
    return propuestas



def aplicar(nota: Nota, propuestas: list[Propuesta], libro: Libro) -> tuple[int, list[str]]:
    """Inserta las propuestas aceptadas. Devuelve (entradas insertadas, avisos)."""
    avisos: list[str] = []
    indices = nota.indices()
    grupos: list[list[int | list[Entrada]]] = []
    for p in propuestas:
        if not p.aceptada:
            continue
        etiqueta = f"L{p.inicio}–L{p.fin} {p.entrada.indicador}"
        if p.entrada.indicador not in libro.rutas:
            avisos.append(f"{etiqueta}: el indicador no existe en el libro de códigos")
            continue
        if not p.entrada.ejes or any(e not in EJES for e in p.entrada.ejes):
            avisos.append(f"{etiqueta}: el eje debe ser uno o más de {', '.join(EJES)}")
            continue
        if not (1 <= p.inicio <= p.fin <= len(indices)) or not (
            nota.lineas[indices[p.inicio - 1]].startswith(f"[{p.ts_inicio}]")
            and nota.lineas[indices[p.fin - 1]].startswith(f"[{p.ts_fin}]")
        ):
            avisos.append(f"{etiqueta}: la copia cambió desde la propuesta (los timestamps no coinciden)")
            continue
        grupos.append([p.inicio, p.fin, [p.entrada]])

    ordinal = {i: k for k, i in enumerate(indices, 1)}
    cambio = True
    while cambio:
        cambio = False
        for g in grupos:
            for k in (g[0], g[1]):
                a, b = nota.parrafo(indices[k - 1])
                if not nota.codificado(a, b):
                    continue
                ks = [ordinal[i] for i in range(a, b + 1) if i in ordinal]
                nuevo = (min(g[0], ks[0]), max(g[1], ks[-1]))
                if nuevo != (g[0], g[1]):
                    g[0], g[1], cambio = *nuevo, True
        grupos.sort(key=lambda g: g[0])
        fusion: list = []
        for g in grupos:
            if fusion and g[0] <= fusion[-1][1]:
                fusion[-1][1] = max(fusion[-1][1], g[1])
                fusion[-1][2] += g[2]
                cambio = True
            else:
                fusion.append(g)
        grupos = fusion

    insertadas = 0
    for ini, fin, entradas in reversed(grupos):
        a, b = indices[ini - 1], indices[fin - 1]
        a_mano, previas_ia = set(), set()
        for i in range(a, b + 1):
            base, ents = separar_ia(nota.lineas[i], libro)
            a_mano |= {m.strip() for m in RE_LINK.findall(base)}
            previas_ia |= {e.indicador for e in ents}
        nuevas = []
        for e in entradas:
            if e.indicador not in a_mano and e.indicador not in previas_ia:
                previas_ia.add(e.indicador)
                nuevas.append(e)
        if nuevas:
            base, previas = separar_ia(nota.lineas[b], libro)
            nota.lineas[b] = unir_ia(base, previas + nuevas, libro)
            insertadas += len(nuevas)
        if b + 1 < len(nota.lineas) and nota.lineas[b + 1].strip():
            nota.lineas.insert(b + 1, "")
        for i in range(b - 1, a, -1):
            if not nota.lineas[i].strip():
                del nota.lineas[i]
        if a - 1 >= nota.inicio_cuerpo and nota.lineas[a - 1].strip():
            nota.lineas.insert(a, "")
    return insertadas, avisos


def revertir(nota: Nota, libro: Libro) -> int:
    quitadas = 0
    for i, linea in enumerate(nota.lineas):
        base, entradas = separar_ia(linea, libro)
        if entradas:
            nota.lineas[i] = base
            quitadas += len(entradas)
    i = nota.inicio_cuerpo + 1
    while i < len(nota.lineas) - 1:
        if not nota.lineas[i].strip() and nota.lineas[i - 1].strip() and nota.lineas[i + 1].strip():
            if not (nota.codificado(*nota.parrafo(i - 1)) or nota.codificado(*nota.parrafo(i + 1))):
                del nota.lineas[i]
                continue
        i += 1
    return quitadas



def seleccionar(rutas, seleccion: list[str]) -> list[Path]:
    return sorted(p for p in rutas if not seleccion or any(s in p.relative_to(VAULT).as_posix() for s in seleccion))


def main_proponer(args, libro: Libro) -> None:
    revisiones = por_audio_id(REVISION.glob("*.md"))
    entrevistas: dict[Path, list[Path]] = {}
    for txt in sorted(ANONIMIZADAS.rglob("*.txt")):
        entrevistas.setdefault(txt.parent, []).append(txt)
    elegidas = {t.parent for t in seleccionar(ANONIMIZADAS.rglob("*.txt"), args.seleccion)}
    entrevistas = {c: txts for c, txts in entrevistas.items() if c in elegidas}

    pendientes = []
    for carpeta, txts in entrevistas.items():
        nombre = carpeta.relative_to(ANONIMIZADAS).as_posix()
        if any(leer_frontmatter(t).get("anonimizada") != "true" for t in txts):
            print(f"rechazada (no está anonimizada): {nombre}", file=sys.stderr)
        elif args.force or any(leer_frontmatter(t).get("audio_id") not in revisiones for t in txts):
            pendientes.append((nombre, txts))
        else:
            print(f"omitida (ya tiene revisiones): {nombre}")

    if args.dry_run or not pendientes:
        for nombre, txts in pendientes:
            print(f"pendiente: {nombre} ({len(txts)} partes) con {args.model}")
        return

    for nombre, txts in pendientes:
        print(f"codificando: {nombre} ({len(txts)} partes) con {args.model}", flush=True)
        partes = []
        for txt in txts:
            nota = Nota.leer(txt)
            partes.append(Parte(txt, leer_frontmatter(txt)["audio_id"], [nota.lineas[i] for i in nota.indices()]))
        try:
            propuestas = proponer(partes, libro, args.model)
        except RuntimeError as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            continue
        for parte, lista in zip(partes, propuestas):
            if parte.audio_id in revisiones and not args.force:
                continue
            destino = revisiones.get(parte.audio_id) or REVISION / f"{PREFIJO_REVISION}{parte.path.stem}.md"
            escribir_revision(parte, lista, libro, args.model, destino)
            print(f"  → {destino.relative_to(VAULT)} ({len(lista)} propuestas)")


def main_aplicar(args, libro: Libro) -> None:
    copias = por_audio_id(DATOS.glob("*.md"))
    anonimizadas = por_audio_id(ANONIMIZADAS.rglob("*.txt"))
    for revision in seleccionar(REVISION.glob("*.md"), args.seleccion):
        audio_id = leer_frontmatter(revision).get("audio_id")
        if not audio_id:
            continue
        copia = copias.get(audio_id)
        nueva = not copia
        if copia:
            nota = Nota.leer(copia)
        elif audio_id in anonimizadas:
            nota = Nota.leer(anonimizadas[audio_id])
            nota.path = DATOS / f"{anonimizadas[audio_id].stem}.md"
        else:
            print(f"  AVISO {revision.stem}: no hay transcripción anonimizada con su audio_id", file=sys.stderr)
            continue
        copia = nota.path
        n, avisos = aplicar(nota, leer_revision(revision), libro)
        for a in avisos:
            print(f"  AVISO {copia.stem}: {a}", file=sys.stderr)
        if n or nueva:
            print(f"{copia.relative_to(VAULT)}: {n} entradas {'por insertar' if args.dry_run else 'insertadas'}")
            if not args.dry_run:
                DATOS.mkdir(exist_ok=True)
                nota.escribir()


def main_revertir(args, libro: Libro) -> None:
    for path in seleccionar(DATOS.glob("*.md"), args.seleccion):
        nota = Nota.leer(path)
        n = revertir(nota, libro)
        if n:
            print(f"{path.relative_to(VAULT)}: {n} entradas {'por quitar' if args.dry_run else 'quitadas'}")
            if not args.dry_run:
                nota.escribir()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("seleccion", nargs="*", help="texto que debe aparecer en la ruta (por defecto, todas)")
    modo = ap.add_mutually_exclusive_group()
    modo.add_argument("--aplicar", action="store_true", help="inserta las propuestas aprobadas")
    modo.add_argument("--revertir", action="store_true", help="quita los enlaces, etiquetas y comentarios %%ia%%")
    ap.add_argument("--model", default=MODEL, help=f"modelo de Claude Code (por defecto {MODEL})")
    ap.add_argument("--force", action="store_true", help="vuelve a proponer aunque existan las revisiones")
    ap.add_argument("--dry-run", action="store_true", help="muestra qué haría sin llamar a Claude ni escribir")
    args = ap.parse_args()

    libro = cargar_libro()
    if args.revertir:
        main_revertir(args, libro)
    elif args.aplicar:
        main_aplicar(args, libro)
    else:
        main_proponer(args, libro)


if __name__ == "__main__":
    main()
