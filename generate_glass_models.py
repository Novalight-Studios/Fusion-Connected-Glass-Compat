#!/usr/bin/env python3
"""
Génère les fichiers Fusion (connected glass) d'un resource pack à partir des textures.

Pour chaque image trouvée dans assets/<namespace>/textures/block/** :
  - crée  <image>.png.mcmeta           (fusion connecting / pieced)
  - crée  models/block/<image>.json    (cube_all + fusion:connecting)
  - si <image>_edge.png existe, crée aussi la pane :
        models/block/templates/template_glass_pane_*.json  (si absents)
        models/block/panes/<image>_pane_*.json             (7 modèles)
        blockstates/<image>_pane.json
        fusion/model_modifiers/blocks/pane_culling_fix.json (cible ajoutée)

Pour chaque image, le script demande le render_type : translucent ou cutout.

Usage :
    python generate_glass_models.py                  # dossier courant (celui qui contient assets/)
    python generate_glass_models.py --root chemin/du/pack
    python generate_glass_models.py --dry-run        # n'écrit rien
    python generate_glass_models.py --force          # régénère même si le modèle existe
    python generate_glass_models.py -n undergarden   # un seul namespace
    python generate_glass_models.py --new            # crée dossiers + images transparentes
"""

import argparse
import json
import re
import struct
import sys
import zlib
from pathlib import Path

# --------------------------------------------------------------------------- #
# Templates de pane (identiques pour tous les blocs)
# --------------------------------------------------------------------------- #

TEMPLATES = {
    "template_glass_pane_noside": """{
  "ambientocclusion": false,
  "textures": {
    "particle": "#pane"
  },
  "elements": [
    {
      "from": [7, 0, 7],
      "to": [9, 16, 9],
      "faces": {
        "north": { "texture": "#pane" }
      }
    }
  ]
}
""",
    "template_glass_pane_noside_alt": """{
  "ambientocclusion": false,
  "textures": {
    "particle": "#pane"
  },
  "elements": [
    {
      "from": [7, 0, 7],
      "to": [9, 16, 9],
      "faces": {
        "east": { "texture": "#pane" }
      }
    }
  ]
}
""",
    "template_glass_pane_post": """{
  "ambientocclusion": false,
  "textures": {
    "particle": "#pane"
  },
  "elements": [
    {
      "from": [7, 0, 7],
      "to": [9, 16, 9],
      "faces": {
        "down": { "uv": [7, 7, 9, 9], "texture": "#edge" },
        "up": { "uv": [7, 7, 9, 9], "texture": "#edge" }
      }
    }
  ]
}
""",
    "template_glass_pane_side": """{
  "ambientocclusion": false,
  "textures": {
    "particle": "#pane"
  },
  "elements": [
    {
      "from": [7, 0, 0],
      "to": [9, 16, 7],
      "faces": {
        "down": { "uv": [7, 0, 9, 7], "texture": "#edge" },
        "up": { "uv": [7, 0, 9, 7], "texture": "#edge" },
        "north": { "texture": "#edge", "cullface": "north" },
        "west": { "texture": "#pane" },
        "east": { "texture": "#pane" }
      }
    }
  ]
}
""",
    "template_glass_pane_side_alt": """{
  "ambientocclusion": false,
  "textures": {
    "particle": "#pane"
  },
  "elements": [
    {
      "from": [7, 0, 9],
      "to": [9, 16, 16],
      "faces": {
        "down": { "uv": [7, 0, 9, 7], "texture": "#edge" },
        "up": { "uv": [7, 0, 9, 7], "texture": "#edge" },
        "south": { "texture": "#edge", "cullface": "south" },
        "west": { "texture": "#pane" },
        "east": { "texture": "#pane" }
      }
    }
  ]
}
""",
}

# nom du modèle de pane -> (template utilisé, direction pour les "side")
PANE_MODELS = {
    "post": ("template_glass_pane_post", None),
    "noside": ("template_glass_pane_noside", None),
    "noside_alt": ("template_glass_pane_noside_alt", None),
    "side_north": ("template_glass_pane_side", "north"),
    "side_east": ("template_glass_pane_side", "east"),
    "side_south": ("template_glass_pane_side_alt", "south"),
    "side_west": ("template_glass_pane_side_alt", "west"),
}

DIAGONALS = [
    "bottom_left", "bottom_right", "left", "right", "top_left", "top_right",
]

