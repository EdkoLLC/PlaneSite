"""Seams between flight lines that keep raised thin things (power lines, fences) in one piece.

Each spot of the orthomosaic is drawn from the most straight-down photo, so the seam between two
neighbouring flight lines runs halfway between them. Anything above the ground appears shifted
in opposite directions in the two lines' photos (away from each line), by height x distance /
flight height: a wire 8 m up, halfway between lines 42 m apart flown at 82 m, sits about 2 m
north in one line's photos and 2 m south in the other's. With the seam halfway, each side uses
the photos in which the wire falls on the other side of the seam, and the wire disappears or
comes out in pieces.

Here the photos are grouped into flight lines. For each pair of neighbouring lines, a strip
image of each is drawn along the corridor between them from the small thumbnails, and their
disagreement is measured on fine detail (thin bright and dark lines), smoothed along the flight
direction so long features like wires stand out from grass. A shortest-path search then picks
the boundary through the corridor: near the middle, smooth, and clear of disagreement by a
margin, so a wire ends up wholly on one side and comes from one line's photos. Rendering then
draws each spot from the photos of its own line (feathered across the boundary).

Only regular missions (parallel flight lines) are handled; anything else keeps the plain
most-straight-down rule.
"""
import math
import os

import numpy as np

BAND_M = 1.5          # keep the seam this far from places where the lines disagree
GAP_M = 7.0           # the two copies of a raised thing (one per flight line) can be this far apart
ALONG_M = 8.0         # smoothing along the flight direction (long features stand out from grass)
DU, DW = 0.5, 0.25    # path grid: along, across (m)
FEATHER_M = 1.0       # blend across the boundary


def flight_lines(cams, min_photos=4, max_turn_deg=20.0):
    """Group photos into straight flight lines. Returns (lines, d) or None when the photos don't
    form parallel lines: lines = [{"ids": [cam index], "w": offset across, "u0", "u1"}] sorted by
    w, d = unit flight direction (E, N)."""
    order = sorted(range(len(cams)), key=lambda i: cams[i].get("idx", i))
    P = np.array([cams[i]["C"][:2] for i in order], float)
    if len(P) < 2 * min_photos:
        return None
    steps = np.diff(P, axis=0)
    slen = np.hypot(steps[:, 0], steps[:, 1])
    med = float(np.median(slen[slen > 0])) if (slen > 0).any() else 0.0
    if med <= 0:
        return None
    runs, cur = [], [0]
    for k in range(len(steps)):
        ok = 0 < slen[k] < 3 * med
        if ok and len(cur) >= 2:
            a = steps[k - 1] / max(slen[k - 1], 1e-9)
            b = steps[k] / slen[k]
            ok = float(np.dot(a, b)) > math.cos(math.radians(max_turn_deg))
        if ok:
            cur.append(k + 1)
        else:
            runs.append(cur)
            cur = [k + 1]
    runs.append(cur)
    runs = [r for r in runs if len(r) >= min_photos]
    if len(runs) < 2:
        return None
    # the dominant direction (axis, so lines flown both ways count together)
    ang = []
    wts = []
    for r in runs:
        v = P[r[-1]] - P[r[0]]
        ang.append(2 * math.atan2(v[1], v[0]))
        wts.append(len(r))
    th = math.atan2(np.average(np.sin(ang), weights=wts), np.average(np.cos(ang), weights=wts)) / 2
    d = np.array([math.cos(th), math.sin(th)])
    n = np.array([-d[1], d[0]])
    keep = []
    for r in runs:
        v = P[r[-1]] - P[r[0]]
        if abs(np.dot(v / max(np.hypot(*v), 1e-9), d)) > math.cos(math.radians(15)):
            keep.append(r)
    if len(keep) < 2 or sum(len(r) for r in keep) < 0.6 * len(P):
        return None
    lines = []
    for r in keep:
        ids = [order[j] for j in r]
        Q = P[r]
        lines.append({"ids": ids, "w": float(np.median(Q @ n)), "u0": float((Q @ d).min()), "u1": float((Q @ d).max())})
    lines.sort(key=lambda L: L["w"])
    # lines flown twice (or split by a battery change) at the same offset: one line
    gaps = np.diff([L["w"] for L in lines])
    spacing = float(np.median(gaps[gaps > 1.0])) if (gaps > 1.0).any() else 0.0
    if spacing <= 0:
        return None
    merged = [lines[0]]
    for L in lines[1:]:
        m = merged[-1]
        if L["w"] - m["w"] < 0.3 * spacing:
            m["ids"] += L["ids"]
            m["u0"], m["u1"] = min(m["u0"], L["u0"]), max(m["u1"], L["u1"])
            m["w"] = (m["w"] + L["w"]) / 2
        else:
            merged.append(L)
    if len(merged) < 2:
        return None
    return merged, d


