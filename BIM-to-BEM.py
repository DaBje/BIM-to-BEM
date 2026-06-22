# BIM-to-BEM.py
#
# Note-worthy:
#   - Version is the first working proof of concept considering multiple standards

bl_info = {
    "name": "BIM to BEM",
    "author": "David Bjelland",
    "version": (2, 2, 0),
    # Feature: clicking an IFC Space in the 3D viewport highlights it in the room selector list
    "blender": (5, 1, 2),
    "location": "View3D > Sidebar (N) > BIM to BEM",
    "description": "Query net floor area, volume, OWR and WFR (opening-to-floor ratio per DIN 4108-2) of IFC spaces with orientation breakdown.",
    "category": "Object",
}

import math
import bpy
from bpy.types import PropertyGroup, UIList, Panel, Operator
from bpy.props import (
    StringProperty,
    FloatProperty,
    IntProperty,
    BoolProperty,
    CollectionProperty,
    EnumProperty,
)

# --------------------------------------------------------------------------- #
# Deferred imports
# --------------------------------------------------------------------------- #

def _get_ifc_tools():
    try:
        import bonsai.tool as tool
        import ifcopenshell.util.element as ue
        return tool, ue
    except Exception:
        return None, None


def _get_geom_tools():
    try:
        import numpy as np
        import ifcopenshell.util.placement as placement
        return np, placement
    except Exception:
        return None, None


# --------------------------------------------------------------------------- #
# Unit helpers
# --------------------------------------------------------------------------- #

def _get_unit_scale(model):
    """Return factor to convert the project length unit to metres.

    E.g. 0.001 for a project modelled in mm, 1.0 for metres.
    Reads IfcUnitAssignment; falls back to 1.0 when the unit cannot be
    determined so that m-based projects are never broken.
    """
    try:
        for assignment in model.by_type("IfcUnitAssignment"):
            for unit in (assignment.Units or []):
                if not hasattr(unit, "UnitType") or unit.UnitType != "LENGTHUNIT":
                    continue
                if unit.is_a("IfcSIUnit"):
                    prefix_map = {
                        "MILLI": 1e-3,
                        "CENTI": 1e-2,
                        "DECI":  1e-1,
                        None:    1.0,
                        "KILO":  1e3,
                    }
                    return prefix_map.get(getattr(unit, "Prefix", None), 1.0)
                if unit.is_a("IfcConversionBasedUnit"):
                    cf = getattr(unit, "ConversionFactor", None)
                    if cf is not None:
                        return float(getattr(cf, "ValueComponent", 1.0))
    except Exception:
        pass
    return 1.0


# --------------------------------------------------------------------------- #
# Quantity lookup (set-name agnostic)
# --------------------------------------------------------------------------- #

def _find_quantity(element, ue, names):
    qtos = ue.get_psets(element, qtos_only=True) or {}
    for name in names:
        for set_name, qset in qtos.items():
            value = qset.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value), f"{set_name}.{name}"
    return None, None


# --------------------------------------------------------------------------- #
# Element / boundary helpers
# --------------------------------------------------------------------------- #

def _is_external_wall(wall, ue):
    try:
        pset = ue.get_pset(wall, "Pset_WallCommon")
        if pset and "IsExternal" in pset:
            return bool(pset.get("IsExternal"))
    except Exception:
        pass
    return False


def _is_external_opening(el, ue):
    pset_name = "Pset_DoorCommon" if el.is_a("IfcDoor") else "Pset_WindowCommon"
    try:
        pset = ue.get_pset(el, pset_name)
        if pset and "IsExternal" in pset:
            return bool(pset.get("IsExternal"))
    except Exception:
        pass
    return None


def _boundary_is_external(rel):
    flag = getattr(rel, "InternalOrExternalBoundary", None) or ""
    return flag.upper().startswith("EXTERNAL")


def _opening_host_wall(el, model):
    opening = None
    fills = getattr(el, "FillsVoids", None)
    if fills:
        for rel_fill in fills:
            opening = getattr(rel_fill, "RelatingOpeningElement", None)
            if opening: break
    else:
        for rel_fill in model.by_type("IfcRelFillsElement"):
            if getattr(rel_fill, "RelatedBuildingElement", None) == el:
                opening = getattr(rel_fill, "RelatingOpeningElement", None)
                if opening: break
    if not opening:
        return None
    voids = getattr(opening, "VoidsElements", None)
    if voids:
        for rel_void in voids:
            host = getattr(rel_void, "RelatingBuildingElement", None)
            if host: return host
    else:
        for rel_void in model.by_type("IfcRelVoidsElement"):
            if getattr(rel_void, "RelatedOpeningElement", None) == opening:
                host = getattr(rel_void, "RelatingBuildingElement", None)
                if host: return host
    return None


def _wall_openings(wall):
    out = []
    for rel_void in (getattr(wall, "HasOpenings", None) or []):
        opening = getattr(rel_void, "RelatedOpeningElement", None)
        if opening is None:
            continue
        for rel_fill in (getattr(opening, "HasFillings", None) or []):
            filler = getattr(rel_fill, "RelatedBuildingElement", None)
            if filler is not None and (filler.is_a("IfcWindow") or filler.is_a("IfcDoor")):
                out.append(filler)
    return out


def _wall_quantity_area(wall, ue):
    area, src = _find_quantity(wall, ue, ["GrossSideArea", "NetSideArea"])
    if area is not None:
        return area, src
    length, _ = _find_quantity(wall, ue, ["Length"])
    height, _ = _find_quantity(wall, ue, ["Height"])
    if length and height:
        return length * height, "Length*Height"
    return None, None


def _wall_gross_area(wall, ue):
    area, _ = _find_quantity(wall, ue, ["GrossSideArea"])
    if area is not None:
        return float(area)
    length, _ = _find_quantity(wall, ue, ["Length"])
    height, _ = _find_quantity(wall, ue, ["Height"])
    if length and height:
        return float(length) * float(height)
    return None


def _opening_quantity_area(el, ue, scale=1.0):
    area, _ = _find_quantity(el, ue, ["Area"])
    if area is not None:
        return area  # IfcElementQuantity values are in SI (m²)
    w = getattr(el, "OverallWidth", None)
    h = getattr(el, "OverallHeight", None)
    if w and h:
        # OverallWidth / OverallHeight are raw IFC attributes stored in the
        # project length unit (e.g. mm), so apply scale² to get m².
        return float(w) * float(h) * (scale * scale)
    return None


def _get_space_usage_type(entity):
    for rel in (getattr(entity, "IsTypedBy", None) or []):
        stype = getattr(rel, "RelatingType", None)
        if stype is not None and stype.is_a("IfcSpaceType"):
            ln = getattr(stype, "LongName", None) or ""
            if ln:
                return str(ln)
            nm = getattr(stype, "Name", None) or ""
            if nm:
                return str(nm)
    return ""


# --------------------------------------------------------------------------- #
# OWR colormap (viridis, inverted: higher WFR -> darker / more purple)
# --------------------------------------------------------------------------- #

# 11 control points sampled from the viridis colormap (t = 0..1)
_VIRIDIS = [
    (0.267, 0.005, 0.329),   # t=0.0  dark purple  <- highest WFR (most critical)
    (0.282, 0.100, 0.422),   # t=0.1
    (0.263, 0.196, 0.492),   # t=0.2
    (0.220, 0.292, 0.532),   # t=0.3
    (0.177, 0.390, 0.553),   # t=0.4
    (0.139, 0.488, 0.551),   # t=0.5
    (0.128, 0.587, 0.526),   # t=0.6
    (0.219, 0.682, 0.471),   # t=0.7
    (0.399, 0.763, 0.367),   # t=0.8
    (0.620, 0.826, 0.226),   # t=0.9
    (0.993, 0.906, 0.144),   # t=1.0  bright yellow <- lowest non-zero WFR
]


def _viridis_color(t):
    """Linearly interpolate the viridis colormap. t ∈ [0, 1]."""
    t = max(0.0, min(1.0, t))
    n = len(_VIRIDIS) - 1
    fi = t * n
    lo, hi = int(fi), min(int(fi) + 1, n)
    f = fi - lo
    r = _VIRIDIS[lo][0] + f * (_VIRIDIS[hi][0] - _VIRIDIS[lo][0])
    g = _VIRIDIS[lo][1] + f * (_VIRIDIS[hi][1] - _VIRIDIS[lo][1])
    b = _VIRIDIS[lo][2] + f * (_VIRIDIS[hi][2] - _VIRIDIS[lo][2])
    return (r, g, b)


def _wfr_to_color(wfr_pct):
    """Map WFR percentage to RGB.
    0%  → neutral grey.
    >0% → viridis inverted in 5% steps (higher WFR = darker / more purple).
    Steps cap at 50%+."""
    if wfr_pct <= 0.0:
        return (0.55, 0.55, 0.55)
    step = min(int(wfr_pct / 5.0), 10)   # 1..10 for 5%..50%+
    t = 1.0 - step / 10.0                 # invert: step 10 → t=0 (dark)
    return _viridis_color(t)


# --------------------------------------------------------------------------- #
# Material-slot override helpers (non-destructive: only changes link level)
# --------------------------------------------------------------------------- #

_VIZ_SLOT_KEY = "bim_bem_viz_slot"
_bim_bem_backup = {}
_bim_bem_shading = {}
_bim_bem_hidden = set()   # names of objects hidden by zone visualization


def _apply_mat_override(obj, mat):
    if not obj.material_slots:
        obj.data.materials.append(None)
        obj.material_slots[0].link = 'OBJECT'
        obj.material_slots[0].material = mat
        obj[_VIZ_SLOT_KEY] = "added"
    else:
        obj[_VIZ_SLOT_KEY] = obj.material_slots[0].link
        obj.material_slots[0].link = 'OBJECT'
        obj.material_slots[0].material = mat


def _restore_mat_override(obj):
    marker = obj.get(_VIZ_SLOT_KEY)
    if marker is None:
        return
    if marker == "added":
        obj.material_slots[0].link = 'DATA'
    else:
        obj.material_slots[0].link = marker
    del obj[_VIZ_SLOT_KEY]


def _make_bim_material(name, rgb, alpha, opaque):
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    if opaque:
        nodes.clear()
        emit = nodes.new("ShaderNodeEmission")
        out  = nodes.new("ShaderNodeOutputMaterial")
        links.new(emit.outputs["Emission"], out.inputs["Surface"])
        emit.inputs["Color"].default_value    = (rgb[0], rgb[1], rgb[2], 1.0)
        emit.inputs["Strength"].default_value = 1.0
        mat.blend_method = 'OPAQUE'
    else:
        nodes.clear()
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
        out  = nodes.new("ShaderNodeOutputMaterial")
        links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
        bsdf.inputs["Base Color"].default_value = (rgb[0], rgb[1], rgb[2], 1.0)
        bsdf.inputs["Alpha"].default_value = alpha
        bsdf.inputs["Roughness"].default_value = 0.85
        mat.blend_method = 'BLEND'
    return mat


# --------------------------------------------------------------------------- #
# Geometry: boundary surface area + centroid + normal (space-local frame)
# --------------------------------------------------------------------------- #

def _boundary_plane(rel):
    cg = getattr(rel, "ConnectionGeometry", None)
    if cg is None or not cg.is_a("IfcConnectionSurfaceGeometry"):
        return None, None
    surf = getattr(cg, "SurfaceOnRelatingElement", None)
    if surf is None:
        return None, None
    if surf.is_a("IfcCurveBoundedPlane"):
        return surf.BasisSurface, surf.OuterBoundary
    if surf.is_a("IfcCurveBoundedSurface"):
        bnds = getattr(surf, "Boundaries", None) or []
        outer = None
        for b in bnds:
            if b.is_a("IfcOuterBoundaryCurve"):
                outer = b
                break
        if outer is None and bnds:
            outer = bnds[0]
        return surf.BasisSurface, outer
    return None, None


def _curve_points_2d(curve):
    pts = []
    if curve is None:
        return pts
    if curve.is_a("IfcPolyline"):
        for p in curve.Points:
            c = p.Coordinates
            pts.append((float(c[0]), float(c[1])))
    elif curve.is_a("IfcIndexedPolyCurve"):
        plist = getattr(curve, "Points", None)
        coords = getattr(plist, "CoordList", None) or []
        for c in coords:
            pts.append((float(c[0]), float(c[1])))
    elif curve.is_a("IfcCompositeCurve") or curve.is_a("IfcBoundaryCurve"):
        for seg in (getattr(curve, "Segments", None) or []):
            parent = getattr(seg, "ParentCurve", None)
            sub = _curve_points_2d(parent)
            if pts and sub and pts[-1] == sub[0]:
                sub = sub[1:]
            pts.extend(sub)
    return pts


def _polygon_area(pts):
    n = len(pts)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


