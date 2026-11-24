"""Lampada stampabile per Bambu Lab A1 mini con Bambu Lab LED Lamp Kit 001.

Genera:
  lampada_fondo_LED001.stl   fondo con sede del puck LED e canale per cavo USB / telecomando
  lampada_corpo.stl          paralume a fiore ritorto, si incastra sul collare del fondo
  lampada_assemblata.stl     i due pezzi montati (solo per controllo, non da stampare)
  lampada_anteprima.png      anteprima

Uso:
  pip install trimesh manifold3d numpy matplotlib
  python lampada_led001.py
"""
from __future__ import annotations

import math
import os

import numpy as np
import trimesh
from manifold3d import CrossSection, Manifold

# ----------------------------------------------------------------------------- parametri (mm)
PUCK_D = 59.5          # diametro del puck LED
PUCK_CLEAR = 0.8       # gioco sul diametro della sede -> 60.3
SEAT_DEPTH = 19.0      # profondità della sede (puck ~18 mm con la cupola)

BASE_D = 96.0          # fondo: diametro
BASE_H = 20.0          # fondo: altezza del cilindro
COLLAR_D = 82.0        # collare su cui calza il corpo
COLLAR_H = 8.0

CABLE_W = 24.0         # canale per USB + telecomando: larghezza
CABLE_H = 15.0         # canale: altezza (aperto sul fondo)

SHADE_H = 175.0        # corpo: altezza (deve restare sotto i 180 mm della A1 mini)
LOBES = 8              # petali del fiore
R0 = 46.0              # raggio medio esterno del fiore
AMP = 3.0              # ampiezza dei petali
WALL = 2.0             # spessore parete (fiore interno con r0 = R0 - WALL)
TWIST = 90.0           # torsione totale in gradi
TOP_SCALE = 0.82       # scala della sezione in cima
FIT = 0.35             # gioco radiale manicotto/collare -> foro Ø82.7
SLEEVE_H = 8.0         # manicotto pieno in basso

SEGMENTS = 256         # risoluzione dei cerchi
FLOWER_PTS = 480       # punti della sezione a fiore
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
PRINTER_MAX = 180.0


# ----------------------------------------------------------------------------- utilità

def cyl(d: float, h: float, z0: float = 0.0) -> Manifold:
    return Manifold.cylinder(h, d / 2, d / 2, SEGMENTS).translate((0, 0, z0))


def box(x0, x1, y0, y1, z0, z1) -> Manifold:
    return Manifold.cube((x1 - x0, y1 - y0, z1 - z0)).translate((x0, y0, z0))


def flower(r0: float) -> CrossSection:
    t = np.linspace(0, 2 * math.pi, FLOWER_PTS, endpoint=False)
    r = r0 + AMP * np.cos(LOBES * t)
    return CrossSection([np.column_stack((r * np.cos(t), r * np.sin(t)))])


def flower_extrude(r0: float, height: float) -> Manifold:
    """Estrusione con torsione e scala proporzionali all'altezza (coerenti con SHADE_H)."""
    k = height / SHADE_H
    return Manifold.extrude(flower(r0), height, n_divisions=int(height),
                            twist_degrees=TWIST * k, scale_top=(1 - (1 - TOP_SCALE) * k,) * 2)


def to_trimesh(m: Manifold) -> trimesh.Trimesh:
    mesh = m.to_mesh()
    return trimesh.Trimesh(vertices=np.asarray(mesh.vert_properties)[:, :3],
                           faces=np.asarray(mesh.tri_verts), process=True)


# ----------------------------------------------------------------------------- pezzi

def make_base() -> Manifold:
    body = cyl(BASE_D, BASE_H) + cyl(COLLAR_D, COLLAR_H, BASE_H)
    top = BASE_H + COLLAR_H
    seat = cyl(PUCK_D + PUCK_CLEAR, SEAT_DEPTH + 1, top - SEAT_DEPTH)
    # canale radiale dal centro al bordo, aperto sotto; passa sotto la sede del LED
    channel = box(0, BASE_D, -CABLE_W / 2, CABLE_W / 2, -1, CABLE_H)
    return body - seat - channel