# --------------------------------------------------------------------------- #
# Utilitaires
# --------------------------------------------------------------------------- #


def dump_json(data, indent=2):
    """json.dumps, mais en gardant les petites listes ([\"true\"], [7, 0, 7]) sur une ligne."""
    text = json.dumps(data, indent=indent, ensure_ascii=False)

    def collapse(m):
        items = [s.strip() for s in m.group(1).split(",")]
        return "[" + ", ".join(items) + "]" if len(items) <= 2 else m.group(0)

    return re.sub(r"\[\s*([^\[\]{}]*?)\s*\]", collapse, text) + "\n"


class Writer:
    def __init__(self, root: Path, dry_run: bool):
        self.root = root
        self.dry_run = dry_run

    def write(self, path: Path, content: str, label: str = "créé"):
        rel = path.relative_to(self.root).as_posix()
        print(f"      {'(dry-run) ' if self.dry_run else ''}{label} : {rel}")
        if self.dry_run:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")


    def write_bytes(self, path: Path, content: bytes, label: str = "créé"):
        rel = path.relative_to(self.root).as_posix()
        print(f"      {'(dry-run) ' if self.dry_run else ''}{label} : {rel}")
        if self.dry_run:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def png_size(path: Path):
    """Lit largeur/hauteur dans l'en-tête PNG (sans dépendance)."""
    try:
        with open(path, "rb") as f:
            head = f.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", head[16:24])
    except OSError:
        pass
    return None


def check_pieced(png: Path):
    size = png_size(png)
    if not size:
        return
    w, h = size
    tile = w // 5
    if w % 5 != 0 or tile == 0 or h % tile != 0:
        print(f"      ⚠ {png.name} fait {w}x{h} : ça ne ressemble pas à un layout "
              f"« pieced » (5 tuiles de large). Pense à vérifier la texture.")


def normalize_render_type(value):
    if not value:
        return None
    value = value.split(":")[-1]
    return f"minecraft:{value}" if value in ("translucent", "cutout") else None


def ask_render_type(label, has_pane):
    suffix = "  [+ pane]" if has_pane else ""
    while True:
        try:
            ans = input(f"  ▶ {label}{suffix}\n    render_type ? "
                        f"[t]ranslucent / [c]utout / [s]kip / [q]uit : ").strip().lower()
        except EOFError:
            return "quit"
        if ans in ("t", "translucent"):
            return "minecraft:translucent"
        if ans in ("c", "cutout"):
            return "minecraft:cutout"
        if ans in ("s", "skip", ""):
            return None
        if ans in ("q", "quit"):
            return "quit"
        print("    Réponse non reconnue (t / c / s / q).")


# --------------------------------------------------------------------------- #
# Génération
# --------------------------------------------------------------------------- #


def write_mcmeta(w: Writer, png: Path):
    path = png.with_name(png.name + ".mcmeta")
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}
    data = {"fusion": {"type": "connecting", "layout": "pieced"}}
    # on conserve le reste (ex. "animation") tel quel
    data.update({k: v for k, v in existing.items() if k != "fusion"})
    if existing == data:
        return
    w.write(path, dump_json(data, indent=2), "mis à jour" if existing else "créé")


def write_cube_model(w: Writer, path: Path, ns: str, tex_id: str, render_type: str):
    data = {
        "type": "fusion:connecting",
        "parent": "minecraft:block/cube_all",
        "render_type": render_type,
        "loader": "fusion:model",
        "textures": {"all": tex_id},
    }
    w.write(path, dump_json(data))


def side_connections(pane_block: str, direction: str):
    return {
        "type": "fusion:or",
        "predicates": [{
            "type": "fusion:or",
            "predicates": [
                {
                    "type": "fusion:and",
                    "predicates": [
                        {
                            "type": "fusion:match_state",
                            "block": pane_block,
                            "properties": {direction: ["true"]},
                        },
                        {"type": "fusion:is_direction", "directions": ["bottom", "top"]},
                    ],
                },
                {
                    "type": "fusion:and",
                    "predicates": [
                        {"type": "fusion:is_same_block"},
                        {"type": "fusion:is_direction", "directions": DIAGONALS},
                    ],
                },
            ],
        }],
    }


def same_block_connections():
    return {"type": "fusion:or", "predicates": [{"type": "fusion:is_same_block"}]}