def _polygon_centroid_2d(pts):
    if not pts:
        return (0.0, 0.0)
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _polygon_intersection_area_3d(wall_verts, win_verts, wall_normal, np):
    """Compute the area of (window polygon ∩ wall polygon) for two coplanar polygons.

    Both vertex arrays must be in the same 3D coordinate frame (space-local).
    Returns the intersection area in the same square units as the inputs (i.e.
    project-unit², so caller must still apply unit_scale²), or None on failure.

    Uses Sutherland-Hodgman clipping; works correctly for convex wall polygons
    (the typical case for rectangular wall boundary surfaces).
    Non-convex wall polygons are flagged in the returned tuple so the caller
    can surface a warning — results remain best-effort.
    Returns (area_or_None, non_convex_warning: bool).
    """
    non_convex = False
    try:
        n = np.array(wall_normal, dtype=float)
        n_len = float(np.linalg.norm(n))
        if n_len < 1e-9:
            return None, False
        n /= n_len

        # Build an orthonormal basis (u, v) in the wall plane.
        ref = np.array([0.0, 0.0, 1.0]) if abs(n[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
        u = np.cross(n, ref)
        u /= np.linalg.norm(u)
        v = np.cross(n, u)

        origin = np.array(wall_verts[0], dtype=float)

        def _to_2d(verts):
            result = []
            for p in verts:
                d = np.array(p, dtype=float) - origin
                result.append((float(np.dot(d, u)), float(np.dot(d, v))))
            return result

        wall_2d = _to_2d(wall_verts)
        win_2d  = _to_2d(win_verts)

        # Convexity check: all cross-products of consecutive edges must have the
        # same sign for a convex polygon.
        nw = len(wall_2d)
        if nw >= 3:
            signs = []
            for i in range(nw):
                ax, ay = wall_2d[(i+1)%nw][0]-wall_2d[i][0], wall_2d[(i+1)%nw][1]-wall_2d[i][1]
                bx, by = wall_2d[(i+2)%nw][0]-wall_2d[(i+1)%nw][0], wall_2d[(i+2)%nw][1]-wall_2d[(i+1)%nw][1]
                cross = ax*by - ay*bx
                if abs(cross) > 1e-10:
                    signs.append(cross > 0)
            if signs and not (all(signs) or not any(signs)):
                non_convex = True

        # Sutherland-Hodgman: clip subject (window) against each edge of clip (wall).
        def _inside(p, a, b):
            return (b[0]-a[0])*(p[1]-a[1]) - (b[1]-a[1])*(p[0]-a[0]) >= -1e-12

        def _edge_intersect(p1, p2, a, b):
            dx1, dy1 = p2[0]-p1[0], p2[1]-p1[1]
            dxa, dya = b[0]-a[0], b[1]-a[1]
            denom = dx1*dya - dy1*dxa
            if abs(denom) < 1e-14:
                return ((p1[0]+p2[0])*0.5, (p1[1]+p2[1])*0.5)
            t = ((a[0]-p1[0])*dya - (a[1]-p1[1])*dxa) / denom
            return (p1[0]+t*dx1, p1[1]+t*dy1)

        clipped = list(win_2d)
        for i in range(nw):
            if not clipped:
                return 0.0, non_convex
            a, b = wall_2d[i], wall_2d[(i+1) % nw]
            out_pts = []
            nc = len(clipped)
            for j in range(nc):
                c, d = clipped[j], clipped[(j+1) % nc]
                c_in, d_in = _inside(c, a, b), _inside(d, a, b)
                if c_in:
                    out_pts.append(c)
                if c_in != d_in:
                    out_pts.append(_edge_intersect(c, d, a, b))
            clipped = out_pts

        return (_polygon_area(clipped) if len(clipped) >= 3 else 0.0), non_convex
    except Exception:
        return None, False


def _boundary_geometry(rel, np, placement, scale=1.0):
    plane, outer = _boundary_plane(rel)
    if plane is None or outer is None:
        return None
    pts = _curve_points_2d(outer)
    if len(pts) < 3:
        return None
    area = _polygon_area(pts)
    if area <= 0.0:
        return None
    # _polygon_area result is in project length units squared (e.g. mm²);
    # multiply by scale² to convert to m².
    area = area * scale * scale
    cu, cv = _polygon_centroid_2d(pts)
    pos = getattr(plane, "Position", None)
    if pos is None:
        return None
    try:
        m = placement.get_axis2placement(pos)
    except Exception:
        return None
    centroid_local = m @ np.array([cu, cv, 0.0, 1.0])
    normal_local = np.array(m[:3, 2], dtype=float)
    verts_local = np.array(
        [m @ np.array([x, y, 0.0, 1.0]) for x, y in pts], dtype=float
    )[:, :3]
    return float(area), np.array(centroid_local[:3], dtype=float), normal_local, verts_local


# --------------------------------------------------------------------------- #
# Orientation: surface normal -> compass azimuth + cardinal
# --------------------------------------------------------------------------- #

CARDINALS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")

# Solar-gain weights per cardinal (northern hemisphere, 8-direction).
# S = most direct sun → 1.0; N = almost none → 0.2.
# Diagonal directions interpolated between their neighbours.
# Criticality = Σ(opening_area × weight) / total_floor_area.
ORIENT_WEIGHTS = {
    "N":  0.2, "NE": 0.4,
    "E":  0.6, "SE": 0.8,
    "S":  1.0, "SW": 0.8,
    "W":  0.6, "NW": 0.4,
}


def _true_north_offset_deg(model):
    try:
        for ctx in model.by_type("IfcGeometricRepresentationContext"):
            if ctx.is_a("IfcGeometricRepresentationSubContext"):
                continue
            tn = getattr(ctx, "TrueNorth", None)
            if tn is not None and tn.DirectionRatios:
                dr = tn.DirectionRatios
                return math.degrees(math.atan2(dr[0], dr[1]))
    except Exception:
        pass
    return 0.0


def _orientation(normal_local, centroid_local, center_local, space_rot, tn_offset, np):
    nlx, nly = float(normal_local[0]), float(normal_local[1])
    if math.hypot(nlx, nly) < 1e-6:
        return None, None
    out_x = float(centroid_local[0] - center_local[0])
    out_y = float(centroid_local[1] - center_local[1])
    n = np.array(normal_local, dtype=float)
    if (nlx * out_x + nly * out_y) < 0.0:
        n = -n
    g = space_rot @ n[:3]
    gx, gy = float(g[0]), float(g[1])
    if math.hypot(gx, gy) < 1e-9:
        return None, None
    proj_az = math.degrees(math.atan2(gx, gy))
    az = (proj_az - tn_offset) % 360.0
    idx = int((az + 22.5) // 45.0) % 8
    return az, CARDINALS[idx]


# --------------------------------------------------------------------------- #
# Main per-space query
# --------------------------------------------------------------------------- #

def query_space(space, model, ue):
    out = {
        "net_floor_area": None,
        "net_volume": None,
        "ext_wall_count": 0,
        "total_wall_area": 0.0,
        "total_opening_area": 0.0,
        "window_area": 0.0,
        "door_area": 0.0,
        "wwr": None,
        "wall_area_source": "",
        "openings": [],
        "orient": {c: {"wall": 0.0, "opening": 0.0, "wwr": None} for c in CARDINALS},
        "notes": [],
    }

    out["net_floor_area"], _ = _find_quantity(space, ue, ["NetFloorArea"])
    out["net_volume"], _ = _find_quantity(space, ue, ["NetVolume"])
    if out["net_floor_area"] is None:
        out["notes"].append("No NetFloorArea quantity found on this space.")
    if out["net_volume"] is None:
        out["notes"].append("No NetVolume quantity found on this space.")

    unit_scale = _get_unit_scale(model)

    boundaries = [r for r in (getattr(space, "BoundedBy", None) or [])
                  if r.is_a("IfcRelSpaceBoundary")]
    if not boundaries:
        out["notes"].append(
            "Space has no IfcRelSpaceBoundary; cannot determine bounding walls. "
            "Generate 2nd-level space boundaries to enable the ratio."
        )
        return out

    np, placement = _get_geom_tools()
    space_rot = None
    tn_offset = 0.0
    if np is not None and getattr(space, "ObjectPlacement", None) is not None:
        try:
            space_rot = placement.get_local_placement(space.ObjectPlacement)[:3, :3]
            tn_offset = _true_north_offset_deg(model)
        except Exception:
            space_rot = None

    geom = {}
    all_centroids = []
    wall_bounds = []
    opening_bounds = {}
    for rel in boundaries:
        el = getattr(rel, "RelatedBuildingElement", None)
        if el is None:
            continue
        if np is not None:
            try:
                g = _boundary_geometry(rel, np, placement, unit_scale)
            except Exception:
                g = None
            if g is not None:
                geom[rel.id()] = g
                all_centroids.append(g[1])
        if el.is_a("IfcWall"):
            if _boundary_is_external(rel) or _is_external_wall(el, ue):
                wall_bounds.append((el, rel))
        elif el.is_a("IfcWindow") or el.is_a("IfcDoor"):
            gid = el.GlobalId
            if gid not in opening_bounds or _boundary_is_external(rel):
                opening_bounds[gid] = (el, rel)

    center_local = np.mean(np.array(all_centroids), axis=0) if all_centroids else None
    have_geom = space_rot is not None and center_local is not None

    def orient_of(rel):
        g = geom.get(rel.id()) if rel is not None else None
        if g is None or not have_geom:
            return None, None
        return _orientation(g[2], g[1], center_local, space_rot, tn_offset, np)

    ext_wall_ids = {el.GlobalId for el, _ in wall_bounds}

    wall_gid_count = {}
    for _el, _rel in wall_bounds:
        if (getattr(_rel, "InternalOrExternalBoundary", None) or "").upper() == "INTERNAL":
            continue
        wall_gid_count[_el.GlobalId] = wall_gid_count.get(_el.GlobalId, 0) + 1

    wall_area_geom = 0.0
    walls_with_geom = 0
    for el, rel in wall_bounds:
        g = geom.get(rel.id())
        if g is None:
            continue
        # Skip boundaries explicitly marked INTERNAL.  Some IFC exporters
        # create both an EXTERNAL and an INTERNAL boundary for the same
        # exterior wall in the same space; counting both would double the
        # wall area.  The EXTERNAL boundary already covers the surface.
        if (getattr(rel, "InternalOrExternalBoundary", None) or "").upper() == "INTERNAL":
            continue
        walls_with_geom += 1
        bnd = g[0]
        area = bnd
        if wall_gid_count.get(el.GlobalId, 0) == 1:
            gross = _wall_gross_area(el, ue)
            if (gross is not None and bnd > 0
                    and gross >= bnd
                    and (gross - bnd) / bnd < 0.20):
                area = gross
        wall_area_geom += area
        _, card = orient_of(rel)
        if card:
            out["orient"][card]["wall"] += area

    if walls_with_geom > 0:
        out["total_wall_area"] = wall_area_geom
        out["wall_area_source"] = "boundary geometry (space-facing surface)"
    elif wall_bounds:
        seen = set()
        total = 0.0
        for el, _ in wall_bounds:
            if el.GlobalId in seen:
                continue
            seen.add(el.GlobalId)
            a, _src = _wall_quantity_area(el, ue)
            if a:
                total += a
        out["total_wall_area"] = total
        out["wall_area_source"] = "element quantity (whole wall - may exceed space)"
        out["notes"].append(
            "No boundary connection geometry; wall area is the whole wall and "
            "may exceed the part bounding this space."
        )

    def counts(el, rel):
        ext = _is_external_opening(el, ue)
        if ext is True:
            return True
        if ext is False:
            return False
        host_wall = _opening_host_wall(el, model)
        return _boundary_is_external(rel) or (
            host_wall is not None
            and getattr(host_wall, "GlobalId", None) in ext_wall_ids
        )

    jobs = []
    used_fallback = False
    if opening_bounds:
        for gid, (el, rel) in opening_bounds.items():
            if counts(el, rel):
                jobs.append((el, rel, rel))
        if not jobs:
            out["notes"].append(
                f"{len(opening_bounds)} opening boundary(ies) found but all excluded "
                "(none classified as external). Falling back to wall-traversal."
            )

    # Count distinct spaces per opening across the whole model.
    # Used in the fallback below (restrict_to_space check) and later for the
    # spanning-window area correction.  Must be computed before the fallback.
    _opening_n_spaces: dict = {}
    try:
        for _bnd in model.by_type("IfcRelSpaceBoundary"):
            _bel = getattr(_bnd, "RelatedBuildingElement", None)
            _bsp = getattr(_bnd, "RelatingSpace", None)
            if (_bel is not None and _bsp is not None
                    and (_bel.is_a("IfcWindow") or _bel.is_a("IfcDoor"))):
                _sid = getattr(_bsp, "GlobalId", str(id(_bsp)))
                _opening_n_spaces.setdefault(_bel.GlobalId, set()).add(_sid)
    except Exception:
        pass
    _opening_n_spaces = {gid: len(s) for gid, s in _opening_n_spaces.items()}

    if not jobs and wall_bounds:
        used_fallback = True
        for wall_el, rel in wall_bounds:
            # Only traverse walls that face the exterior directly from this
            # space (InternalOrExternalBoundary starts with "EXTERNAL").
            # Walls classified as INTERNAL bound an adjacent interior zone
            # from this space's side — their windows/doors belong to that zone.
            if not _boundary_is_external(rel):
                continue
            for op in _wall_openings(wall_el):
                if _is_external_opening(op, ue) is False:
                    continue
                # Skip openings already evaluated by the primary path.
                # If counts() accepted them, jobs would not be empty and we
                # would not be here.  If counts() rejected them (e.g. an
                # interior door whose host wall happens to be on an exterior
                # face of this space), respect that decision.
                if op.GlobalId in opening_bounds:
                    continue
                jobs.append((op, None, rel))

    # Second-tier fallback: wall-traversal found nothing AND opening_bounds
    # has openings that counts() rejected due to incomplete data (no IsExternal
    # pset, missing void relationship).  Include only those whose IFC space
    # boundary is not explicitly INTERNAL — an INTERNAL boundary means the
    # opening faces an adjacent interior zone, not the exterior.
    if not jobs and opening_bounds:
        for gid, (el, rel) in opening_bounds.items():
            ext = _is_external_opening(el, ue)
            if ext is True:
                jobs.append((el, rel, rel))
            elif ext is None:
                flag = (getattr(rel, "InternalOrExternalBoundary", None) or "").upper()
                if not flag.startswith("INTERNAL"):
                    jobs.append((el, rel, rel))

    # Build wall-boundary-geometry lookup for spanning-window clipping below.
    _wall_geom_lookup: dict = {}
    for _we, _wr in wall_bounds:
        _wg = geom.get(_wr.id())
        if _wg is not None:
            _wall_geom_lookup[_we.GlobalId] = _wg

    total_opening_area = 0.0
    openings_without_area = 0
    seen_op = set()
    for el, geom_rel, orient_rel in jobs:
        if el.GlobalId in seen_op:
            continue
        seen_op.add(el.GlobalId)
        kind = "Door" if el.is_a("IfcDoor") else "Window"

        area = None
        if geom_rel is not None and geom.get(geom_rel.id()) is not None:
            area = geom[geom_rel.id()][0]
        elem_area = _opening_quantity_area(el, ue, unit_scale)
        if area is None:
            area = elem_area

        # Spanning-window correction ----------------------------------------
        # Trigger: boundary geometry area ≈ element area (ratio > 0.99),
        # meaning the IFC boundary polygon was not clipped to this space.
        if (area is not None and elem_area is not None
                and elem_area > 0 and area / elem_area > 0.99):
            corrected = None

            # Primary: clip window polygon against host-wall boundary polygon.
            win_g = geom.get(geom_rel.id()) if geom_rel is not None else None
            if (win_g is not None and win_g[3] is not None
                    and len(win_g[3]) >= 3 and np is not None):
                host_wall = _opening_host_wall(el, model)
                if host_wall is not None:
                    wall_g = _wall_geom_lookup.get(host_wall.GlobalId)
                    if (wall_g is not None and wall_g[3] is not None
                            and len(wall_g[3]) >= 3):
                        raw, _ncx = _polygon_intersection_area_3d(
                            wall_g[3], win_g[3], wall_g[2], np)
                        if _ncx:
                            out.setdefault("non_convex_walls", set()).add(
                                host_wall.Name or host_wall.GlobalId)
                        if raw is not None and raw > 0:
                            clipped_m2 = raw * unit_scale * unit_scale
                            if clipped_m2 < area * 0.99:
                                corrected = clipped_m2

            # Fallback: equal share across all spaces this opening bounds.
            if corrected is None:
                n_sp = _opening_n_spaces.get(el.GlobalId, 1)
                if n_sp > 1:
                    corrected = elem_area / n_sp

            if corrected is not None:
                area = corrected
        # -------------------------------------------------------------------
        az, card = orient_of(orient_rel)
        if area is None:
            openings_without_area += 1
        else:
            if card:
                out["orient"][card]["opening"] += area
            # Decide whether to count this opening in the WFR totals.
            # Rules (in priority order):
            #  1. Orientation known → only count if this space has a wall in
            #     that direction (opening facing a direction with no wall is on
            #     an adjacent space's wall).
            #  2. Orientation unknown + geometry globally unavailable → count
            #     (numpy/placement absent; can't determine anything better).
            #  3. Orientation unknown + from wall traversal (geom_rel is None)
            #     + geometry available → the wall has no boundary geometry so
            #     we cannot confirm it faces this space; skip to avoid counting
            #     openings on perpendicular or projecting walls.
            #  4. Orientation unknown + from primary/second-tier path → count
            #     conservatively (e.g. skylights with no horizontal component).
            from_wall_traversal = (geom_rel is None)
            if card is not None:
                count_it = out["orient"][card]["wall"] > 0.0
            elif not have_geom:
                count_it = True
            elif from_wall_traversal:
                count_it = False
            else:
                count_it = True
            if count_it:
                total_opening_area += area
                if kind == "Door":
                    out["door_area"] += area
                else:
                    out["window_area"] += area
        out["openings"].append({
            "name": el.Name or f"({kind.lower()})",
            "kind": kind,
            "area": area if area is not None else 0.0,
            "has_area": area is not None,
            "azimuth": az,
            "cardinal": card,
        })

    out["total_opening_area"] = total_opening_area
    out["ext_wall_count"] = len(ext_wall_ids)

    if out["total_wall_area"] > 0.0:
        out["wwr"] = total_opening_area / out["total_wall_area"]
    elif wall_bounds:
        out["notes"].append("Exterior walls found but wall area is 0; ratio unavailable.")

    for c in CARDINALS:
        wa = out["orient"][c]["wall"]
        out["orient"][c]["wwr"] = (out["orient"][c]["opening"] / wa) if wa > 0.0 else None

    if openings_without_area:
        out["notes"].append(f"{openings_without_area} opening(s) had no usable area.")
    if used_fallback:
        out["notes"].append(
            "No opening-level space boundaries; openings taken from wall openings "
            "and may include ones facing other spaces."
        )
    if not have_geom:
        out["notes"].append("Orientation unavailable (no numpy/placement or no geometry).")
    elif abs(tn_offset) > 1e-6:
        out["notes"].append(f"Azimuth corrected for True North ({tn_offset:.1f} deg).")

    return out


# --------------------------------------------------------------------------- #
# DIN 4108-2 S_zul calculation
# --------------------------------------------------------------------------- #

# S1 table: (nutzung, nachtlueftung, bauart) -> (A, B, C)
_S1 = {
    'wohn': {
        'ohne':   {'leicht': (0.071, 0.056, 0.041),
                   'mittel': (0.080, 0.067, 0.054),
                   'schwer': (0.087, 0.074, 0.061)},
        'erhoht': {'leicht': (0.098, 0.088, 0.078),
                   'mittel': (0.114, 0.103, 0.092),
                   'schwer': (0.125, 0.113, 0.101)},
        'hoch':   {'leicht': (0.128, 0.117, 0.105),
                   'mittel': (0.160, 0.152, 0.143),
                   'schwer': (0.181, 0.171, 0.160)},
    },
    'nichtwohn': {
        'ohne':   {'leicht': (0.013, 0.007, 0.000),
                   'mittel': (0.020, 0.013, 0.006),
                   'schwer': (0.025, 0.018, 0.011)},
        'erhoht': {'leicht': (0.071, 0.060, 0.048),
                   'mittel': (0.089, 0.081, 0.072),
                   'schwer': (0.101, 0.092, 0.083)},
        'hoch':   {'leicht': (0.090, 0.082, 0.074),
                   'mittel': (0.135, 0.124, 0.113),
                   'schwer': (0.170, 0.158, 0.145)},
    },
}
_KLIMA_IDX = {'A': 0, 'B': 1, 'C': 2}

# S2 parameters: nutzung -> (a, b)  where S2 = a - b * f_WG
_S2_PARAMS = {'wohn': (0.060, 0.231), 'nichtwohn': (0.030, 0.115)}

# S6: bauart -> value when passive cooling is active
_S6_VALS = {'leicht': 0.02, 'mittel': 0.04, 'schwer': 0.06}

# F_C table (DIN 4108-2 Tab. 7): sonnenschutz_type -> (g≤0.40, g>0.40 dreifach, g>0.40 zweifach)
_FC_TABLE = {
    'none':  (1.00, 1.00, 1.00),  # ohne Sonnenschutzvorrichtung
    '2_1':   (0.65, 0.70, 0.65),  # innen: weiß / hoch reflektierend, geringe Transparenz
    '2_2':   (0.75, 0.80, 0.75),  # innen: helle Farben / geringe Transparenz
    '2_3':   (0.90, 0.90, 0.85),  # innen: dunkle Farben / höhere Transparenz
    '3_1_1': (0.40, 0.35, 0.35),  # außen: Fensterläden / Rollläden ¾ geschlossen
    '3_1_2': (0.15, 0.10, 0.10),  # außen: Fensterläden / Rollläden geschlossen
    '3_2_1': (0.30, 0.25, 0.25),  # außen: Jalousie/Raffstore 45° Lamellenstellung
    '3_2_2': (0.20, 0.15, 0.15),  # außen: Jalousie/Raffstore 10° Lamellenstellung
    '3_3':   (0.30, 0.25, 0.25),  # außen: Markise, parallel zur Verglasung
    '3_4':   (0.55, 0.50, 0.50),  # außen: Vordächer / Markisen allgemein
}


def _get_fc(scene):
    """Look up F_C from the DIN 4108-2 table given current project settings."""
    vals = _FC_TABLE.get(scene.bim_project_sonnenschutz, (1.0, 1.0, 1.0))
    if scene.bim_project_g_value <= 0.40:
        return vals[0]
    return vals[1] if scene.bim_project_verglasung == 'dreifach' else vals[2]


def _s_vorh(item, scene):
    """S_vorh = WFR × g × F_C  (simplified uniform g and F_C across all openings)."""
    if not item.has_wfr or item.net_floor_area <= 0:
        return None
    return item.wfr * scene.bim_project_g_value * _get_fc(scene)


def _din_s1(scene):
    try:
        idx = _KLIMA_IDX[scene.bim_project_klimaregion]
        return _S1[scene.bim_project_nutzung][scene.bim_project_nachtlueftung][scene.bim_project_bauart][idx]
    except (KeyError, IndexError):
        return 0.0


def _din_s2(f_wg, nutzung):
    a, b = _S2_PARAMS.get(nutzung, (0.060, 0.231))
    return a - b * f_wg


def _din_s3(g_value):
    return 0.03 if g_value <= 0.40 else 0.0


def _din_s4(f_neig):
    return -0.035 * f_neig


def _din_s5(f_nord):
    return 0.10 * f_nord


def _din_s6(scene):
    return _S6_VALS.get(scene.bim_project_bauart, 0.0) if scene.bim_project_passive_kuehlung else 0.0


def _din_szul_components(scene, f_wg, f_neig=0.0, f_nord=0.0):
    """Return (s1, s2, s3, s4, s5, s6, s_zul).
    f_neig and f_nord are per-space values computed from IFC data;
    f_neig defaults to 0.0 (vertical windows assumed)."""
    s1 = _din_s1(scene)
    s2 = _din_s2(f_wg, scene.bim_project_nutzung)
    s3 = _din_s3(scene.bim_project_g_value)
    s4 = _din_s4(f_neig)
    s5 = _din_s5(f_nord)
    s6 = _din_s6(scene)
    return s1, s2, s3, s4, s5, s6, s1 + s2 + s3 + s4 + s5 + s6


def _space_f_nord(item):
    """Fraction of total opening area facing north (DIN 4108-2 f_nord proxy).
    Uses the N, NE and NW cardinal bins (8-direction scheme)."""
    if item.total_opening_area <= 0.0:
        return 0.0
    north_area = sum(
        o.opening_area for o in item.orient if o.cardinal in ('N', 'NE', 'NW')
    )
    return north_area / item.total_opening_area


def _space_szul(item, scene):
    """Compute S_zul for a single space given current project settings."""
    if not item.has_wfr:
        return None
    _, _, _, _, _, _, szul = _din_szul_components(
        scene, item.wfr, f_neig=0.0, f_nord=_space_f_nord(item)
    )
    return szul


def _is_space_heated(item, scene):
    """Return True if the space's group is marked as heated (default True)."""
    key = _item_group_key(item, scene)
    for f in _active_filters(scene):
        if f.name == key:
            return f.heated
    return True


# --------------------------------------------------------------------------- #
# Data model (stored on the Scene)
# --------------------------------------------------------------------------- #

class BIMQueryOpening(PropertyGroup):
    name: StringProperty()
    kind: StringProperty(default="Window")
    has_area: BoolProperty(default=False)
    area: FloatProperty(default=0.0)
    has_orientation: BoolProperty(default=False)
    azimuth: FloatProperty(default=0.0)
    cardinal: StringProperty(default="")


class BIMQueryOrient(PropertyGroup):
    cardinal: StringProperty()
    wall_area: FloatProperty(default=0.0)
    opening_area: FloatProperty(default=0.0)
    has_wwr: BoolProperty(default=False)
    wwr: FloatProperty(default=0.0)


class BIMQueryUsageFilter(PropertyGroup):
    """One entry per distinct IfcSpaceType usage label."""
    name: StringProperty()
    enabled: BoolProperty(default=True)
    heated: BoolProperty(default=True)  # type: ignore[assignment]


class BIMQuerySpaceItem(PropertyGroup):
    name: StringProperty(name="Name")
    long_name: StringProperty(name="Long Name")
    global_id: StringProperty(name="GlobalId")
    usage_type: StringProperty(name="Usage Type", default="")

    has_result: BoolProperty(default=False)
    has_floor_area: BoolProperty(default=False)
    net_floor_area: FloatProperty(default=0.0)
    has_volume: BoolProperty(default=False)
    net_volume: FloatProperty(default=0.0)

    ext_wall_count: IntProperty(default=0)
    total_wall_area: FloatProperty(default=0.0)
    total_opening_area: FloatProperty(default=0.0)
    window_area: FloatProperty(default=0.0)
    door_area: FloatProperty(default=0.0)
    has_wwr: BoolProperty(default=False)
    wwr: FloatProperty(default=0.0)
    wall_area_source: StringProperty(default="")

    # WFR = window area / net floor area ("Grundflächenbezogener Fensterflächenanteil")
    has_wfr: BoolProperty(default=False)
    wfr: FloatProperty(default=0.0)

    openings: CollectionProperty(type=BIMQueryOpening)
    orient: CollectionProperty(type=BIMQueryOrient)
    notes: StringProperty(default="")


def _store_result(item, res):
    item.has_floor_area = res["net_floor_area"] is not None
    item.net_floor_area = float(res["net_floor_area"]) if item.has_floor_area else 0.0
    item.has_volume = res["net_volume"] is not None
    item.net_volume = float(res["net_volume"]) if item.has_volume else 0.0
    item.ext_wall_count = res["ext_wall_count"]
    item.total_wall_area = float(res["total_wall_area"])
    item.total_opening_area = float(res["total_opening_area"])
    item.window_area = float(res["window_area"])
    item.door_area = float(res["door_area"])
    item.has_wwr = res["wwr"] is not None
    item.wwr = float(res["wwr"]) if item.has_wwr else 0.0
    item.wall_area_source = res["wall_area_source"]

    # WFR = total external opening area (windows + doors) / net floor area.
    # DIN 4108-2 §8 counts all Verglasungen (incl. Fenstertüren) as solar-gain
    # elements; all external doors are included conservatively since IFC cannot
    # reliably distinguish glazed from opaque doors.
    item.has_wfr = item.has_floor_area and item.net_floor_area > 0.0
    item.wfr = item.total_opening_area / item.net_floor_area if item.has_wfr else 0.0

    item.openings.clear()
    for w in res["openings"]:
        oi = item.openings.add()
        oi.name = w["name"]
        oi.kind = w["kind"]
        oi.has_area = w["has_area"]
        oi.area = float(w["area"])
        oi.has_orientation = w["cardinal"] is not None
        oi.azimuth = float(w["azimuth"]) if w["azimuth"] is not None else 0.0
        oi.cardinal = w["cardinal"] or ""
    item.orient.clear()
    for c in CARDINALS:
        o = res["orient"][c]
        oi = item.orient.add()
        oi.cardinal = c
        oi.wall_area = float(o["wall"])
        oi.opening_area = float(o["opening"])
        oi.has_wwr = o["wwr"] is not None
        oi.wwr = float(o["wwr"]) if o["wwr"] is not None else 0.0
    item.notes = " | ".join(res["notes"])
    item.has_result = True


def _weights_dict(scene):
    return {
        'N': scene.bim_query_weight_n,
        'E': scene.bim_query_weight_e,
        'S': scene.bim_query_weight_s,
        'W': scene.bim_query_weight_w,
    }


def _ranking_score(item, scene):
    """Score where higher = more critical, for sorting and colorization.

    wfr         — plain WFR (m² opening / m² floor).
    wfr_orient  — WFR weighted by cardinal solar-exposure (S=1.0, E/W=0.6, N=0.2).
    szul        — DIN 4108-2 S_zul inverted: lower S_zul → higher score (more constrained).
    szul_orient — orientation-weighted WFR divided by S_zul: highest solar risk vs. allowance.
    """
    if not item.has_wfr:
        return 0.0
    metric = scene.bim_query_ranking_metric

    def _orient_weighted():
        if item.net_floor_area <= 0:
            return item.wfr
        w = _weights_dict(scene)
        weighted = sum(
            o.opening_area * w.get(o.cardinal, 0.5)
            for o in item.orient if o.opening_area > 0
        )
        return (weighted / item.net_floor_area) if weighted > 0 else item.wfr

    if metric == 'wfr':
        return item.wfr
    if metric == 'wfr_orient':
        return _orient_weighted()
    if metric == 'szul':
        szul = _space_szul(item, scene)
        if szul is None:
            return 0.0
        # Lower S_zul = tighter constraint = more critical.
        # Invert against reference 0.30 so the score is always ≥ 0.
        return max(0.0, 0.30 - szul)
    if metric == 'szul_orient':
        szul = _space_szul(item, scene)
        if szul is None:
            return 0.0
        # orientation-weighted WFR / S_zul: high solar exposure + tight allowance = worst case.
        # Cap S_zul at 0.01 to avoid division by zero for very highly glazed rooms.
        return _orient_weighted() / max(0.01, szul)
    return 0.0


def _dominant_orientation(item, scene):
    """Cardinal with the highest weighted opening area (orientation-based metrics only)."""
    if scene.bim_query_ranking_metric not in ('wfr_orient', 'szul_orient'):
        return ""
    w = _weights_dict(scene)
    best_card, best_val = "", 0.0
    for o in item.orient:
        val = o.opening_area * w.get(o.cardinal, 0.5)
        if val > best_val:
            best_val, best_card = val, o.cardinal
    return best_card


def _active_filters(scene):
    """Return the filter collection for the active grouping mode."""
    if getattr(scene, "bim_query_group_by", "usage") == "longname":
        return scene.bim_query_longname_filters
    return scene.bim_query_usage_filters


def _item_group_key(item, scene):
    """Return the grouping key for a space item under the active mode."""
    if getattr(scene, "bim_query_group_by", "usage") == "longname":
        return item.long_name
    return item.usage_type


def _sync_usage_filters(scene):
    """Rebuild the active-mode filter list to match current spaces exactly."""
    items = scene.bim_query_spaces
    filters = _active_filters(scene)
    present = sorted({_item_group_key(it, scene) for it in items})
    to_remove = [i for i, f in enumerate(filters) if f.name not in set(present)]
    for i in reversed(to_remove):
        filters.remove(i)
    existing_names = {f.name for f in filters}
    for t in present:
        if t not in existing_names:
            f = filters.add()
            f.name = t
            f.enabled = True


def _on_group_by_change(self, context):
    _sync_usage_filters(context.scene)


# --------------------------------------------------------------------------- #
# Operators
# --------------------------------------------------------------------------- #

class BIM_OT_enable_spatial_decomposition(Operator):
    bl_idname = "bim_query.enable_spatial_decomposition"
    bl_label = "Make Spaces Available"
    bl_description = (
        "Toggle IFC space visibility in the viewport. "
        "Click once to show and unlock spaces; click again to hide them."
    )
    bl_options = {"REGISTER"}

    def execute(self, context):
        def _find_lc(lc, name):
            if lc.name == name:
                return lc
            for child in lc.children:
                r = _find_lc(child, name)
                if r:
                    return r
            return None

        scene = context.scene
        currently_available = scene.bim_spaces_available
        lc = _find_lc(context.view_layer.layer_collection, "IfcSpace")

        tool, _ = _get_ifc_tools()
        model = tool.Ifc.get() if tool else None

        if currently_available:
            # Hide the IfcSpace layer collection and every space object directly.
            if lc is not None:
                lc.hide_viewport = True
            if model:
                for entity in model.by_type("IfcSpace"):
                    try:
                        obj = tool.Ifc.get_object(entity)
                        if obj:
                            obj.hide_viewport = True
                    except Exception:
                        pass
            scene.bim_spaces_available = False
            self.report({"INFO"}, "Spaces are now hidden.")
        else:
            # Un-hide the IfcSpace layer collection and every space object
            # directly — without touching BIMSpatialDecompositionProperties,
            # which affects the whole spatial hierarchy and makes the site
            # visible as an unavoidable side effect.
            if lc is not None:
                lc.hide_viewport = False
                lc.exclude = False
            if model:
                for entity in model.by_type("IfcSpace"):
                    try:
                        obj = tool.Ifc.get_object(entity)
                        if obj:
                            obj.hide_viewport = False
                    except Exception:
                        pass
            scene.bim_spaces_available = True
            self.report({"INFO"}, "Spaces are now visible and selectable.")

        return {"FINISHED"}


class BIM_OT_query_add_selected(Operator):
    bl_idname = "bim_query.add_selected"
    bl_label = "Add Selected Spaces"
    bl_description = "Add selected IfcSpace object(s) to the query list and analyze them"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        tool, ue = _get_ifc_tools()
        if tool is None:
            self.report({"ERROR"}, "Bonsai / IfcOpenShell not available.")
            return {"CANCELLED"}
        model = tool.Ifc.get()
        if model is None:
            self.report({"ERROR"}, "No IFC project loaded in Bonsai.")
            return {"CANCELLED"}
        items = context.scene.bim_query_spaces
        existing = {it.global_id for it in items}
        added = 0
        skipped_non_space = 0
        for obj in context.selected_objects:
            entity = tool.Ifc.get_entity(obj)
            if entity is None:
                continue
            if not entity.is_a("IfcSpace"):
                skipped_non_space += 1
                continue
            if entity.GlobalId in existing:
                continue
            item = items.add()
            item.name = entity.Name or "(unnamed)"
            item.long_name = getattr(entity, "LongName", "") or ""
            item.global_id = entity.GlobalId
            item.usage_type = _get_space_usage_type(entity)
            existing.add(entity.GlobalId)
            _store_result(item, query_space(entity, model, ue))
            added += 1
        if added:
            _sync_usage_filters(context.scene)
        context.scene.bim_query_space_index = max(0, len(items) - 1)
        if added == 0:
            msg = "No new IfcSpace selected."
            if skipped_non_space:
                msg += f" ({skipped_non_space} selected object(s) were not spaces.)"
            self.report({"WARNING"}, msg)
            return {"CANCELLED"}
        self.report({"INFO"}, f"Added and analyzed {added} space(s).")
        return {"FINISHED"}


class BIM_OT_query_refresh(Operator):
    bl_idname = "bim_query.refresh"
    bl_label = "Re-analyze"
    bl_description = "Recompute metrics for every space in the list (after model edits)"
    bl_options = {"REGISTER"}

    def execute(self, context):
        tool, ue = _get_ifc_tools()
        if tool is None or tool.Ifc.get() is None:
            self.report({"ERROR"}, "No IFC project loaded in Bonsai.")
            return {"CANCELLED"}
        model = tool.Ifc.get()
        refreshed = 0
        for item in context.scene.bim_query_spaces:
            try:
                entity = model.by_guid(item.global_id)
            except Exception:
                entity = None
            if entity is None:
                item.notes = "Space no longer found in model."
                item.has_result = False
                continue
            item.usage_type = _get_space_usage_type(entity)
            _store_result(item, query_space(entity, model, ue))
            refreshed += 1
        _sync_usage_filters(context.scene)
        self.report({"INFO"}, f"Re-analyzed {refreshed} space(s).")
        return {"FINISHED"}


class BIM_OT_query_remove(Operator):
    bl_idname = "bim_query.remove"
    bl_label = "Remove Single Entry"
    bl_description = "Remove the active space from the list"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        items = context.scene.bim_query_spaces
        idx = context.scene.bim_query_space_index
        if 0 <= idx < len(items):
            items.remove(idx)
            context.scene.bim_query_space_index = min(idx, len(items) - 1)
        _sync_usage_filters(context.scene)
        return {"FINISHED"}


class BIM_OT_query_clear(Operator):
    bl_idname = "bim_query.clear"
    bl_label = "Clear All"
    bl_description = "Remove all spaces from the list"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        context.scene.bim_query_spaces.clear()
        context.scene.bim_query_space_index = 0
        context.scene.bim_query_usage_filters.clear()
        return {"FINISHED"}


class BIM_OT_query_toggle_usage(Operator):
    bl_idname = "bim_query.toggle_usage"
    bl_label = "Toggle Usage Group"
    bl_description = "Show or hide all spaces with this usage type"
    bl_options = {"REGISTER", "UNDO"}

    usage_name: StringProperty()

    def execute(self, context):
        for f in _active_filters(context.scene):
            if f.name == self.usage_name:
                f.enabled = not f.enabled
                return {"FINISHED"}
        return {"CANCELLED"}


class BIM_OT_toggle_heated(Operator):
    bl_idname = "bim_query.toggle_heated"
    bl_label = "Toggle Heated"
    bl_description = "Mark all spaces in this group as heated or unheated"
    bl_options = {"REGISTER", "UNDO"}

    group_name: StringProperty()

    def execute(self, context):
        for f in _active_filters(context.scene):
            if f.name == self.group_name:
                f.heated = not f.heated
                return {"FINISHED"}
        return {"CANCELLED"}


class BIM_OT_transform_spaces(Operator):
    bl_idname = "bim_query.transform_spaces"
    bl_label = "Transform into Zones and Visualize"
    bl_description = "Group spaces into IFC zones per the selected standard and colorize the viewport"
    bl_options = {"REGISTER"}

    def execute(self, context):
        status, msg = _create_and_visualize_zones(context)
        self.report({"INFO" if status == "FINISHED" else "WARNING"}, msg)
        return {status}


class BIM_OT_delete_zones(Operator):
    bl_idname  = "bim_query.delete_zones"
    bl_label   = "Delete BEM Zones"
    bl_description = ("Remove all BEM Zone boundary objects from the viewport "
                      "and delete the corresponding IfcZone entities, then save the IFC file")
    bl_options = {"REGISTER"}

    def execute(self, context):
        global _bim_bem_hidden, _bim_bem_backup
        tool, _ = _get_ifc_tools()
        if tool is None or tool.Ifc.get() is None:
            self.report({"WARNING"}, "No IFC project loaded.")
            return {"CANCELLED"}
        model = tool.Ifc.get()

        # Remove Blender zone boundary objects.
        _remove_bem_zone_objects()

        # Remove IfcZone entities from the in-memory model.
        _remove_existing_bem_zones(model)

        # Restore any hidden IFC space objects.
        for obj in context.scene.objects:
            if obj.name in _bim_bem_hidden:
                obj.hide_viewport = False
        _bim_bem_hidden = set()
        # Keep viz_active True so the "Reset Colors" button stays visible if a
        # prior colorization (e.g. heating condition) was active before zones
        # were created.  The user can click Reset Colors to fully restore the
        # original colors; setting viz_active=False here would hide that button
        # and leave colored spaces with no way to reset.

        # Save the updated IFC file.
        try:
            ifc_path = getattr(bpy.context.scene, "BIMProperties", None)
            ifc_path = ifc_path.ifc_file if ifc_path else None
            if ifc_path:
                model.write(ifc_path)
                self.report({"INFO"}, "BEM Zones deleted and IFC file saved.")
            else:
                self.report({"INFO"}, "BEM Zones deleted (IFC not saved — no file path).")
        except Exception as exc:
            self.report({"WARNING"}, f"BEM Zones deleted but IFC save failed: {exc}")
        return {"FINISHED"}


class BIM_OT_export_zones(Operator):
    bl_idname  = "bim_query.export_zones"
    bl_label   = "Export Zones (JSON)"
    bl_description = ("Export BEM zone summary — name, thermal condition, storey, "
                      "member spaces, and total floor area — as a JSON file")
    bl_options = {"REGISTER"}

    filepath: StringProperty(subtype="FILE_PATH")  # type: ignore[assignment]
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})  # type: ignore[assignment]

    def invoke(self, context, event):
        self.filepath = "bem_zones.json"
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        import json as _json
        tool, ue = _get_ifc_tools()
        if tool is None or tool.Ifc.get() is None:
            self.report({"WARNING"}, "No IFC project loaded.")
            return {"CANCELLED"}
        model = tool.Ifc.get()
        scene  = context.scene

        zone_groups = _build_zone_groups(scene, model)
        if not zone_groups:
            self.report({"WARNING"}, "No zones to export — run 'Transform into Zones' first.")
            return {"CANCELLED"}

        export_data = {"zones": []}
        for (heated, storey, comp_idx), items in sorted(zone_groups.items(), key=lambda x: (x[0][1], x[0][0], x[0][2])):
            cond = "beheizt" if heated else "unbeheizt"
            spaces = []
            total_area = 0.0
            for item in items:
                space_entry = {
                    "name":       item.name,
                    "long_name":  item.long_name,
                    "global_id":  item.global_id,
                    "usage_type": item.usage_type,
                }
                if item.has_result and item.net_floor_area > 0.0:
                    space_entry["net_floor_area_m2"] = round(item.net_floor_area, 4)
                    total_area += item.net_floor_area
                spaces.append(space_entry)
            comp_suffix = f" ({comp_idx + 1})" if comp_idx > 0 else ""
            zone_entry = {
                "name":               f"{cond} {storey}{comp_suffix}".strip(),
                "thermal_condition":  cond,
                "storey":             storey,
                "component":          comp_idx,
                "heated":             heated,
                "space_count":        len(items),
                "total_floor_area_m2": round(total_area, 4),
                "spaces":             spaces,
            }
            export_data["zones"].append(zone_entry)

        try:
            fp = self.filepath if self.filepath.endswith(".json") else self.filepath + ".json"
            with open(fp, "w", encoding="utf-8") as f:
                _json.dump(export_data, f, indent=2, ensure_ascii=False)
            self.report({"INFO"}, f"Exported {len(export_data['zones'])} zone(s) to {fp}")
        except Exception as exc:
            self.report({"ERROR"}, f"Export failed: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


def _apply_colorize(context, compliance_mode):
    """Shared colorize logic. Returns FINISHED or CANCELLED string."""
    global _bim_bem_backup, _bim_bem_shading, _bim_bem_hidden
    tool, ue = _get_ifc_tools()
    if tool is None or tool.Ifc.get() is None:
        return "CANCELLED", "No IFC project loaded."
    model = tool.Ifc.get()
    scene = context.scene
    scene.bim_query_colorize_mode = 'compliance' if compliance_mode else 'ranking'

    for obj in scene.objects:
        if obj.name in _bim_bem_hidden:
            obj.hide_viewport = False
    _bim_bem_hidden = set()

    filters_coll = _active_filters(scene)
    enabled_types = {f.name for f in filters_coll if f.enabled}
    has_filters = len(filters_coll) > 0

    colored_objs = {}
    for item in scene.bim_query_spaces:
        if not item.has_result:
            continue
        try:
            entity = model.by_guid(item.global_id)
            obj = tool.Ifc.get_object(entity)
        except Exception:
            obj = None
        if obj is None:
            continue
        if not ((not has_filters) or (_item_group_key(item, scene) in enabled_types)):
            continue
        if compliance_mode:
            sv = _s_vorh(item, scene)
            sz = _space_szul(item, scene)
            if sv is not None and sz is not None:
                colored_objs[obj] = _VIRIDIS[0] if sv > sz else _VIRIDIS[-1]
        else:
            colored_objs[obj] = _wfr_to_color(_ranking_score(item, scene) * 100.0)

    if not colored_objs:
        return "CANCELLED", "No space objects found in the 3D viewport."

    if not scene.bim_query_viz_active:
        _bim_bem_backup = {o.name: tuple(o.color) for o in scene.objects if o.type == 'MESH'}

    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            shd = area.spaces[0].shading
            if not scene.bim_query_viz_active:
                _bim_bem_shading = {
                    'type':       shd.type,
                    'color_type': getattr(shd, 'color_type', 'MATERIAL'),
                }
            shd.type       = 'SOLID'
            shd.color_type = 'OBJECT'
            break

    space_set = set(colored_objs.keys())
    for obj, rgb in colored_objs.items():
        obj.color = (rgb[0], rgb[1], rgb[2], 1.0)
    for obj in scene.objects:
        if obj.type == 'MESH' and obj not in space_set:
            obj.color = (0.7, 0.7, 0.7, 0.1)

    scene.bim_query_viz_active = True
    return "FINISHED", f"Colored {len(colored_objs)} space(s)."


# Distinct colors for the heating-condition colorize mode.
_COLOR_HEATED   = (0.90, 0.42, 0.05)   # warm orange
_COLOR_UNHEATED = (0.15, 0.50, 0.85)   # cool blue

# Per-zone color palettes: warm tones for heated zones, cool for unheated.
# Each index gives one zone a distinct hue within its category.
_WARM_ZONE_COLORS = [
    (0.90, 0.42, 0.05),  # orange
    (0.85, 0.20, 0.15),  # red-orange
    (0.95, 0.70, 0.10),  # amber
    (0.80, 0.55, 0.15),  # golden
    (0.70, 0.30, 0.05),  # brown-orange
    (0.95, 0.45, 0.35),  # salmon
    (0.75, 0.15, 0.30),  # crimson
    (0.90, 0.60, 0.20),  # light orange
]
_COOL_ZONE_COLORS = [
    (0.15, 0.50, 0.85),  # blue
    (0.10, 0.65, 0.75),  # teal
    (0.20, 0.35, 0.80),  # dark blue
    (0.05, 0.55, 0.55),  # dark teal
    (0.30, 0.60, 0.90),  # light blue
    (0.15, 0.45, 0.65),  # steel blue
    (0.25, 0.75, 0.85),  # cyan
    (0.10, 0.30, 0.70),  # navy
]


def _zone_color(idx, heated):
    palette = _WARM_ZONE_COLORS if heated else _COOL_ZONE_COLORS
    return palette[idx % len(palette)]


def _build_zone_groups(scene, model=None):
    """Return dict: (heated_bool, storey_name, component_idx) -> [BIMQuerySpaceItem].

    Spaces with the same thermal condition on the same storey are first
    collected together, then split into spatially-connected components via
    shared IfcWall boundaries.  Each disconnected component becomes its own
    zone entry (component_idx = 0, 1, 2, …), preventing geographically
    separate corridor wings etc. from being merged into one zone object.
    Filter checkboxes still control which spaces are included.
    """
    filters_coll = _active_filters(scene)
    enabled_types = {f.name for f in filters_coll if f.enabled}
    has_filters = len(filters_coll) > 0

    # Step 1: bin by (heated, storey) — same as before
    raw_groups = {}
    for item in scene.bim_query_spaces:
        if not item.has_result:
            continue
        gk = _item_group_key(item, scene)
        if has_filters and gk not in enabled_types:
            continue
        storey = ""
        if model is not None:
            try:
                storey = _get_storey(model.by_guid(item.global_id))
            except Exception:
                pass
        key = (_is_space_heated(item, scene), storey)
        raw_groups.setdefault(key, []).append(item)

    # Step 2: split each bin into connected components via shared wall boundaries
    groups = {}
    for (heated, storey), items in raw_groups.items():
        if model is None or len(items) <= 1:
            # No model to query adjacency, or trivially one space → single component
            groups[(heated, storey, 0)] = list(items)
            continue

        # Build wall-based adjacency graph among items in this bin
        gid_to_item = {item.global_id: item for item in items}
        adj = {gid: set() for gid in gid_to_item}
        wall_to_gids = {}
        for item in items:
            try:
                entity = model.by_guid(item.global_id)
            except Exception:
                continue
            for rel in (getattr(entity, "BoundedBy", None) or []):
                if not rel.is_a("IfcRelSpaceBoundary"):
                    continue
                el = getattr(rel, "RelatedBuildingElement", None)
                if el is not None and el.is_a("IfcWall"):
                    wall_to_gids.setdefault(el.GlobalId, []).append(item.global_id)
        for sgids in wall_to_gids.values():
            in_bin = [g for g in sgids if g in gid_to_item]
            for i in range(len(in_bin)):
                for j in range(i + 1, len(in_bin)):
                    adj[in_bin[i]].add(in_bin[j])
                    adj[in_bin[j]].add(in_bin[i])

        # BFS to extract connected components
        visited = set()
        comp_idx = 0
        for start_gid in gid_to_item:
            if start_gid in visited:
                continue
            component = []
            queue = [start_gid]
            while queue:
                cur = queue.pop()
                if cur in visited:
                    continue
                visited.add(cur)
                component.append(gid_to_item[cur])
                queue.extend(adj[cur] - visited)
            groups[(heated, storey, comp_idx)] = component
            comp_idx += 1

    return groups


_BEM_ZONE_TAG = "BIM_to_BEM_auto_zone"
_BEM_ZONE_COLL = "BEM Zones"


# --------------------------------------------------------------------------- #
# DIN V 18599-1 zone geometry helpers
# --------------------------------------------------------------------------- #

def _get_wall_thickness(wall, ue, unit_scale=1.0, wall_obj=None):
    """Return wall thickness in metres, or None if not found.

    Lookup order:
    1. IFC quantity sets (Qto_WallBaseQuantities Width / Thickness).
    2. Pset_WallCommon.Thickness.
    3. Blender bounding-box fallback: smallest dimension of *wall_obj*
       (typical for walls modelled as flat extrusions where the thin edge
       is the thickness, 5 cm – 1 m sanity range).
    """
    qtos = ue.get_psets(wall, qtos_only=True) or {}
    for qset in qtos.values():
        for name in ("Width", "Thickness"):
            val = qset.get(name)
            if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
                return float(val) * unit_scale
    try:
        pset = ue.get_pset(wall, "Pset_WallCommon") or {}
        val = pset.get("Thickness")
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            return float(val) * unit_scale
    except Exception:
        pass
    # Fallback: smallest Blender object dimension (in Blender world metres).
    if wall_obj is not None and wall_obj.type == 'MESH':
        try:
            dims = sorted(d for d in wall_obj.dimensions if d > 0.001)
            if dims and 0.05 <= dims[0] <= 1.0:
                return float(dims[0])
        except Exception:
            pass
    return None


def _get_storey(entity):
    """Return the Name of the IfcBuildingStorey containing this space, or ''.

    Checks both relationship types used in IFC files:
    - IfcRelContainedInSpatialStructure (ContainedInStructure) — IFC4 elements
    - IfcRelAggregates (Decomposes) — how most IFC2x3/4 files nest spaces
    """
    try:
        for rel in (getattr(entity, "ContainedInStructure", None) or []):
            container = getattr(rel, "RelatingStructure", None)
            if container is not None and container.is_a("IfcBuildingStorey"):
                return getattr(container, "Name", None) or ""
        for rel in (getattr(entity, "Decomposes", None) or []):
            container = getattr(rel, "RelatingObject", None)
            if container is not None and container.is_a("IfcBuildingStorey"):
                return getattr(container, "Name", None) or ""
    except Exception:
        pass
    return ""


def _build_zone_adjacency(model, zone_groups):
    """Return dict: wall GlobalId -> set of zone_keys (heated, group_name) that border it."""
    wall_to_zones = {}
    for key, items in zone_groups.items():
        for item in items:
            try:
                entity = model.by_guid(item.global_id)
            except Exception:
                continue
            for rel in (getattr(entity, "BoundedBy", None) or []):
                if not rel.is_a("IfcRelSpaceBoundary"):
                    continue
                el = getattr(rel, "RelatedBuildingElement", None)
                if el is not None and el.is_a("IfcWall"):
                    wall_to_zones.setdefault(el.GlobalId, set()).add(key)
    return wall_to_zones


def _outward_normal(normal_local, centroid, space_center, np_mod):
    """Return normal pointing away from the space interior."""
    n = np_mod.array(normal_local, dtype=float)
    nlen = float(np_mod.linalg.norm(n))
    if nlen < 1e-10:
        return n
    n = n / nlen
    if space_center is not None:
        to_face = np_mod.array(centroid, dtype=float) - np_mod.array(space_center, dtype=float)
        if float(np_mod.dot(n, to_face)) < 0.0:
            n = -n
    return n


def _check_zone_connectivity(model, zone_groups):
    """Superseded: connectivity is now resolved in _build_zone_groups.

    Each zone_group entry is already a single connected component, so this
    function always returns an empty list and is kept only for API compatibility.
    """
    return []


def _remove_bem_zone_objects():
    """Delete Blender objects and collection previously created by this tool."""
    import bpy  # type: ignore[import]
    coll = bpy.data.collections.get(_BEM_ZONE_COLL)
    if coll is None:
        return
    for obj in list(coll.objects):
        mesh = obj.data if obj.type == 'MESH' else None
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    bpy.data.collections.remove(coll)


def _add_wall_faces_to_bm(bm, wall_obj, bm_mod):
    """Copy all faces of *wall_obj* (world coordinates) into *bm*.

    Fills the wall-thickness gap that would otherwise appear between the
    inner-face boundary surfaces of two adjacent IFC spaces whose shared
    internal wall has been dissolved from the zone boundary surface set.
    Bonsai stores Blender mesh vertices in metres, matching the world-space
    coordinates produced by the boundary-surface pipeline.
    """
    mx  = wall_obj.matrix_world
    tmp = bm_mod.new()
    tmp.from_mesh(wall_obj.data)
    for face in tmp.faces:
        if len(face.verts) < 3:
            continue
        try:
            wv = [bm.verts.new((mx @ v.co)[:]) for v in face.verts]
            bm.faces.new(wv)
        except Exception:
            pass
    tmp.free()


def _add_slab_to_bm(bm, inner_w, outer_w):
    """Add a closed slab (inner face + midplane cap + side quads) to *bm*.

    Used to extend a zone boundary from the IFC space's inner wall face out to
    the wall midplane, implementing the DIN V 18599-1 convention for
    heated / unheated zone interfaces (Stage 2).
    """
    nv = len(inner_w)
    if nv < 3:
        return
    try:
        bm.faces.new([bm.verts.new(v) for v in inner_w])
    except Exception:
        pass
    try:
        bm.faces.new([bm.verts.new(v) for v in reversed(outer_w)])
    except Exception:
        pass
    for i in range(nv):
        j = (i + 1) % nv
        try:
            bm.faces.new([bm.verts.new(inner_w[i]),
                          bm.verts.new(inner_w[j]),
                          bm.verts.new(outer_w[j]),
                          bm.verts.new(outer_w[i])])
        except Exception:
            pass


def _compute_wall_offset(normal, centroid, space_ctr, world_xf, offset_m, np_mod):
    """Return an (dx, dy, dz) offset tuple in world metres for wall-slab extrusion.

    Ensures the normal points *outward* from the space interior before
    rotating it from space-local to world frame.  Returns None when the
    normal vector is degenerate.

    Used by:
      Stage EXT – full wall thickness T   for exterior walls.
      Stage 2   – half wall thickness T/2 for cross-zone internal walls.
    """
    n = np_mod.array(normal[:3], dtype=float)
    nlen = float(np_mod.linalg.norm(n))
    if nlen < 1e-9:
        return None
    n /= nlen
    # Flip if the normal points toward the space interior rather than away.
    if space_ctr is not None:
        to_face = np_mod.array(centroid[:3]) - space_ctr[:3]
        if float(np_mod.dot(n, to_face)) < 0.0:
            n = -n
    # Rotate from space-local frame to world frame.
    if world_xf is not None:
        rot_n = world_xf[:3, :3] @ n
        rlen = float(np_mod.linalg.norm(rot_n))
        if rlen > 1e-9:
            n = rot_n / rlen
    return (float(n[0]) * offset_m,
            float(n[1]) * offset_m,
            float(n[2]) * offset_m)


def _create_zone_boundary_objects(context, zone_groups, space_color_map,
                                   standard='din_18599_1'):
    """
    Build one Blender mesh object per zone by merging all constituent IFC spaces.

    *standard* controls how zone boundaries are placed at walls:

    DIN V 18599-1:
      Stage EXT  – Exterior wall: extrude outward by full T → zone reaches outer face.
      Stage 1    – Internal wall within the same zone: dissolve (fill with wall solid).
      Stage 2a   – Cross-zone, heated ↔ unheated: heated +T, unheated = inner face.
      Stage 2b   – Cross-zone, same thermal condition: midplane (+T/2 each side).

    VDI 6020:
      Stage EXT  – Exterior wall: extrude outward by T/2 → zone reaches wall centre.
      Stage 1    – Internal wall within the same zone: dissolve (same as above).
      Stage 2a   – Cross-zone, heated ↔ unheated: midplane (+T/2 each side).
      Stage 2b   – Cross-zone, same thermal condition: inner face (0) for each side.

    VDI 2078:
      Stage EXT  – Exterior wall: extrude outward by full T → zone reaches outer face.
      Stage 1    – Internal wall within the same zone: dissolve (same as above).
      Stage 2     – ALL cross-zone interior walls: inner face (0) regardless of thermal condition.

    ASHRAE 140-2020:
      Stage EXT  – Exterior wall: inner face only (0) → zone stops at inner surface of outer wall.
      Stage 1    – Internal wall within the same zone: dissolve (same as above).
      Stage 2     – ALL cross-zone interior walls: midplane (+T/2) regardless of thermal condition.

    Default   – All other boundaries (floors, ceilings): add the inner-face polygon unchanged.
    Weld coincident vertices with remove_doubles (1 cm tolerance).

    Objects land in a 'BEM Zones' collection visible in the Blender outliner.
    Returns (n_created, warnings) where warnings is a list of human-readable
    strings about missing geometry, empty zones, or non-convex walls.
    """
    import bpy       # type: ignore[import]
    import bmesh as bm_mod  # type: ignore[import]

    warnings = []
    tool, ue = _get_ifc_tools()
    if tool is None:
        return 0, ["Bonsai not available — cannot create zone boundaries."]
    model = tool.Ifc.get()
    np_mod, placement_mod = _get_geom_tools()
    if np_mod is None:
        return 0, ["NumPy or ifcopenshell.util.placement not available — "
                   "zone boundary geometry cannot be built."]
    unit_scale = _get_unit_scale(model)

    # wall_gid → set of zone_keys that have a space boundary on that wall
    wall_to_zones = _build_zone_adjacency(model, zone_groups)

    _remove_bem_zone_objects()
    zone_coll = bpy.data.collections.new(_BEM_ZONE_COLL)
    context.scene.collection.children.link(zone_coll)

    n_created = 0

    for (heated, storey, comp_idx), items in zone_groups.items():
        zone_key   = (heated, storey, comp_idx)
        zone_color = space_color_map.get(items[0].global_id if items else None,
                                         (0.5, 0.5, 0.5))

        cond_label   = "beheizt" if heated else "unbeheizt"
        storey_label = f"_{storey}" if storey else ""
        comp_label   = f"_{comp_idx + 1}" if comp_idx > 0 else ""
        obj_name     = f"Zone_{cond_label}{storey_label}{comp_label}"

        bm = bm_mod.new()
        dissolved_wall_gids = set()   # intra-zone walls → fill with solid mesh
        zone_has_boundary_geom = False   # track if any boundary polygon was found

        for item in items:
            try:
                entity = model.by_guid(item.global_id)
            except Exception:
                continue

            # Space → world transform (translations in project units).
            # Compose with unit_scale so final coords are in metres.
            world_xf = None
            op = getattr(entity, "ObjectPlacement", None)
            if op is not None:
                try:
                    world_xf = placement_mod.get_local_placement(op)
                except Exception:
                    pass

            def _to_world(v_local):
                """Project-unit local → metre world coordinates."""
                if world_xf is not None:
                    w = world_xf @ np_mod.array([v_local[0], v_local[1], v_local[2], 1.0])
                    return (float(w[0]) * unit_scale,
                            float(w[1]) * unit_scale,
                            float(w[2]) * unit_scale)
                return (float(v_local[0]) * unit_scale,
                        float(v_local[1]) * unit_scale,
                        float(v_local[2]) * unit_scale)

            boundaries = [r for r in (getattr(entity, "BoundedBy", None) or [])
                          if r.is_a("IfcRelSpaceBoundary")]

            # Pre-compute geometry for every boundary of this space
            geom_cache = {}
            for rel in boundaries:
                try:
                    g = _boundary_geometry(rel, np_mod, placement_mod, unit_scale)
                except Exception:
                    g = None
                if g is not None:
                    geom_cache[rel.id()] = g
            if geom_cache:
                zone_has_boundary_geom = True

            # Approximate space centroid (space-local frame) for outward-normal
            # determination in Stage 2 cross-zone slab extrusion.
            _all_ctrs = [geom_cache[r.id()][1] for r in boundaries
                         if r.id() in geom_cache]
            space_ctr = (np_mod.mean(np_mod.array(_all_ctrs), axis=0)
                         if _all_ctrs else None)

            for rel in boundaries:
                el = getattr(rel, "RelatedBuildingElement", None)
                if el is None:
                    continue

                g = geom_cache.get(rel.id())
                if g is None:
                    continue
                _area, _centroid, _normal, verts = g

                flag = (getattr(rel, "InternalOrExternalBoundary", None) or "").upper()

                # ----------------------------------------------------------------
                # Classify the boundary element.
                #
                # An exterior wall is identified by the IFC boundary flag OR by
                # Pset_WallCommon.IsExternal = True.  The second check is a
                # data-quality fallback: some IFC exporters set the boundary flag
                # to INTERNAL for exterior walls (confirmed in Smiley West 1.OG),
                # which would otherwise cause the full wall solid to be dissolved
                # into the zone mesh and push its envelope beyond the building skin.
                # ----------------------------------------------------------------
                is_wall        = el.is_a("IfcWall")
                is_ext_by_flag = flag.startswith("EXTERNAL")
                is_ext_by_pset = is_wall and _is_external_wall(el, ue)

                # ---- Stage EXT: exterior wall → extend zone to outer building face ----
                # Gate on Pset_WallCommon.IsExternal = True to confirm the wall is truly
                # on the building skin.  Walls that carry flag=EXTERNAL but have
                # IsExternal=False are interior walls with a bad flag; without pset
                # confirmation they fall through to Stage 1/2 below, preventing a slab
                # from being incorrectly inserted in the middle of a merged zone.
                if is_wall and is_ext_by_pset:
                    if is_ext_by_flag:
                        # Both flag and pset agree: inner-face polygon correctly positioned.
                        # ASHRAE 140-2020: boundary = inner face → add polygon flat (no slab).
                        # DIN V 18599-1 / VDI 2078: extrude outward by full T (outer face).
                        # VDI 6020: extrude outward by T/2 (wall centre).
                        if len(verts) >= 3:
                            if standard == 'ashrae_140':
                                try:
                                    bm.faces.new([bm.verts.new(_to_world(v)) for v in verts])
                                except Exception:
                                    pass
                            else:
                                _wo = tool.Ifc.get_object(el) if tool else None
                                T = _get_wall_thickness(el, ue, unit_scale, _wo) or 0.20
                                ext_offset = T * 0.5 if standard == 'vdi_6020' else T
                                off = _compute_wall_offset(
                                    _normal, _centroid, space_ctr, world_xf, ext_offset, np_mod)
                                if off is not None:
                                    inner_w = [_to_world(v) for v in verts]
                                    outer_w = [(v[0]+off[0], v[1]+off[1], v[2]+off[2])
                                               for v in inner_w]
                                    _add_slab_to_bm(bm, inner_w, outer_w)
                    else:
                        # Pset confirms exterior, flag is INTERNAL (IFC data quality
                        # bug, Smiley West 1.OG).  The polygon is at the OUTER face.
                        # ASHRAE 140-2020: boundary = inner face → shift inward by T, add flat.
                        # DIN V 18599-1 / VDI 2078: slab outer face → inner face (T inward).
                        # VDI 6020: slab outer face → midplane (T/2 inward).
                        if len(verts) >= 3:
                            _wo = tool.Ifc.get_object(el) if tool else None
                            T   = _get_wall_thickness(el, ue, unit_scale, _wo) or 0.20
                            if standard == 'ashrae_140':
                                off = _compute_wall_offset(
                                    _normal, _centroid, space_ctr, world_xf, T, np_mod)
                                if off is not None:
                                    neg = (-off[0], -off[1], -off[2])
                                    outer_w = [_to_world(v) for v in verts]
                                    inner_w = [(v[0]+neg[0], v[1]+neg[1], v[2]+neg[2])
                                               for v in outer_w]
                                    try:
                                        bm.faces.new([bm.verts.new(v) for v in inner_w])
                                    except Exception:
                                        pass
                                else:
                                    dissolved_wall_gids.add(el.GlobalId)
                            else:
                                ext_offset = T * 0.5 if standard == 'vdi_6020' else T
                                off = _compute_wall_offset(
                                    _normal, _centroid, space_ctr, world_xf, ext_offset, np_mod)
                                if off is not None:
                                    neg = (-off[0], -off[1], -off[2])
                                    outer_w = [_to_world(v) for v in verts]
                                    inner_w = [(v[0]+neg[0], v[1]+neg[1], v[2]+neg[2])
                                               for v in outer_w]
                                    _add_slab_to_bm(bm, inner_w, outer_w)
                                else:
                                    dissolved_wall_gids.add(el.GlobalId)
                    continue

                if is_wall and flag == "INTERNAL":
                    adj_zones = wall_to_zones.get(el.GlobalId, set())
                    if adj_zones <= {zone_key}:
                        # ---- Stage 1: intra-zone wall → fill with Blender solid ----
                        dissolved_wall_gids.add(el.GlobalId)
                    elif len(verts) >= 3:
                        # ---- Stage 2: cross-zone wall --------------------------------
                        # DIN V 18599-1:
                        #   a) Heated ↔ Unheated: heated side +T (outer face), unheated = inner face.
                        #   b) Same thermal condition, different zones: midplane (+T/2).
                        #
                        # VDI 6020:
                        #   a) Heated ↔ Unheated: midplane (+T/2) for BOTH sides.
                        #   b) Same thermal condition, different zones: inner face (0).
                        #
                        # VDI 2078:
                        #   All interior cross-zone walls → inner face (0) regardless of condition.
                        #
                        # ASHRAE 140-2020:
                        #   All interior cross-zone walls → midplane (+T/2) regardless of condition.
                        # ---------------------------------------------------------------
                        other_zones     = adj_zones - {zone_key}
                        adj_conditions  = {z[0] for z in other_zones}   # set of booleans
                        thermal_mismatch = bool(adj_conditions - {heated})

                        _wo = tool.Ifc.get_object(el) if tool else None
                        T = _get_wall_thickness(el, ue, unit_scale, _wo) or 0.20

                        if standard == 'vdi_2078':
                            offset = 0.0           # inner face — all cross-zone walls
                        elif standard == 'ashrae_140':
                            offset = T * 0.5       # midplane — all cross-zone walls
                        elif standard == 'vdi_6020':
                            if thermal_mismatch:
                                offset = T * 0.5   # midplane for both sides
                            else:
                                offset = 0.0       # inner face — wall not included in either zone
                        else:  # din_18599_1
                            if thermal_mismatch:
                                offset = T if heated else 0.0
                            else:
                                offset = T * 0.5

                        if offset > 1e-6:
                            off = _compute_wall_offset(
                                _normal, _centroid, space_ctr, world_xf, offset, np_mod)
                            if off is not None:
                                inner_w = [_to_world(v) for v in verts]
                                outer_w = [(v[0]+off[0], v[1]+off[1], v[2]+off[2])
                                           for v in inner_w]
                                _add_slab_to_bm(bm, inner_w, outer_w)
                        else:
                            # Boundary = inner face: add the polygon unchanged.
                            try:
                                bm.faces.new([bm.verts.new(_to_world(v)) for v in verts])
                            except Exception:
                                pass
                    continue

                # ---- Default: floors, ceilings → keep inner-face polygon --------
                if len(verts) >= 3:
                    try:
                        bm_verts = [bm.verts.new(_to_world(v)) for v in verts]
                        bm.faces.new(bm_verts)
                    except Exception:
                        pass

        # ---- Fill intra-zone gaps -------------------------------------------
        # Add the Blender mesh of each dissolved internal wall.  The wall solid
        # (a box with its door/window cutouts) fills the wall-thickness gap
        # between the inner-face boundary surfaces of the two adjacent spaces,
        # removing dark bands without any loop-bridging heuristic.
        for gid in dissolved_wall_gids:
            try:
                wall_entity = model.by_guid(gid)
                wall_obj    = tool.Ifc.get_object(wall_entity)
                if wall_obj is not None and wall_obj.type == 'MESH':
                    _add_wall_faces_to_bm(bm, wall_obj, bm_mod)
            except Exception:
                pass

        # ---- Shapely footprint: rebuild zone as a clean closed solid -----------
        # Pipeline:
        # 1. Collect horizontal face polygons from the assembled bmesh.
        # 2. Union + morphological closing (CLOSE_R) to fill corner notches where
        #    internal walls meet the facade or two external walls meet.
        # 3. simplify(0.001) removes collinear/near-duplicate vertices produced
        #    by the closing operation.
        # 4. buffer(0.001) + simplify(0.001) resolves self-touching "figure-8"
        #    rings that arise where two zone pieces share only a single edge.
        # 5. Fill holes < MIN_HOLE_AREA (corner artefacts, not real rooms).
        # 6. Extrude to a closed shell using edge-loop + triangle_fill for the
        #    floor/ceiling (handles concave polygons robustly) and quads for walls.
        # Falls back to the raw bmesh if Shapely is unavailable.
        CLOSE_R       = 0.20   # morphological closing radius (m)
        MIN_HOLE_AREA = 0.5    # holes smaller than this (m²) are filled

        _shapely_ok  = False
        _footprint   = None
        _fp_z_lo     = None
        _fp_z_hi     = None

        if bm.verts:
            try:
                from shapely.geometry import Polygon as _ShPoly
                from shapely.ops import unary_union as _shunion

                # Face normals are zero on a programmatically-assembled bmesh
                # until explicitly computed — must call before reading face.normal.z
                bm.normal_update()

                _z_vals = [v.co.z for v in bm.verts]
                _fp_z_lo = min(_z_vals)
                _fp_z_hi = max(_z_vals)

                _face_polys = []
                for face in bm.faces:
                    if abs(face.normal.z) > 0.5:
                        _coords = [(v.co.x, v.co.y) for v in face.verts]
                        try:
                            _p = _ShPoly(_coords)
                            if _p.is_valid and not _p.is_empty and _p.area > 1e-4:
                                _face_polys.append(_p)
                        except Exception:
                            pass

                if _face_polys:
                    _merged = _shunion(_face_polys)
                    _fp = (_merged
                           .buffer(CLOSE_R, join_style=2)
                           .buffer(-CLOSE_R, join_style=2))

                    # Remove collinear/near-duplicate vertices from closing
                    _fp = _fp.simplify(0.001)
                    # Resolve self-touching "figure-8" rings (single-edge joins)
                    _fp = _fp.buffer(0.001).simplify(0.001)

                    # Fill holes that are corner artefacts (too small to be rooms)
                    def _fill_small_holes(poly):
                        if poly.geom_type == "Polygon":
                            _kept = [h for h in poly.interiors
                                     if _ShPoly(h).area >= MIN_HOLE_AREA]
                            return _ShPoly(poly.exterior, _kept)
                        elif poly.geom_type == "MultiPolygon":
                            return _shunion([_fill_small_holes(g) for g in poly.geoms])
                        return poly

                    _fp = _fill_small_holes(_fp)

                    # Fill small step-corners where two external walls meet at a
                    # building corner.  Morphological closing cannot fill these because
                    # the closing radius equals the wall thickness, so the fill cancels
                    # out.  Instead, for each concave vertex B between neighbours A and
                    # C: if the missing-corner triangle area is tiny (< MAX_STEP m²),
                    # replace B with the outer right-angle corner D — the bounding-box
                    # corner of (A, C) that lies outside the polygon.  Large concavities
                    # (L-shapes, stairwells) are left untouched.
                    def _fix_step_corners(poly, max_step=0.12):
                        if poly.geom_type != "Polygon":
                            return poly
                        def _fix_ring(coords_in):
                            pts = list(coords_in)[:-1]
                            n = len(pts)
                            out = []
                            i = 0
                            while i < n:
                                A = pts[(i - 1) % n]
                                B = pts[i]
                                C = pts[(i + 1) % n]
                                ax, ay = A[0] - B[0], A[1] - B[1]
                                cx, cy = C[0] - B[0], C[1] - B[1]
                                cross = ax * cy - ay * cx   # negative → concave
                                if cross < -1e-6 and abs(cross) * 0.5 < max_step:
                                    cands = [
                                        (min(A[0], C[0]), min(A[1], C[1])),
                                        (min(A[0], C[0]), max(A[1], C[1])),
                                        (max(A[0], C[0]), min(A[1], C[1])),
                                        (max(A[0], C[0]), max(A[1], C[1])),
                                    ]
                                    replaced = False
                                    for cand in cands:
                                        not_A = (abs(cand[0]-A[0])>0.001
                                                 or abs(cand[1]-A[1])>0.001)
                                        not_C = (abs(cand[0]-C[0])>0.001
                                                 or abs(cand[1]-C[1])>0.001)
                                        if not_A and not_C:
                                            from shapely.geometry import Point as _Pt
                                            if not poly.contains(_Pt(cand)):
                                                out.append(cand)
                                                i += 1
                                                replaced = True
                                                break
                                    if replaced:
                                        continue
                                out.append(B)
                                i += 1
                            return out + [out[0]]
                        try:
                            new_ext = _fix_ring(_fp.exterior.coords)
                            new_holes = [_fix_ring(h.coords) for h in _fp.interiors]
                            fixed = _ShPoly(new_ext, new_holes)
                            return fixed.simplify(0.001)
                        except Exception:
                            return poly

                    _fp = _fix_step_corners(_fp)

                    if not _fp.is_empty:
                        _footprint  = _fp
                        _shapely_ok = True

            except ImportError:
                pass   # Shapely not available — fall back to raw bmesh below
            except Exception:
                pass   # Don't abort zone creation on footprint failure

        if not bm.verts:
            bm.free()
            if not zone_has_boundary_geom:
                warnings.append(
                    f"Zone '{obj_name}': no IFC space boundary geometry found "
                    f"(missing ConnectionGeometry on IfcRelSpaceBoundary). "
                    f"Boundary object skipped."
                )
            else:
                warnings.append(
                    f"Zone '{obj_name}': boundary geometry produced an empty mesh "
                    f"(all faces degenerate or wall solids unavailable). "
                    f"Boundary object skipped."
                )
            continue

        if _shapely_ok and _footprint is not None:
            # Build a new clean bmesh from the Shapely footprint shell.
            # Walls are simple quads. Floor/ceiling uses edge-loop + triangle_fill
            # so concave and L-shaped polygons are filled without self-intersection.
            bm.free()
            bm = bm_mod.new()

            def _add_cap_ring(ring_coords, z, flip):
                """Add a triangulated floor/ceiling cap for one polygon ring."""
                pts = list(ring_coords)[:-1]
                if len(pts) < 3:
                    return
                if flip:
                    pts = pts[::-1]
                cap_verts = [bm.verts.new((x, y, z)) for x, y in pts]
                cap_edges = []
                for i in range(len(cap_verts)):
                    try:
                        cap_edges.append(
                            bm.edges.new([cap_verts[i],
                                          cap_verts[(i + 1) % len(cap_verts)]])
                        )
                    except Exception:
                        pass
                bm_mod.ops.triangle_fill(bm, use_beauty=True, edges=cap_edges)

            def _add_walls_ring(ring_coords, z_lo, z_hi):
                pts = list(ring_coords)[:-1]
                n = len(pts)
                for i in range(n):
                    x0, y0 = pts[i]
                    x1, y1 = pts[(i + 1) % n]
                    v0 = bm.verts.new((x0, y0, z_lo))
                    v1 = bm.verts.new((x1, y1, z_lo))
                    v2 = bm.verts.new((x1, y1, z_hi))
                    v3 = bm.verts.new((x0, y0, z_hi))
                    try:
                        bm.faces.new([v0, v1, v2, v3])
                    except Exception:
                        pass

            def _extrude_polygon(poly, z_lo, z_hi):
                if poly.geom_type == "MultiPolygon":
                    for part in poly.geoms:
                        _extrude_polygon(part, z_lo, z_hi)
                    return
                _add_cap_ring(poly.exterior.coords, z_lo, flip=False)
                _add_cap_ring(poly.exterior.coords, z_hi, flip=True)
                _add_walls_ring(poly.exterior.coords, z_lo, z_hi)
                for interior in poly.interiors:
                    _add_cap_ring(interior.coords, z_lo, flip=True)
                    _add_cap_ring(interior.coords, z_hi, flip=False)
                    _add_walls_ring(interior.coords, z_lo, z_hi)

            _extrude_polygon(_footprint, _fp_z_lo, _fp_z_hi)

        # Weld coincident vertices and fix normals.
        bm_mod.ops.remove_doubles(bm, verts=list(bm.verts), dist=0.005)
        bm_mod.ops.recalc_face_normals(bm, faces=list(bm.faces))

        mesh = bpy.data.meshes.new(obj_name)
        bm.to_mesh(mesh)
        bm.free()
        mesh.update()

        obj = bpy.data.objects.new(obj_name, mesh)
        zone_coll.objects.link(obj)
        mat = _make_bim_material(f"BEM_Zone_{obj_name}", zone_color, 1.0, True)
        obj.data.materials.append(mat)
        obj.color = (zone_color[0], zone_color[1], zone_color[2], 1.0)
        obj["bim_bem_zone"] = True

        n_created += 1

    return n_created, warnings


def _remove_existing_bem_zones(model):
    """Delete any IfcZone entities previously created by this tool."""
    for zone in list(model.by_type("IfcZone")):
        if (getattr(zone, "Description", None) or "") == _BEM_ZONE_TAG:
            for rel in list(model.by_type("IfcRelAssignsToGroup")):
                if getattr(rel, "RelatingGroup", None) == zone:
                    model.remove(rel)
            model.remove(zone)


def _create_and_visualize_zones(context):
    """Create IfcZone entities and colorize the viewport. Returns (status, msg)."""
    global _bim_bem_backup, _bim_bem_shading, _bim_bem_hidden
    tool, _ = _get_ifc_tools()
    if tool is None or tool.Ifc.get() is None:
        return "CANCELLED", "No IFC project loaded."
    model = tool.Ifc.get()
    scene = context.scene

    try:
        import ifcopenshell.guid as _guid  # type: ignore[import]
    except Exception:
        return "CANCELLED", "ifcopenshell not available."

    zone_groups = _build_zone_groups(scene, model)
    if not zone_groups:
        return "CANCELLED", "No spaces available — add spaces in the Room Selector first."

    # Spatial-connectivity check: warn about disconnected spaces within a zone.
    connectivity_warnings = _check_zone_connectivity(model, zone_groups)

    _remove_existing_bem_zones(model)

    owner_history = next(iter(model.by_type("IfcOwnerHistory")), None)

    # Create one IfcZone per (heated, storey) and assign member spaces.
    # Each heated condition gets a distinct colour index; same colour on all floors.
    space_color_map = {}   # global_id -> rgb tuple
    color_key_map   = {}   # heated_bool -> rgb tuple
    warm_idx = cool_idx = 0

    for (heated, storey, comp_idx), items in zone_groups.items():
        if heated not in color_key_map:
            color_key_map[heated] = _zone_color(warm_idx if heated else cool_idx, heated)
            if heated:
                warm_idx += 1
            else:
                cool_idx += 1
        color = color_key_map[heated]

        cond_label   = "beheizt" if heated else "unbeheizt"
        storey_label = f" {storey}" if storey else ""
        comp_label   = f" ({comp_idx + 1})" if comp_idx > 0 else ""
        zone_name    = f"{cond_label}{storey_label}{comp_label}"

        zone_kw = {"GlobalId": _guid.new(), "Name": zone_name,
                   "Description": _BEM_ZONE_TAG}
        if owner_history:
            zone_kw["OwnerHistory"] = owner_history
        zone = model.create_entity("IfcZone", **zone_kw)

        members = []
        for item in items:
            try:
                members.append(model.by_guid(item.global_id))
                space_color_map[item.global_id] = color
            except Exception:
                pass

        if members:
            rel_kw = {"GlobalId": _guid.new(), "RelatedObjects": members,
                      "RelatingGroup": zone}
            if owner_history:
                rel_kw["OwnerHistory"] = owner_history
            model.create_entity("IfcRelAssignsToGroup", **rel_kw)

    # ---- Viewport colorization ------------------------------------------
    # Restore objects hidden by any previous zone run before taking backup.
    for obj in scene.objects:
        if obj.name in _bim_bem_hidden:
            obj.hide_viewport = False
    _bim_bem_hidden = set()

    if not scene.bim_query_viz_active:
        _bim_bem_backup = {o.name: tuple(o.color)
                           for o in scene.objects if o.type == 'MESH'}

    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            shd = area.spaces[0].shading
            if not scene.bim_query_viz_active:
                _bim_bem_shading = {'type': shd.type,
                                    'color_type': getattr(shd, 'color_type', 'MATERIAL')}
            shd.type       = 'SOLID'
            shd.color_type = 'OBJECT'
            break

    # Hide the IFC space objects; opaque zone boundary meshes represent each zone.
    space_objs = set()
    for item in scene.bim_query_spaces:
        if item.global_id not in space_color_map:
            continue
        try:
            obj = tool.Ifc.get_object(model.by_guid(item.global_id))
        except Exception:
            obj = None
        if obj is None:
            continue
        obj.hide_viewport = True
        _bim_bem_hidden.add(obj.name)
        space_objs.add(obj)

    for obj in scene.objects:
        if obj.type == 'MESH' and obj not in space_objs:
            obj.color = (0.7, 0.7, 0.7, 0.1)

    scene.bim_query_viz_active = True
    scene.bim_query_colorize_mode = 'zones'

    # Build DIN V 18599-1 boundary objects and place them in "BEM Zones" collection
    n_objs, geom_warnings = _create_zone_boundary_objects(
        context, zone_groups, space_color_map,
        standard=scene.bim_transform_standard)

    # Save IFC zones to file so they persist between sessions.
    try:
        import bpy as _bpy
        _props = getattr(_bpy.context.scene, "BIMProperties", None)
        ifc_path = _props.ifc_file if _props else None
        if ifc_path:
            model.write(ifc_path)
    except Exception:
        geom_warnings.append("IFC zones created in memory but could not be saved to disk "
                             "(no file path found — save the project first).")

    # Compose the status message; surface all warnings via INFO reports.
    all_warnings = connectivity_warnings + geom_warnings
    msg = (f"Created {len(zone_groups)} IFC zone(s), "
           f"{n_objs} boundary object(s) in '{_BEM_ZONE_COLL}' collection.")
    if all_warnings:
        msg += "  Warnings: " + "  |  ".join(all_warnings)
    return "FINISHED", msg


def _apply_colorize_heating(context):
    """Colorize spaces by heated / unheated condition. Returns (status, msg)."""
    global _bim_bem_backup, _bim_bem_shading, _bim_bem_hidden
    tool, _ = _get_ifc_tools()
    if tool is None or tool.Ifc.get() is None:
        return "CANCELLED", "No IFC project loaded."
    model = tool.Ifc.get()
    scene = context.scene
    scene.bim_query_colorize_mode = 'heating'

    for obj in scene.objects:
        if obj.name in _bim_bem_hidden:
            obj.hide_viewport = False
    _bim_bem_hidden = set()

    filters_coll = _active_filters(scene)
    enabled_types = {f.name for f in filters_coll if f.enabled}
    has_filters = len(filters_coll) > 0

    colored_objs = {}
    for item in scene.bim_query_spaces:
        if not item.has_result:
            continue
        if has_filters and _item_group_key(item, scene) not in enabled_types:
            continue
        try:
            entity = model.by_guid(item.global_id)
            obj = tool.Ifc.get_object(entity)
        except Exception:
            obj = None
        if obj is None:
            continue
        colored_objs[obj] = _COLOR_HEATED if _is_space_heated(item, scene) else _COLOR_UNHEATED

    if not colored_objs:
        return "CANCELLED", "No space objects found in the 3D viewport."

    if not scene.bim_query_viz_active:
        _bim_bem_backup = {o.name: tuple(o.color) for o in scene.objects if o.type == 'MESH'}

    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            shd = area.spaces[0].shading
            if not scene.bim_query_viz_active:
                _bim_bem_shading = {
                    'type':       shd.type,
                    'color_type': getattr(shd, 'color_type', 'MATERIAL'),
                }
            shd.type       = 'SOLID'
            shd.color_type = 'OBJECT'
            break

    space_set = set(colored_objs.keys())
    for obj, rgb in colored_objs.items():
        obj.color = (rgb[0], rgb[1], rgb[2], 1.0)
    for obj in scene.objects:
        if obj.type == 'MESH' and obj not in space_set:
            obj.color = (0.7, 0.7, 0.7, 0.1)

    scene.bim_query_viz_active = True
    return "FINISHED", f"Colored {len(colored_objs)} space(s) by heating condition."


class BIM_OT_colorize_heating(Operator):
    bl_idname = "bim_query.colorize_heating"
    bl_label = "Colorize by Heating Condition"
    bl_description = "Color spaces by thermal conditioning: orange = heated, blue = unheated"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        status, msg = _apply_colorize_heating(context)
        self.report({"INFO" if status == "FINISHED" else "WARNING"}, msg)
        return {status}


class BIM_OT_colorize_spaces(Operator):
    bl_idname = "bim_query.colorize_spaces"
    bl_label = "Colorize by Criticality"
    bl_description = "Color spaces by criticality ranking using viridis (higher = darker purple)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        status, msg = _apply_colorize(context, compliance_mode=False)
        self.report({"INFO" if status == "FINISHED" else "WARNING"}, msg)
        return {status}


class BIM_OT_colorize_compliance(Operator):
    bl_idname = "bim_query.colorize_compliance"
    bl_label = "Colorize Pass / Fail"
    bl_description = "Color spaces by DIN 4108-2 compliance: dark purple = fail (S_vorh > S_zul), yellow = pass"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        status, msg = _apply_colorize(context, compliance_mode=True)
        self.report({"INFO" if status == "FINISHED" else "WARNING"}, msg)
        return {status}


class BIM_OT_reset_colors(Operator):
    bl_idname = "bim_query.reset_colors"
    bl_label = "Reset Colors"
    bl_description = "Restore original materials and switch back to Solid shading"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        global _bim_bem_backup, _bim_bem_shading, _bim_bem_hidden
        scene = context.scene
        for obj in scene.objects:
            if obj.name in _bim_bem_hidden:
                obj.hide_viewport = False
        _bim_bem_hidden = set()
        restored = 0
        for obj in scene.objects:
            if obj.type != 'MESH':
                continue
            if obj.name in _bim_bem_backup:
                obj.color = _bim_bem_backup[obj.name]
                restored += 1
            else:
                obj.color = (1.0, 1.0, 1.0, 1.0)
        _bim_bem_backup = {}
        orig_type  = _bim_bem_shading.get('type',       'SOLID')
        orig_color = _bim_bem_shading.get('color_type', 'MATERIAL')
        _bim_bem_shading = {}
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                shd = area.spaces[0].shading
                shd.type = orig_type
                if hasattr(shd, 'color_type'):
                    shd.color_type = orig_color
                break
        scene.bim_query_viz_active = False
        self.report({"INFO"}, f"Restored {restored} object colour(s).")
        return {"FINISHED"}


class BIM_OT_select_space(Operator):
    bl_idname = "bim_query.select_space"
    bl_label = "Select in Viewport"
    bl_description = "Select and highlight this space in the 3D viewport"
    bl_options = {"REGISTER", "UNDO"}

    global_id: StringProperty()

    def execute(self, context):
        tool, _ = _get_ifc_tools()
        if tool is None or tool.Ifc.get() is None:
            self.report({"WARNING"}, "No IFC project loaded.")
            return {"CANCELLED"}
        try:
            entity = tool.Ifc.get().by_guid(self.global_id)
            obj = tool.Ifc.get_object(entity)
        except Exception:
            obj = None
        if obj is None:
            self.report({"WARNING"}, "Object not found in viewport.")
            return {"CANCELLED"}
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj
        return {"FINISHED"}

# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #

class BIM_UL_query_spaces(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        st = _list_state['query']
        st['n_drawn'] += 1
        st['scrollbar'] = st['n_drawn'] < st['n_visible']
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            label = item.name
            if item.long_name and item.long_name != item.name:
                label = f"{item.name} - {item.long_name}"
            row = layout.row(align=True)
            sel = row.operator("bim_query.select_space", text="", icon="RESTRICT_SELECT_OFF", emboss=False)
            sel.global_id = item.global_id
            sp = row.split(factor=0.40, align=True)
            sp.label(text=label, icon="MESH_PLANE")
            sp2 = sp.split(factor=0.667, align=True)
            sp2.label(text=item.usage_type or "")
            wfr_col = sp2.row()
            wfr_col.alignment = "CENTER"
            wfr_col.label(text=f"{item.wfr * 100:.1f}%" if item.has_wfr else "")
        elif self.layout_type == "GRID":
            layout.label(text=item.name)

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        filters_coll = _active_filters(context.scene)
        enabled_types = {f.name for f in filters_coll if f.enabled}
        has_filters = len(filters_coll) > 0
        group_by = getattr(context.scene, "bim_query_group_by", "usage")
        flt_flags = []
        for item in items:
            key = item.long_name if group_by == "longname" else item.usage_type
            if not has_filters or key in enabled_types:
                flt_flags.append(self.bitflag_filter_item)
            else:
                flt_flags.append(0)
        if group_by == "longname":
            sorted_indices = sorted(
                range(len(items)),
                key=lambda i: (items[i].long_name, items[i].name),
            )
        else:
            sorted_indices = sorted(
                range(len(items)),
                key=lambda i: (items[i].usage_type, items[i].name),
            )
        flt_neworder = [0] * len(items)
        for display_pos, orig_idx in enumerate(sorted_indices):
            flt_neworder[orig_idx] = display_pos
        _list_state['query']['n_visible'] = sum(1 for f in flt_flags if f)
        return flt_flags, flt_neworder


class BIM_UL_compliance(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        st = _list_state['compliance']
        st['n_drawn'] += 1
        st['scrollbar'] = st['n_drawn'] < st['n_visible']
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            scene = context.scene
            sv = _s_vorh(item, scene)
            sz = _space_szul(item, scene)
            full_name = item.name or "(unnamed)"
            if item.long_name and item.long_name != item.name:
                full_name = f"{item.name} – {item.long_name}"

            row = layout.row(align=True)
            sel = row.operator("bim_query.select_space", text="", icon="RESTRICT_SELECT_OFF", emboss=False)
            sel.global_id = item.global_id

            if sv is None or sz is None:
                row.label(text=full_name, icon="QUESTION")
                return

            delta = sv - sz
            fail = delta > 0
            sp = row.split(factor=0.45, align=True)
            sp.label(text=full_name, icon="CANCEL" if fail else "CHECKMARK")
            sp2 = sp.split(factor=0.333, align=True)
            vc = sp2.row()
            vc.alignment = "CENTER"
            vc.label(text=f"{sv:.3f}")
            sp3 = sp2.split(factor=0.50, align=True)
            vc = sp3.row()
            vc.alignment = "CENTER"
            vc.label(text=f"{sz:.3f}")
            vc = sp3.row()
            vc.alignment = "CENTER"
            vc.label(text=f"{'+'if delta > 0 else ''}{delta:.3f}")
        elif self.layout_type == "GRID":
            layout.label(text=item.name)

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        scene = context.scene
        filters_coll = _active_filters(scene)
        enabled_types = {f.name for f in filters_coll if f.enabled}
        has_filters = len(filters_coll) > 0

        flt_flags = []
        for item in items:
            visible = (not has_filters or _item_group_key(item, scene) in enabled_types) and item.has_result
            flt_flags.append(self.bitflag_filter_item if visible else 0)

        # Sort: failing rooms first (largest positive delta), then passing
        def sort_key(i):
            item = items[i]
            sv = _s_vorh(item, scene)
            sz = _space_szul(item, scene)
            if sv is None or sz is None:
                return (2, 0.0)
            d = sv - sz
            return (0 if d > 0 else 1, -d)

        sorted_indices = sorted(range(len(items)), key=sort_key)
        flt_neworder = [0] * len(items)
        for display_pos, orig_idx in enumerate(sorted_indices):
            flt_neworder[orig_idx] = display_pos
        _list_state['compliance']['n_visible'] = sum(1 for f in flt_flags if f)
        return flt_flags, flt_neworder


class BIM_UL_transform_spaces(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        st = _list_state['transform']
        st['n_drawn'] += 1
        st['scrollbar'] = st['n_drawn'] < st['n_visible']
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            label = item.name
            if item.long_name and item.long_name != item.name:
                label = f"{item.name} - {item.long_name}"
            row = layout.row(align=True)
            sel = row.operator("bim_query.select_space", text="", icon="RESTRICT_SELECT_OFF", emboss=False)
            sel.global_id = item.global_id
            sp = row.split(factor=0.55, align=True)
            sp.label(text=label, icon="MESH_PLANE")
            sp.label(text=item.usage_type or "")
        elif self.layout_type == "GRID":
            layout.label(text=item.name)

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        filters_coll = _active_filters(context.scene)
        enabled_types = {f.name for f in filters_coll if f.enabled}
        has_filters = len(filters_coll) > 0
        group_by = getattr(context.scene, "bim_query_group_by", "usage")
        flt_flags = []
        for item in items:
            key = item.long_name if group_by == "longname" else item.usage_type
            if not has_filters or key in enabled_types:
                flt_flags.append(self.bitflag_filter_item)
            else:
                flt_flags.append(0)
        if group_by == "longname":
            sorted_indices = sorted(
                range(len(items)),
                key=lambda i: (items[i].long_name, items[i].name),
            )
        else:
            sorted_indices = sorted(
                range(len(items)),
                key=lambda i: (items[i].usage_type, items[i].name),
            )
        flt_neworder = [0] * len(items)
        for display_pos, orig_idx in enumerate(sorted_indices):
            flt_neworder[orig_idx] = display_pos
        _list_state['transform']['n_visible'] = sum(1 for f in flt_flags if f)
        return flt_flags, flt_neworder


def _draw_value(layout, label, has, value, fmt="{:.2f}", suffix=""):
    row = layout.row()
    row.label(text=label)
    row.label(text=(fmt.format(value) + suffix) if has else "n/a")


# Per-list state: filter_items writes n_visible; draw_item counts n_drawn.
# The panel reads scrollbar (set by the previous frame's draw pass) so the
# header width is always correct one frame after any list-height change.
_list_state = {
    'query':      {'n_visible': 0, 'n_drawn': 0, 'scrollbar': False},
    'compliance': {'n_visible': 0, 'n_drawn': 0, 'scrollbar': False},
    'transform':  {'n_visible': 0, 'n_drawn': 0, 'scrollbar': False},
}


def _header_row(layout, context, scrollbar_needed):
    """Return a row inset to match template_list item width.

    template_list draws its own inner frame (~5 px each side at scale 1).
    We compensate for:
      - left inset  : 5 px  (template_list left border)
      - right inset : 5 px  (template_list right border)
      - scroll      : 20 px (UI_UNIT_X, only when scrollbar is present)
    All values scale with ui_scale and are derived from the actual region width.
    """
    try:
        ui_scale  = context.preferences.system.ui_scale
        region_w  = context.region.width
        padding   = round(30 * ui_scale)   # sidebar panel + parent-box borders
        content_w = max(1, region_w - padding)
        inset_w   = round(5 * ui_scale)    # template_list border on each side
        scroll_w  = round(20 * ui_scale) if scrollbar_needed else 0

        # --- left spacer -------------------------------------------------
        left_f     = max(0.001, min(0.05, inset_w / content_w))
        left_split = layout.split(factor=left_f, align=True)
        left_split.label(text="")          # consume left column as silent spacer

        # --- content + right spacer (inset + optional scroll) ------------
        remain_w  = max(1, (1.0 - left_f) * content_w)
        right_f   = max(0.80, min(0.999, (remain_w - inset_w - scroll_w) / remain_w))
        return left_split.split(factor=right_f, align=True).row(align=True)

    except Exception:
        return layout.row(align=True)


class BIM_PT_query(Panel):
    bl_label = "BIM to BEM"
    bl_idname = "BIM_PT_query"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BIM to BEM"

    def draw(self, context):
        pass


class BIM_PT_space_transformation(Panel):
    bl_label = "Space Transformation for Building Physics"
    bl_idname = "BIM_PT_space_transformation"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BIM to BEM"
    bl_parent_id = "BIM_PT_query"
    bl_options = {"DEFAULT_CLOSED"}
    bl_order = 1

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        tool, _ = _get_ifc_tools()
        if tool is None:
            layout.label(text="Bonsai not detected.", icon="ERROR")
            layout.label(text="Install & enable the Bonsai add-on.")
            return
        if tool.Ifc.get() is None:
            layout.label(text="No IFC project loaded.", icon="INFO")

        # ---- Add / Refresh --------------------------------------------------
        layout.operator("bim_query.enable_spatial_decomposition", icon="HIDE_OFF",
                        depress=scene.bim_spaces_available)
        row = layout.row(align=True)
        row.operator("bim_query.add_selected", icon="ADD")
        row.operator("bim_query.refresh", text="", icon="FILE_REFRESH")

        # ---- Group-by -------------------------------------------------------
        gb_row = layout.row(align=True)
        gb_row.label(text="Group by:")
        gb_row.prop(scene, "bim_query_group_by", text="")

        # ---- Usage / LongName filter chips ----------------------------------
        active_f = _active_filters(scene)
        if active_f:
            group_by = scene.bim_query_group_by
            counts = {}
            for it in scene.bim_query_spaces:
                key = _item_group_key(it, scene)
                counts[key] = counts.get(key, 0) + 1
            fcol = layout.column(align=True)
            fcol.scale_y = 0.9
            for f in active_f:
                n = counts.get(f.name, 0)
                if group_by == "longname":
                    label = f"{f.name} ({n})" if f.name else f"(no long name) ({n})"
                else:
                    label = f"{f.name} ({n})" if f.name else f"(untyped) ({n})"
                icon = "CHECKBOX_HLT" if f.enabled else "CHECKBOX_DEHLT"
                op = fcol.operator("bim_query.toggle_usage", text=label, icon=icon, depress=f.enabled)
                op.usage_name = f.name

        # ---- Column header + list -------------------------------------------
        _list_state['transform']['n_drawn'] = 0
        t_col = layout.column(align=True)
        thdr = _header_row(t_col, context, _list_state['transform']['scrollbar'])
        thdr.scale_y = 0.55
        thdr.label(text="", icon="BLANK1")
        ts = thdr.split(factor=0.55, align=True)
        ts.label(text="IFC Space Name", icon="BLANK1")
        ts.label(text="Usage")
        t_col.template_list(
            "BIM_UL_transform_spaces", "",
            scene, "bim_query_spaces",
            scene, "bim_query_space_index",
            rows=8,
        )

        # ---- Remove / Clear -------------------------------------------------
        row = layout.row(align=True)
        row.operator("bim_query.remove", icon="REMOVE")
        row.operator("bim_query.clear", icon="TRASH")

        # ---- Thermal conditioning per group ---------------------------------
        # Only show groups that are currently enabled (checked) in the filter.
        # Unchecking a group in the Room Selector instantly removes it here.
        active_heated = [f for f in active_f if f.enabled]
        if active_heated:
            layout.separator(factor=0.6)
            layout.label(text="Thermal Conditioning:")
            tcol = layout.column(align=True)
            for f in active_heated:
                trow = tcol.row(align=True)
                label = f.name if f.name else ("(no long name)" if scene.bim_query_group_by == "longname" else "(untyped)")
                trow.label(text=label)
                op = trow.operator(
                    "bim_query.toggle_heated",
                    text="Heated (20 °C)" if f.heated else "Unheated (≤ 15 °C)",
                    icon="LIGHT_SUN" if f.heated else "FREEZE",
                    depress=f.heated,
                )
                op.group_name = f.name

            layout.separator(factor=0.5)
            heat_row = layout.row(align=True)
            if scene.bim_query_viz_active and scene.bim_query_colorize_mode == 'heating':
                heat_row.operator("bim_query.reset_colors", icon="LOOP_BACK")
                heat_row.operator("bim_query.colorize_heating", text="", icon="FILE_REFRESH")
            else:
                heat_row.operator("bim_query.colorize_heating", icon="SHADING_RENDERED")

        # ---- Standard selection + transform ---------------------------------
        layout.separator(factor=0.6)
        std_row = layout.split(factor=0.28)
        std_row.label(text="Standard:")
        std_row.prop(scene, "bim_transform_standard", text="")
        layout.separator(factor=0.3)
        zone_row = layout.row(align=True)
        if scene.bim_query_viz_active and scene.bim_query_colorize_mode == 'zones':
            zone_row.operator("bim_query.reset_colors", icon="LOOP_BACK")
            zone_row.operator("bim_query.transform_spaces", text="", icon="FILE_REFRESH")
        else:
            zone_row.operator("bim_query.transform_spaces", icon="MODIFIER")
        layout.operator("bim_query.delete_zones", icon="TRASH")
        layout.separator(factor=0.6)
        layout.operator("bim_query.export_zones", icon="DISK_DRIVE")


class BIM_PT_summer_overheating(Panel):
    bl_label = "Summer Overheating Protection"
    bl_idname = "BIM_PT_summer_overheating"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BIM to BEM"
    bl_parent_id = "BIM_PT_query"
    bl_options = {"DEFAULT_CLOSED"}
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # ---- DIN 4108-2 project inputs (dark box, collapsible) -----------
        inp = layout.box()
        inp_icon = 'TRIA_DOWN' if scene.bim_query_input_expanded else 'TRIA_RIGHT'
        ihdr = inp.row()
        ihdr.alignment = "LEFT"
        ihdr.prop(scene, "bim_query_input_expanded",
                  text="Input Parameters", icon=inp_icon, emboss=False)

        if scene.bim_query_input_expanded:
            col = inp.column(align=False)

            def _dd(label, prop):
                """Dropdown row: label left 50 %, widget right 50 %."""
                row = col.split(factor=0.5)
                row.label(text=label)
                row.prop(scene, prop, text="")

            _dd("Klimaregion", "bim_project_klimaregion")
            _dd("Nutzung",     "bim_project_nutzung")
            col.separator(factor=0.5)
            _dd("Nachtlüftung (S₁)", "bim_project_nachtlueftung")
            _dd("Bauart (S₁)",       "bim_project_bauart")
            col.prop(scene, "bim_project_passive_kuehlung", text="Passive Kühlung vorhanden (S₆)")
            col.separator(factor=0.5)
            _dd("g-Wert Verglasung", "bim_project_g_value")
            g_le = scene.bim_project_g_value <= 0.40
            col.label(text=f"  → {'Sonnenschutzglas (S₃ +0.03)' if g_le else 'kein Sonnenschutzglas'}")
            _dd("Sonnenschutz F_C", "bim_project_sonnenschutz")
            # Verglasung (zweifach/dreifach) only affects F_C for internal shading
            # types 2.1–2.3 when g > 0.40; hide it when it has no effect.
            verglasung_relevant = (not g_le) and scene.bim_project_sonnenschutz in ('2_1', '2_2', '2_3')
            if verglasung_relevant:
                _dd("Verglasung", "bim_project_verglasung")
            fc_val = _get_fc(scene)
            col.label(text=f"  → F_C = {fc_val:.2f}")
            col.separator(factor=0.3)
            # S2, S4, S5 are computed per space from IFC geometry data (full-width note)
            note = inp.column(align=True)
            note.scale_y = 0.75
            note.label(text="S₂ (f_WG), S₄ (Fensterneigung), S₅ (Orientierung) calculated from IFC spaces.", icon="INFO")

        # ---- Bonsai / IFC check ------------------------------------------
        tool, _ = _get_ifc_tools()
        if tool is None:
            layout.label(text="Bonsai not detected.", icon="ERROR")
            layout.label(text="Install & enable the Bonsai add-on.")
            return
        if tool.Ifc.get() is None:
            layout.label(text="No IFC project loaded.", icon="INFO")

        # ---- Room Selector (collapsible) --------------------------------
        lst = layout.box()
        rs_icon = 'TRIA_DOWN' if scene.bim_query_room_selector_expanded else 'TRIA_RIGHT'
        rshdr = lst.row()
        rshdr.alignment = "LEFT"
        rshdr.prop(scene, "bim_query_room_selector_expanded",
                   text="Room Selector", icon=rs_icon, emboss=False)

        active_filters = _active_filters(scene)

        if scene.bim_query_room_selector_expanded:
            lst.operator("bim_query.enable_spatial_decomposition", icon="HIDE_OFF",
                         depress=scene.bim_spaces_available)
            row = lst.row(align=True)
            row.operator("bim_query.add_selected", icon="ADD")
            row.operator("bim_query.refresh", text="", icon="FILE_REFRESH")
            # Group-by dropdown (always visible so user can switch modes freely)
            gb_row = lst.row(align=True)
            gb_row.label(text="Group by:")
            gb_row.prop(scene, "bim_query_group_by", text="")
            if active_filters:
                group_by = scene.bim_query_group_by
                counts = {}
                for it in scene.bim_query_spaces:
                    key = _item_group_key(it, scene)
                    counts[key] = counts.get(key, 0) + 1
                fcol = lst.column(align=True)
                fcol.scale_y = 0.9
                for f in active_filters:
                    n = counts.get(f.name, 0)
                    if group_by == "longname":
                        label = f"{f.name} ({n})" if f.name else f"(no long name) ({n})"
                    else:
                        label = f"{f.name} ({n})" if f.name else f"(untyped) ({n})"
                    icon = "CHECKBOX_HLT" if f.enabled else "CHECKBOX_DEHLT"
                    op = fcol.operator("bim_query.toggle_usage", text=label, icon=icon, depress=f.enabled)
                    op.usage_name = f.name

            _list_state['query']['n_drawn'] = 0
            q_col = lst.column(align=True)
            qhdr = _header_row(q_col, context, _list_state['query']['scrollbar'])
            qhdr.scale_y = 0.55
            qhdr.label(text="", icon="BLANK1")
            qs = qhdr.split(factor=0.40, align=True)
            qs.label(text="IFC Space Name", icon="BLANK1")
            qs2 = qs.split(factor=0.667, align=True)
            qs2.label(text="Usage")
            qvc = qs2.row()
            qvc.alignment = "CENTER"
            qvc.label(text="WFR")
            q_col.template_list(
                "BIM_UL_query_spaces", "",
                scene, "bim_query_spaces",
                scene, "bim_query_space_index",
                rows=8,
            )

            row = lst.row(align=True)
            row.operator("bim_query.remove", icon="REMOVE")
            row.operator("bim_query.clear", icon="TRASH")

            # ---- Colorize controls ---------------------------------------
            lst.separator(factor=0.4)
            lst.prop(scene, "bim_query_ranking_metric", text="Metric")
            if scene.bim_query_ranking_metric in ('wfr_orient', 'szul_orient'):
                wrow = lst.row(align=True)
                wrow.prop(scene, "bim_query_weight_n", text="N")
                wrow.prop(scene, "bim_query_weight_e", text="E")
                wrow.prop(scene, "bim_query_weight_s", text="S")
                wrow.prop(scene, "bim_query_weight_w", text="W")
            lst.separator(factor=0.5)
            viz_row = lst.row(align=True)
            if scene.bim_query_viz_active and scene.bim_query_colorize_mode == 'ranking':
                viz_row.operator("bim_query.reset_colors", icon="LOOP_BACK")
                viz_row.operator("bim_query.colorize_spaces", text="", icon="FILE_REFRESH")
            else:
                viz_row.operator("bim_query.colorize_spaces", icon="SHADING_RENDERED")

            # ---- Selected space detail (collapsible, inside Room Selector) --
            idx = scene.bim_query_space_index
            items = scene.bim_query_spaces
            if 0 <= idx < len(items):
                item = items[idx]
                detail_icon = 'TRIA_DOWN' if scene.bim_query_detail_expanded else 'TRIA_RIGHT'
                full_name = item.name or "(unnamed)"
                if item.long_name and item.long_name != item.name:
                    full_name = f"{item.name} – {item.long_name}"
                hdr = lst.row()
                hdr.alignment = "LEFT"
                hdr.prop(scene, "bim_query_detail_expanded",
                         text=f"Detailed View of selected Space:  {full_name}",
                         icon=detail_icon, emboss=False)

            if 0 <= idx < len(items) and scene.bim_query_detail_expanded:
                item = items[idx]
                box = lst
                hrow = box.row(align=True)
                hrow.label(text=item.name or "(unnamed)", icon="MESH_PLANE")
                if item.usage_type:
                    hrow.label(text=item.usage_type)

                if not item.has_result:
                    box.label(text="Not analyzed.")
                else:
                    sp = box.split(factor=0.5)
                    fa_txt  = f"{item.net_floor_area:.2f} m²" if item.has_floor_area else "n/a"
                    vol_txt = f"{item.net_volume:.2f} m³"     if item.has_volume     else "n/a"
                    sp.label(text=f"Floor:  {fa_txt}")
                    sp.label(text=f"Volume:  {vol_txt}")

                    box.separator(factor=0.5)

                    sp = box.split(factor=0.5)
                    wfr_txt = f"{item.wfr * 100:.2f} %" if item.has_wfr else "n/a"
                    owr_txt = f"{item.wwr * 100:.1f} %"  if item.has_wwr else "n/a"
                    sp.label(text=f"WFR (win+door):  {wfr_txt}")
                    sp.label(text=f"OWR:  {owr_txt}")

                    sp = box.split(factor=0.5)
                    wall_txt = f"{item.total_wall_area:.2f} m²" if item.total_wall_area > 0 else "n/a"
                    sp.label(text=f"Wall ({item.ext_wall_count} ext.):  {wall_txt}")
                    sp.label(text="")

                    op_row = box.row()
                    op_row.scale_y = 0.85
                    op_row.label(
                        text=f"Openings:  {item.total_opening_area:.2f} m²"
                             f"  (win {item.window_area:.2f} / door {item.door_area:.2f})"
                    )

                    if item.openings:
                        box.separator(factor=0.5)
                        box.label(text="External openings:")
                        wcol = box.column(align=True)
                        for w in item.openings:
                            wrow = wcol.row()
                            icon = "MESH_PLANE" if w.kind == "Door" else "MOD_LATTICE"
                            wrow.label(text=w.name, icon=icon)
                            area_txt = f"{w.area:.2f} m²" if w.has_area else "n/a"
                            if w.has_orientation:
                                wrow.label(text=f"{w.kind}  {area_txt}  {w.azimuth:.0f}° {w.cardinal}")
                            else:
                                wrow.label(text=f"{w.kind}  {area_txt}")

                    active_orient = [o for o in item.orient if o.wall_area > 0 or o.opening_area > 0]
                    if active_orient:
                        box.separator(factor=0.5)
                        box.label(text="By orientation:")
                        ocol = box.column(align=True)
                        for o in active_orient:
                            orow = ocol.row()
                            orow.label(text=o.cardinal)
                            if o.has_wwr:
                                orow.label(text=f"{o.wwr * 100:.0f}%  (open {o.opening_area:.2f} / wall {o.wall_area:.2f})")
                            elif o.opening_area > 0:
                                orow.label(text=f"open {o.opening_area:.2f}, no wall area")
                            else:
                                orow.label(text=f"wall {o.wall_area:.2f}")

                    all_notes = list(item.notes.split(" | ")) if item.notes else []
                    if item.wall_area_source and "element quantity" in item.wall_area_source:
                        all_notes.insert(0, f"Wall area: {item.wall_area_source}")
                    if all_notes:
                        box.separator(factor=0.5)
                        ncol = box.column(align=True)
                        ncol.scale_y = 0.78
                        for note in all_notes:
                            ncol.label(text=note, icon="DOT")

                    # ---- S_zul breakdown for this space ------------------
                    box.separator(factor=0.5)
                    box.label(text="S_zul (DIN 4108-2):")
                    f_nord = _space_f_nord(item)
                    s1, s2, s3, s4, s5, s6, szul = _din_szul_components(
                        scene, item.wfr if item.has_wfr else 0.0,
                        f_neig=0.0, f_nord=f_nord,
                    )
                    scol = box.column(align=True)
                    scol.scale_y = 0.9

                    def _srow(lbl, val, note=""):
                        r = scol.row()
                        r.label(text=lbl)
                        r.label(text=f"{val:+.3f}" + (f"  ({note})" if note else ""))

                    _srow("S₁  Nachtlüftung / Bauart", s1)
                    _srow("S₂  f_WG", s2,
                          f"f_WG = {item.wfr * 100:.1f}%" if item.has_wfr else "n/a")
                    _srow("S₃  Sonnenschutzglas", s3,
                          f"g = {scene.bim_project_g_value:.2f} ≤ 0.40" if scene.bim_project_g_value <= 0.40 else f"g = {scene.bim_project_g_value:.2f} > 0.40")
                    _srow("S₄  Fensterneigung", s4, "f_neig = 0 (vertikal)")
                    _srow("S₅  Orientierung",   s5, f"f_nord = {f_nord:.2f}")
                    _srow("S₆  Passive Kühlung", s6,
                          scene.bim_project_bauart if scene.bim_project_passive_kuehlung else "nicht vorhanden")
                    scol.separator(factor=0.3)
                    _srow("S_zul", szul if item.has_wfr else 0.0)

        # ---- Compliance check -------------------------------------------
        comp_box = layout.box()
        comp_icon = 'TRIA_DOWN' if scene.bim_query_compliance_expanded else 'TRIA_RIGHT'
        chdr = comp_box.row()
        chdr.alignment = "LEFT"
        chdr.prop(scene, "bim_query_compliance_expanded",
                  text="DIN 4108-2 Compliance Check  (S_vorh vs S_zul)",
                  icon=comp_icon, emboss=False)

        if scene.bim_query_compliance_expanded:
            # Summary counts (respecting active grouping filters)
            filters_coll = _active_filters(scene)
            enabled_types = {f.name for f in filters_coll if f.enabled}
            has_filters = len(filters_coll) > 0
            fail_n = pass_n = 0
            for it in scene.bim_query_spaces:
                if not it.has_result:
                    continue
                if has_filters and _item_group_key(it, scene) not in enabled_types:
                    continue
                sv = _s_vorh(it, scene)
                sz = _space_szul(it, scene)
                if sv is None or sz is None:
                    continue
                if sv > sz:
                    fail_n += 1
                else:
                    pass_n += 1

            summary = comp_box.row()
            summary.label(text=f"{fail_n} fail  /  {pass_n} pass",
                          icon="ERROR" if fail_n else "CHECKMARK")

            _list_state['compliance']['n_drawn'] = 0
            c_col = comp_box.column(align=True)
            chdr2 = _header_row(c_col, context, _list_state['compliance']['scrollbar'])
            chdr2.scale_y = 0.55
            chdr2.label(text="", icon="BLANK1")
            cs = chdr2.split(factor=0.45, align=True)
            cs.label(text="IFC Space Name", icon="BLANK1")
            cs2 = cs.split(factor=0.333, align=True)
            cvc = cs2.row()
            cvc.alignment = "CENTER"
            cvc.label(text="S_vorh")
            cs3 = cs2.split(factor=0.50, align=True)
            cvc = cs3.row()
            cvc.alignment = "CENTER"
            cvc.label(text="S_zul")
            cvc = cs3.row()
            cvc.alignment = "CENTER"
            cvc.label(text="Δ S")
            c_col.template_list(
                "BIM_UL_compliance", "",
                scene, "bim_query_spaces",
                scene, "bim_query_space_index",
                rows=8,
            )

            # Compliance colorize button — overrides the ranking colorize
            comp_row = comp_box.row(align=True)
            if scene.bim_query_viz_active and scene.bim_query_colorize_mode == 'compliance':
                comp_row.operator("bim_query.reset_colors", icon="LOOP_BACK")
                comp_row.operator("bim_query.colorize_compliance", text="", icon="FILE_REFRESH")
            else:
                comp_row.operator("bim_query.colorize_compliance", icon="SHADING_RENDERED")


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #

_classes = (
    BIMQueryOpening,
    BIMQueryOrient,
    BIMQueryUsageFilter,
    BIMQuerySpaceItem,
    BIM_OT_enable_spatial_decomposition,
    BIM_OT_query_add_selected,
    BIM_OT_query_refresh,
    BIM_OT_query_remove,
    BIM_OT_query_clear,
    BIM_OT_query_toggle_usage,
    BIM_OT_toggle_heated,
    BIM_OT_transform_spaces,
    BIM_OT_delete_zones,
    BIM_OT_export_zones,
    BIM_OT_colorize_spaces,
    BIM_OT_colorize_compliance,
    BIM_OT_colorize_heating,
    BIM_OT_reset_colors,
    BIM_OT_select_space,
    BIM_UL_query_spaces,
    BIM_UL_compliance,
    BIM_UL_transform_spaces,
    BIM_PT_query,
    BIM_PT_space_transformation,
    BIM_PT_summer_overheating,
)


_last_active_obj = [None]


def _sync_selection_to_list(scene, depsgraph):
    obj = bpy.context.active_object
    if obj is _last_active_obj[0]:
        return
    _last_active_obj[0] = obj
    if obj is None:
        return
    tool, _ = _get_ifc_tools()
    if tool is None:
        return
    el = tool.Ifc.get_entity(obj)
    if el is None or not el.is_a("IfcSpace"):
        return
    gid = el.GlobalId
    for i, item in enumerate(scene.bim_query_spaces):
        if item.global_id == gid:
            scene.bim_query_space_index = i
            break


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.app.handlers.depsgraph_update_post.append(_sync_selection_to_list)
    bpy.types.Scene.bim_query_spaces = CollectionProperty(type=BIMQuerySpaceItem)
    bpy.types.Scene.bim_query_space_index = IntProperty(default=0)
    bpy.types.Scene.bim_query_usage_filters = CollectionProperty(type=BIMQueryUsageFilter)
    bpy.types.Scene.bim_query_longname_filters = CollectionProperty(type=BIMQueryUsageFilter)
    bpy.types.Scene.bim_query_group_by = EnumProperty(
        name="Group By",
        description="Field used to group and filter spaces in the Room Selector",
        items=[
            ('usage',    "Usage",     "Group by IfcSpaceType usage label"),
            ('longname', "Long Name", "Group by the space's LongName — useful when usage types are undefined"),
        ],
        default='usage',
        update=_on_group_by_change,
    )
    bpy.types.Scene.bim_query_viz_active = BoolProperty(default=False)
    bpy.types.Scene.bim_spaces_available = BoolProperty(
        name="Spaces Available",
        description="Whether IFC spaces are currently visible in the viewport",
        default=False,
    )
    bpy.types.Scene.bim_query_ranking_metric = EnumProperty(
        name="Ranking Metric",
        description="Metric used to rank spaces in the criticality table",
        items=[
            ('wfr',         "WFR",                "Plain window-to-floor ratio — which rooms have the most glazing?"),
            ('wfr_orient',  "WFR × Orientation",  "WFR weighted by cardinal solar exposure (default)"),
            ('szul',        "S_zul",              "DIN 4108-2 allowable solar factor — lowest = most constrained"),
            ('szul_orient', "S_zul × Orientation","Orientation-weighted WFR divided by S_zul — highest solar risk vs. allowance"),
        ],
        default='szul',
    )
    bpy.types.Scene.bim_query_weight_n  = FloatProperty(name="N",  default=0.2, min=0.0, max=1.0, precision=2)
    bpy.types.Scene.bim_query_weight_ne = FloatProperty(name="NE", default=0.4, min=0.0, max=1.0, precision=2)
    bpy.types.Scene.bim_query_weight_e  = FloatProperty(name="E",  default=0.6, min=0.0, max=1.0, precision=2)
    bpy.types.Scene.bim_query_weight_se = FloatProperty(name="SE", default=0.8, min=0.0, max=1.0, precision=2)
    bpy.types.Scene.bim_query_weight_s  = FloatProperty(name="S",  default=1.0, min=0.0, max=1.0, precision=2)
    bpy.types.Scene.bim_query_weight_sw = FloatProperty(name="SW", default=0.8, min=0.0, max=1.0, precision=2)
    bpy.types.Scene.bim_query_weight_w  = FloatProperty(name="W",  default=0.6, min=0.0, max=1.0, precision=2)
    bpy.types.Scene.bim_query_weight_nw = FloatProperty(name="NW", default=0.4, min=0.0, max=1.0, precision=2)

    # ---- DIN 4108-2 project settings -------------------------------------
    bpy.types.Scene.bim_project_klimaregion = EnumProperty(
        name="Klimaregion",
        description="DIN 4108-2 climate region for the project location",
        items=[('A', "A  (sommerkühle Gebiete)",  "Region A – sommerkühle Gebiete"),
               ('B', "B  (Gemäßigte Gebiete)",   "Region B – gemäßigte Gebiete"),
               ('C', "C  (Sommerheiße Gebiete)", "Region C – sommerheiße Gebiete")],
        default='B',
    )
    bpy.types.Scene.bim_project_nutzung = EnumProperty(
        name="Nutzung",
        description="Building use type",
        items=[('wohn',     "Wohngebäude",       "Residential building"),
               ('nichtwohn',"Nichtwohngebäude",  "Non-residential building")],
        default='wohn',
    )
    bpy.types.Scene.bim_project_nachtlueftung = EnumProperty(
        name="Nachtlüftung",
        description="Night ventilation mode",
        items=[('ohne',   "Ohne",                   "No night ventilation"),
               ('erhoht', "Erhöht (n ≥ 2 h⁻¹)",   "Enhanced night ventilation"),
               ('hoch',   "Hoch (n ≥ 5 h⁻¹)",   "High night ventilation")],
        default='ohne',
    )
    bpy.types.Scene.bim_project_bauart = EnumProperty(
        name="Bauart",
        description="Thermal mass of the construction",
        items=[('leicht', "Leicht", "Lightweight construction"),
               ('mittel', "Mittel", "Medium construction"),
               ('schwer', "Schwer", "Heavyweight construction")],
        default='mittel',
    )
    bpy.types.Scene.bim_project_passive_kuehlung = BoolProperty(
        name="Passive Kühlung",
        description="Passive cooling measures are in place",
        default=False,
    )
    bpy.types.Scene.bim_project_g_value = FloatProperty(
        name="g-Wert",
        description="Total solar energy transmittance of the glazing (g-value)",
        default=0.60, min=0.10, max=0.90, step=10, precision=1,
    )
    bpy.types.Scene.bim_project_verglasung = EnumProperty(
        name="Verglasung",
        description="Glazing type (only relevant when g > 0.40)",
        items=[('zweifach', "Zweifach",  "Double glazing"),
               ('dreifach', "Dreifach",  "Triple glazing")],
        default='zweifach',
    )
    bpy.types.Scene.bim_project_sonnenschutz = EnumProperty(
        name="Sonnenschutz",
        description="Shading device type per DIN 4108-2 Tab. 7",
        items=[
            ('none',  "Ohne Sonnenschutzvorrichtung",                                 "No shading — F_C = 1.00"),
            ('2_1',   "Innen: weiß / hoch reflektierend, geringe Transparenz",        "F_C: 0.65 / 0.70 / 0.65"),
            ('2_2',   "Innen: helle Farben / geringe Transparenz",                    "F_C: 0.75 / 0.80 / 0.75"),
            ('2_3',   "Innen: dunkle Farben / höhere Transparenz",                    "F_C: 0.90 / 0.90 / 0.85"),
            ('3_1_1', "Außen: Fensterläden / Rollläden ¾ geschlossen",                "F_C: 0.35 / 0.30 / 0.30"),
            ('3_1_2', "Außen: Fensterläden / Rollläden geschlossen",                  "F_C: 0.15 / 0.10 / 0.10"),
            ('3_2_1', "Außen: Jalousie / Raffstore 45° Lamellenstellung",             "F_C: 0.30 / 0.25 / 0.25"),
            ('3_2_2', "Außen: Jalousie / Raffstore 10° Lamellenstellung",             "F_C: 0.20 / 0.15 / 0.15"),
            ('3_3',   "Außen: Markise, parallel zur Verglasung",                      "F_C: 0.30 / 0.25 / 0.25"),
            ('3_4',   "Außen: Vordächer / Markisen allgemein / freist. Lamellen",     "F_C: 0.55 / 0.50 / 0.50"),
        ],
        default='none',
    )
    bpy.types.Scene.bim_query_colorize_mode = EnumProperty(
        name="Colorize Mode",
        description="Choose what the viewport coloring shows",
        items=[('ranking',    "Criticality",  "Viridis gradient by ranking score"),
               ('compliance', "Compliance",   "Dark purple = fail (S_vorh > S_zul), yellow = pass"),
               ('heating',    "Heating",      "Orange = heated, blue = unheated"),
               ('zones',      "Zones",        "Per-zone colors from Transform into Zones")],
        default='ranking',
    )
    bpy.types.Scene.bim_transform_standard = EnumProperty(
        name="Standard",
        description="Building physics standard used for space transformation",
        items=[
            ('din_18599_1', "DIN EN 12831-1 / DIN V 18599-1",
             "Zones extend to outer wall face; heated zones include full interior wall at unheated boundary"),
            ('vdi_6020',    "VDI 6020",
             "Zones extend to wall centre (exterior) and inner face (interior same-condition) or midplane (interior heated/unheated)"),
            ('vdi_2078',    "VDI 2078",
             "Zones extend to outer wall face (exterior) and inner face of all interior walls — uniform rule"),
            ('ashrae_140',  "ASHRAE 140-2020",
             "Zones extend to inner wall face (exterior) and midplane of all interior walls — uniform rule"),
        ],
        default='din_18599_1',
    )
    bpy.types.Scene.bim_query_input_expanded = BoolProperty(
        name="Input Parameters",
        description="Expand the DIN 4108-2 input parameters section",
        default=False,
    )
    bpy.types.Scene.bim_query_room_selector_expanded = BoolProperty(
        name="Room Selector",
        description="Expand the room selector list",
        default=False,
    )
    bpy.types.Scene.bim_query_detail_expanded = BoolProperty(
        name="Space Detail",
        description="Expand the selected space detail view",
        default=False,
    )
    bpy.types.Scene.bim_query_compliance_expanded = BoolProperty(
        name="Compliance Check",
        description="Expand the DIN 4108-2 compliance check table",
        default=False,
    )


def unregister():
    for _p in ("bim_query_compliance_expanded",
               "bim_query_detail_expanded",
               "bim_query_room_selector_expanded", "bim_query_input_expanded",
               "bim_query_colorize_mode", "bim_transform_standard",
               "bim_project_sonnenschutz", "bim_project_verglasung", "bim_project_g_value",
               "bim_project_passive_kuehlung",
               "bim_project_bauart", "bim_project_nachtlueftung",
               "bim_project_nutzung", "bim_project_klimaregion",
               "bim_query_weight_w", "bim_query_weight_s", "bim_query_weight_e",
               "bim_query_weight_n", "bim_query_ranking_metric", "bim_query_viz_active",
               "bim_spaces_available", "bim_query_group_by"):
        try: delattr(bpy.types.Scene, _p)
        except: pass
    del bpy.types.Scene.bim_query_longname_filters
    del bpy.types.Scene.bim_query_usage_filters
    del bpy.types.Scene.bim_query_space_index
    del bpy.types.Scene.bim_query_spaces
    if _sync_selection_to_list in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_sync_selection_to_list)
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