def _line_image(cams, line, d, n, U, Wc, dsm, thumbs, sample_dsm, project_pts):
    """Luminance of one flight line's photos at ground points (U along, Wc across, same shape),
    each point from the line's photo nearest along the flight direction (its nadir rule within
    the line). NaN where none of them sees it."""
    ids = line["ids"]
    cu = np.array([np.dot(cams[i]["C"][:2], d) for i in ids])
    o = np.argsort(cu)
    ids = [ids[k] for k in o]
    cu = cu[o]
    mids = (cu[1:] + cu[:-1]) / 2
    which = np.searchsorted(mids, U)
    E = U * d[0] + Wc * n[0]
    N = U * d[1] + Wc * n[1]
    Z = sample_dsm(dsm, E, N)
    out = np.full(U.shape, np.nan, np.float32)
    for k, ci in enumerate(ids):
        m = which == k
        if not m.any():
            continue
        t = thumbs.get(ci)
        if t is None:
            continue
        cam = cams[ci]
        u, v, z = project_pts(cam["R"], cam["C"], cam["k"], np.c_[E[m], N[m], Z[m]])
        sx, sy = t.shape[1] / cam["W"], t.shape[0] / cam["H"]
        x = (u + 0.5) * sx - 0.5
        y = (v + 0.5) * sy - 0.5
        ok = (z > 0) & (x >= 0) & (y >= 0) & (x <= t.shape[1] - 1) & (y <= t.shape[0] - 1)
        x0 = np.clip(np.floor(x).astype(int), 0, t.shape[1] - 2)
        y0 = np.clip(np.floor(y).astype(int), 0, t.shape[0] - 2)
        ax, ay = np.clip(x - x0, 0, 1), np.clip(y - y0, 0, 1)
        val = (t[y0, x0] * (1 - ax) * (1 - ay) + t[y0, x0 + 1] * ax * (1 - ay)
               + t[y0 + 1, x0] * (1 - ax) * ay + t[y0 + 1, x0 + 1] * ax * ay)
        res = np.full(m.sum(), np.nan, np.float32)
        res[ok] = val[ok]
        out[m] = res
    return out