def make_shade() -> Manifold:
    outer = flower_extrude(R0, SHADE_H)
    # il fiore interno sfora in cima di 2 mm per lasciare il corpo aperto senza facce coincidenti
    inner = flower_extrude(R0 - WALL, SHADE_H + 2) ^ box(-80, 80, -80, 80, SLEEVE_H, SHADE_H + 3)
    bore = cyl(COLLAR_D + 2 * FIT, SLEEVE_H + 1, -1)
    return outer - inner - bore


# ----------------------------------------------------------------------------- verifica e export

def report(name: str, tm: trimesh.Trimesh) -> None:
    ext = tm.bounds[1] - tm.bounds[0]
    print(f"{name:32s} watertight={tm.is_watertight}  volume={tm.is_volume}  "
          f"ingombro={ext[0]:.1f} x {ext[1]:.1f} x {ext[2]:.1f} mm  "
          f"volume={tm.volume / 1000:.1f} cm³")
    assert tm.is_watertight, f"{name} non è watertight"
    assert (ext <= PRINTER_MAX).all(), f"{name} non entra nel volume della A1 mini"


def preview(base: trimesh.Trimesh, shade: trimesh.Trimesh, path: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    light = np.array([0.4, -0.6, 0.7])
    light /= np.linalg.norm(light)

    def add(ax, tm, color):
        # triangoli piccoli: matplotlib ordina per baricentro, quelli grandi si disegnano male
        v, f = trimesh.remesh.subdivide_to_size(tm.vertices, tm.faces, max_edge=4.0)
        tm = trimesh.Trimesh(v, f, process=False)
        shade_ = np.clip(tm.face_normals @ light, 0, 1) * 0.65 + 0.35
        cols = np.outer(shade_, np.array(color))
        pc = Poly3DCollection(tm.triangles, facecolors=cols, edgecolors=cols, linewidths=0.4, antialiased=False)
        ax.add_collection3d(pc)

    fig = plt.figure(figsize=(15, 6), dpi=120)
    shade_up = shade.copy()
    shade_up.apply_translation((0, 0, BASE_H))
    views = [("Assemblata", [(base, (.25, .25, .28)), (shade_up, (.95, .80, .45))], 25),
             ("Fondo (da sopra)", [(base, (.35, .55, .85))], 55),
             ("Fondo (da sotto)", [(base, (.35, .55, .85))], -50)]
    for i, (title, parts, elev) in enumerate(views):
        ax = fig.add_subplot(1, 3, i + 1, projection="3d")
        for tm, c in parts:
            add(ax, tm, c)
        lo = np.min([p[0].bounds[0] for p in parts], axis=0)
        hi = np.max([p[0].bounds[1] for p in parts], axis=0)
        c = (lo + hi) / 2
        r = (hi - lo).max() / 2 * 0.72
        ax.set_xlim(c[0] - r, c[0] + r); ax.set_ylim(c[1] - r, c[1] + r); ax.set_zlim(c[2] - r, c[2] + r)
        ax.set_box_aspect((1, 1, 1))
        ax.view_init(elev=elev, azim=-60 if i else -50)
        ax.set_title(title)
        ax.set_axis_off()
    fig.subplots_adjust(left=0, right=1, bottom=0, top=0.93, wspace=0)
    fig.savefig(path)
    plt.close(fig)


def main() -> None:
    base_m, shade_m = make_base(), make_shade()
    base, shade = to_trimesh(base_m), to_trimesh(shade_m)

    assembled = trimesh.util.concatenate([base, shade.copy().apply_translation((0, 0, BASE_H))])

    report("lampada_fondo_LED001.stl", base)
    report("lampada_corpo.stl", shade)
    ext = assembled.bounds[1] - assembled.bounds[0]
    print(f"{'lampada_assemblata.stl':32s} ingombro={ext[0]:.1f} x {ext[1]:.1f} x {ext[2]:.1f} mm "
          f"(due corpi, solo per controllo)")

    base.export(os.path.join(OUT_DIR, "lampada_fondo_LED001.stl"))
    shade.export(os.path.join(OUT_DIR, "lampada_corpo.stl"))
    assembled.export(os.path.join(OUT_DIR, "lampada_assemblata.stl"))
    preview(base, shade, os.path.join(OUT_DIR, "lampada_anteprima.png"))
    print("File scritti in", OUT_DIR)


if __name__ == "__main__":
    main()