def write_pane(w: Writer, ns_dir: Path, ns: str, name: str,
               tex_id: str, edge_id: str, render_type: str):
    models = ns_dir / "models" / "block"
    pane_id = f"{ns}:{name}_pane"

    # 1) templates (communs, créés une seule fois par namespace)
    for tpl_name, content in TEMPLATES.items():
        tpl_path = models / "templates" / f"{tpl_name}.json"
        if not tpl_path.exists():
            w.write(tpl_path, content)

    # 2) les 7 modèles de pane
    for suffix, (tpl_name, direction) in PANE_MODELS.items():
        data = {
            "type": "fusion:connecting",
            "parent": f"{ns}:block/templates/{tpl_name}",
            "render_type": render_type,
            "connections": (side_connections(pane_id, direction)
                            if direction else same_block_connections()),
            "loader": "fusion:model",
            "textures": {"edge": edge_id, "pane": tex_id},
        }
        w.write(models / "panes" / f"{name}_pane_{suffix}.json", dump_json(data))

    # 3) blockstate multipart
    def m(suffix, y=None):
        apply = {"model": f"{ns}:block/panes/{name}_pane_{suffix}"}
        if y:
            apply["y"] = y
        return apply

    blockstate = {"multipart": [
        {"apply": m("post")},
        {"apply": m("side_north"), "when": {"north": "true"}},
        {"apply": m("side_east", 90), "when": {"east": "true"}},
        {"apply": m("side_south"), "when": {"south": "true"}},
        {"apply": m("side_west", 90), "when": {"west": "true"}},
        {"apply": m("noside"), "when": {"north": "false"}},
        {"apply": m("noside_alt"), "when": {"east": "false"}},
        {"apply": m("noside_alt", 90), "when": {"south": "false"}},
        {"apply": m("noside", 270), "when": {"west": "false"}},
    ]}
    w.write(ns_dir / "blockstates" / f"{name}_pane.json", dump_json(blockstate))

    # 4) pane_culling_fix (une liste de cibles par namespace)
    fix_path = ns_dir / "fusion" / "model_modifiers" / "blocks" / "pane_culling_fix.json"
    fix = {"pane_culling_fix": True, "targets": []}
    if fix_path.exists():
        try:
            fix.update(json.loads(fix_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    if pane_id not in fix["targets"]:
        fix["targets"].append(pane_id)
        w.write(fix_path, dump_json(fix), "mis à jour" if fix_path.exists() else "créé")


# --------------------------------------------------------------------------- #
# Mode --new : dossiers + images transparentes
# --------------------------------------------------------------------------- #


def transparent_png(width: int, height: int) -> bytes:
    """PNG RGBA entièrement transparent (sans dépendance)."""
    raw = (b"\x00" + b"\x00" * (width * 4)) * height

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def ask_text(prompt, pattern=r"[a-z0-9_.\-]+", allow_empty=False):
    while True:
        try:
            value = input(prompt).strip()
        except EOFError:
            return None
        if not value and allow_empty:
            return None
        if re.fullmatch(pattern, value):
            return value
        print("    Nom invalide (minuscules, chiffres, _ . - uniquement).")


def ask_yes_no(prompt, default=True):
    try:
        ans = input(f"{prompt} [{'O/n' if default else 'o/N'}] ").strip().lower()
    except EOFError:
        return False
    return default if not ans else ans in ("o", "oui", "y", "yes")


def init_assets(w: Writer, root: Path):
    """Demande le mod + les assets, crée les dossiers et les images transparentes.
    Retourne le nom du mod (ou None)."""
    print("=== Création des dossiers et des images transparentes ===")
    mod = ask_text("Nom du mod (nom du dossier dans assets/) : ")
    if not mod:
        return None
    tex_dir = root / "assets" / mod / "textures" / "block"

    created = 0
    while True:
        asset = ask_text("\nNom de l'asset (fichier, sans .png ; vide pour terminer) : ",
                         allow_empty=True)
        if not asset:
            break
        has_pane = ask_yes_no(f"  « {asset} » a-t-il une pane (image {asset}_edge.png) ?")

        png = tex_dir / f"{asset}.png"
        if png.exists():
            print(f"      déjà présent : {png.relative_to(root).as_posix()}")
        else:
            w.write_bytes(png, transparent_png(80, 16))          # layout pieced : 5 tuiles
            created += 1
        if has_pane:
            edge = tex_dir / f"{asset}_edge.png"
            if edge.exists():
                print(f"      déjà présent : {edge.relative_to(root).as_posix()}")
            else:
                w.write_bytes(edge, transparent_png(16, 16))
                created += 1

    if created == 0 and not tex_dir.exists():
        tex_dir.mkdir(parents=True, exist_ok=True) if not w.dry_run else None
    print(f"\n{created} image(s) transparente(s) créée(s) dans assets/{mod}/textures/block/")
    return mod


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="dossier contenant assets/ (défaut : dossier courant)")
    ap.add_argument("-n", "--namespace", action="append",
                    help="ne traiter que ce namespace (répétable)")
    ap.add_argument("--ignore-dir", action="append", default=["construction"],
                    help="nom de sous-dossier à ignorer (défaut : construction ; répétable)")
    ap.add_argument("--force", action="store_true",
                    help="régénère même si le modèle / la pane existe déjà")
    ap.add_argument("--dry-run", action="store_true", help="n'écrit aucun fichier")
    ap.add_argument("--new", action="store_true",
                    help="crée d'abord les dossiers d'un mod et des images transparentes "
                         "(demande le nom du mod puis ceux des assets)")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    assets = root / "assets"

    if args.new:
        mod = init_assets(Writer(root, args.dry_run), root)
        if not mod:
            return
        if not ask_yes_no("\nGénérer maintenant les modèles de ce mod ?"):
            return
        args.namespace = [mod]

    if not assets.is_dir():
        sys.exit(f"Dossier introuvable : {assets}\nLance le script depuis la racine du pack "
                 f"ou utilise --root.")

    w = Writer(root, args.dry_run)
    stats = {"traitées": 0, "ignorées": 0, "panes": 0}

    for tex_dir in sorted(assets.glob("*/textures/block")):
        ns_dir = tex_dir.parent.parent
        ns = ns_dir.name
        if args.namespace and ns not in args.namespace:
            continue

        pngs = sorted(tex_dir.rglob("*.png"))
        print(f"\n=== {ns} ({len(pngs)} image(s)) ===")

        for png in pngs:
            rel = png.relative_to(tex_dir)
            if any(part in args.ignore_dir for part in rel.parts[:-1]):
                continue

            stem = png.stem
            if stem.endswith("_edge"):
                if not png.with_name(stem[:-5] + ".png").exists():
                    print(f"  ⚠ {rel.as_posix()} : pas d'image « {stem[:-5]}.png » associée")
                continue

            rel_noext = rel.with_suffix("").as_posix()          # ex. construction/foo ou foo
            tex_id = f"{ns}:block/{rel_noext}"
            edge_png = png.with_name(stem + "_edge.png")
            has_edge = edge_png.exists()
            edge_id = f"{ns}:block/{rel.with_name(stem + '_edge').as_posix()}"

            model_path = ns_dir / "models" / "block" / f"{rel_noext}.json"
            pane_state = ns_dir / "blockstates" / f"{stem}_pane.json"

            need_cube = args.force or not model_path.exists()
            need_pane = has_edge and (args.force or not pane_state.exists())
            if not need_cube and not need_pane:
                print(f"  • {rel.as_posix()} : déjà généré (utilise --force pour refaire)")
                stats["ignorées"] += 1
                continue

            # render_type : on réutilise celui du modèle existant s'il y en a un
            render_type = None
            if not need_cube:
                try:
                    render_type = normalize_render_type(
                        json.loads(model_path.read_text(encoding="utf-8")).get("render_type"))
                except (json.JSONDecodeError, OSError):
                    pass
            if render_type is None:
                render_type = ask_render_type(f"{ns}:{rel_noext}", has_edge)
                if render_type == "quit":
                    print("\nArrêt demandé.")
                    return _summary(stats)
                if render_type is None:
                    print("      ignoré.")
                    stats["ignorées"] += 1
                    continue

            print(f"  • {rel.as_posix()}  →  {render_type.split(':')[1]}")
            check_pieced(png)
            write_mcmeta(w, png)
            if need_cube:
                write_cube_model(w, model_path, ns, tex_id, render_type)
            if need_pane:
                write_pane(w, ns_dir, ns, stem, tex_id, edge_id, render_type)
                stats["panes"] += 1
            stats["traitées"] += 1

    _summary(stats)


def _summary(stats):
    print(f"\nTerminé : {stats['traitées']} image(s) traitée(s), "
          f"{stats['panes']} pane(s), {stats['ignorées']} ignorée(s).")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrompu.")