def plan(cams, dsm, thumb_path, log=None):
    """Seam plan for the renderer, or None (plain rule). thumb_path(cam) gives a cam's thumbnail
    file. The plan: {"d", "n", "lines": [{"w", "u0", "u1"}], "line_of": cam -> line or -1,
    "bounds": [{"u0", "du", "w": array}] between line i and i+1}."""
    import cv2
    from .ortho import project_pts
    from .surface import sample_dsm
    fl = flight_lines(cams)
    if fl is None:
        if log:
            log("Seams: no regular flight lines found; seams follow the straight-down rule")
        return None
    lines, d = fl
    n = np.array([-d[1], d[0]])
    line_of = np.full(len(cams), -1, np.int32)
    for li, L in enumerate(lines):
        line_of[L["ids"]] = li
    agl = float(np.median([c["agl"] for c in cams]))
    thumbs = {}

    def thumb(ci):
        if ci not in thumbs:
            try:
                tp = thumb_path(cams[ci])
                im = cv2.imread(tp, cv2.IMREAD_GRAYSCALE) if tp and os.path.exists(tp) else None
                thumbs[ci] = None if im is None else im.astype(np.float32)
            except Exception:
                thumbs[ci] = None
        return thumbs[ci]

    # sampling step for the detail images: about the thumbnails' ground pixel
    t0 = None
    for ci in range(len(cams)):
        t0 = thumb(ci)
        if t0 is not None:
            break
    if t0 is None:
        return None
    thumbs.clear()
    px = agl / cams[0]["k"][0] * cams[0]["W"] / t0.shape[1]
    step = max(0.08, min(0.35, px))
    bounds = []
    moved = []
    for li in range(len(lines) - 1):
        A, B = lines[li], lines[li + 1]
        u0, u1 = max(A["u0"], B["u0"]) - 10, min(A["u1"], B["u1"]) + 10
        if u1 - u0 < 10 or B["w"] - A["w"] < 4:
            bounds.append(None)
            continue
        uu = np.arange(u0, u1, step)
        ww = np.arange(A["w"], B["w"], step)
        U, Wc = np.meshgrid(uu, ww)                     # rows: across, cols: along
        for ci in A["ids"] + B["ids"]:
            thumb(ci)
        ia = _line_image(cams, A, d, n, U, Wc, dsm, thumbs, sample_dsm, project_pts)
        ib = _line_image(cams, B, d, n, U, Wc, dsm, thumbs, sample_dsm, project_pts)
        both = np.isfinite(ia) & np.isfinite(ib)
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

        def detail(im):
            im = np.nan_to_num(im, nan=float(np.nanmedian(im)) if np.isfinite(im).any() else 0.0)
            return cv2.morphologyEx(im, cv2.MORPH_TOPHAT, k) - cv2.morphologyEx(im, cv2.MORPH_BLACKHAT, k)

        dd = np.abs(detail(ia) - detail(ib))
        dd[~both] = 0
        L = max(3, int(ALONG_M / step) | 1)
        dl = cv2.blur(dd, (L, 1))                        # along the flight direction
        med = float(np.median(dl[both])) if both.any() else 1.0
        dl = dl / max(med, 1e-6)
        excess = np.clip(dl - 1.15, 0, None)              # only what stands out from grass
        # a raised thing shows as two copies, one per line, a few metres apart across the flight
        # direction: the gap between them is where it would vanish, so close it
        g = max(3, int(GAP_M / step) | 1)
        excess = cv2.morphologyEx(excess, cv2.MORPH_CLOSE, np.ones((g, 1), np.uint8))
        r = max(1, int(BAND_M / step))
        excess = cv2.dilate(excess, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))
        # down to the path grid
        gu = max(1, int(round(DU / step)))
        gw = max(1, int(round(DW / step)))
        hh, wd = excess.shape[0] // gw * gw, excess.shape[1] // gu * gu
        cost = excess[:hh, :wd].reshape(hh // gw, gw, wd // gu, gu).max(axis=(1, 3))
        cov = both[:hh, :wd].reshape(hh // gw, gw, wd // gu, gu).all(axis=(1, 3))
        wv = ww[:hh:gw][:cost.shape[0]]
        uv = uu[:wd:gu][:cost.shape[1]]
        mid = (A["w"] + B["w"]) / 2
        half = (B["w"] - A["w"]) / 2
        data = 0.6 * ((wv - mid) / half) ** 2                  # prefer the middle
        C = cost * 1.0 + data[:, None]
        C[~cov] += 50.0                                        # only where both lines see the ground
        # shortest path along u: one w per column. Between columns the boundary may jump any
        # distance sideways, paying for every cell it cuts through on the way: when it has to get
        # to the other side of a wire (round trees, say) it crosses square to it, so the wire
        # gets one small jog instead of losing a stretch.
        nw, nu = C.shape
        acc = C[:, 0].copy()
        back = np.zeros((nw, nu), np.int32)
        idx = np.arange(nw)
        lo_i = np.minimum(idx[:, None], idx[None, :])          # [new, old]
        hi_i = np.maximum(idx[:, None], idx[None, :])
        move = 0.02 * np.abs(idx[:, None] - idx[None, :])
        for jj in range(1, nu):
            cs = np.concatenate([[0.0], np.cumsum(C[:, jj])])
            seg = cs[hi_i + 1] - cs[lo_i] - C[idx, jj][:, None]   # cells cut, apart from the new one
            T = acc[None, :] + move + seg
            k = np.argmin(T, axis=1)
            acc = T[idx, k] + C[:, jj]
            back[:, jj] = k
        path = np.zeros(nu, np.int64)
        path[-1] = int(np.argmin(acc))
        for jj in range(nu - 1, 0, -1):
            path[jj - 1] = back[path[jj], jj]
        bw = wv[np.clip(path, 0, nw - 1)]
        bounds.append({"u0": float(uv[0]), "du": float(uv[1] - uv[0]) if len(uv) > 1 else DU, "w": bw.astype(np.float32)})
        moved.append(float(np.abs(bw - mid).max()))
        thumbs.clear()
    if log:
        log("Seams: %d flight lines; boundaries moved up to %.1f m from halfway to keep raised detail whole"
            % (len(lines), max(moved) if moved else 0.0))
    return {"d": d.tolist(), "n": n.tolist(), "lines": [{"w": L["w"], "u0": L["u0"], "u1": L["u1"]} for L in lines],
            "line_of": line_of, "bounds": bounds, "feather": FEATHER_M}


def line_weight(plan_, E, N, ci_line):
    """For ground points E, N (1-D arrays) and an array of line numbers (one per candidate photo),
    the share (0-1) of each candidate's line at each point: 1 inside its line's area, fading across
    the boundary. Shape (len(ci_line), len(E)). Photos outside every line (turns, -1) get 0."""
    d = np.asarray(plan_["d"])
    n = np.asarray(plan_["n"])
    U = E * d[0] + N * d[1]
    W = E * n[0] + N * n[1]
    lines = plan_["lines"]
    nl = len(lines)
    # boundary between line i and i+1 at each point
    wl = np.array([L["w"] for L in lines])
    lo = np.full((nl, len(E)), -np.inf)
    hi = np.full((nl, len(E)), np.inf)
    for i, b in enumerate(plan_["bounds"]):
        if b is None:
            bw = np.full(len(E), (wl[i] + wl[i + 1]) / 2)
        else:
            x = (U - b["u0"]) / b["du"]
            j = np.clip(np.floor(x).astype(int), 0, len(b["w"]) - 2) if len(b["w"]) > 1 else np.zeros(len(E), int)
            t = np.clip(x - j, 0, 1)
            bw = b["w"][j] * (1 - t) + b["w"][np.minimum(j + 1, len(b["w"]) - 1)] * t
        hi[i] = bw
        lo[i + 1] = bw
    f = max(plan_.get("feather", 1.0), 1e-3)

    def ramp(x):
        x = np.clip(x / f + 0.5, 0, 1)
        return x * x * (3 - 2 * x)

    share = np.zeros((nl, len(E)))
    for i in range(nl):
        share[i] = ramp(W - lo[i]) * ramp(hi[i] - W)
    out = np.zeros((len(ci_line), len(E)))
    for k, li in enumerate(ci_line):
        if li >= 0:
            out[k] = share[li]
    return out
