"""Turn a LAS/LAZ point cloud into web map layers.

Outputs (written to web/public/data/):
  tiles3d/                                        point cloud as OGC 3D Tiles, streamed by level of detail
  points_pos.bin, points_rgb.bin, points_cls.bin  full point cloud for deck.gl (metre offsets, colours, classes)
  dtm.tif, dsm.tif, ndsm.tif, density.tif         rendered layers as Web Mercator COGs, streamed by HTTP range requests
  qa.pmtiles                                      vector tiles: contours, QA voids and the planned survey area
  dtm_cog.tif, dsm_cog.tif                        analysis grade COGs (UTM, metres, float32) for download
  gaps.geojson, qa_summary.json                   QA results from lidar-qa-toolkit
  meta.json                                       layer metadata read by the web app

Usage:
  python prep/prepare_data.py data/autzen_trim.las --aoi data/autzen_aoi.geojson --utm EPSG:3740
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile

import laspy
import matplotlib
import numpy as np
import pyproj
import subprocess

import rasterio
import rasterio.shutil
from rasterio.enums import ColorInterp
from matplotlib import colormaps
from PIL import Image
from rasterio.transform import from_origin
from rasterio.warp import Resampling, calculate_default_transform, reproject
from scipy import ndimage
from scipy.interpolate import griddata

from lidarqa import analyse, gap_polygons, run_checks, write_report

matplotlib.use("Agg")
OUT = os.path.join(os.path.dirname(__file__), "..", "web", "public", "data")


# ---------------------------------------------------------------- point cloud
def load_points(path, utm):
    las = laspy.read(path)
    src = las.header.parse_crs()
    h_unit = src.axis_info[0].unit_conversion_factor
    # Vertical units follow the horizontal units in this sample (feet); convert to metres
    z_m = np.asarray(las.z) * h_unit
    to_utm = pyproj.Transformer.from_crs(src, utm, always_xy=True)
    e, n = to_utm.transform(np.asarray(las.x), np.asarray(las.y))
    rgb = np.stack([las.red, las.green, las.blue], axis=1).astype(np.float64)
    if rgb.max() > 255:                       # 16 bit colour
        rgb = rgb / 256
    return dict(e=np.asarray(e), n=np.asarray(n), z=z_m, rgb=rgb.clip(0, 255).astype(np.uint8),
                cls=np.asarray(las.classification).astype(np.uint8), src=src)


def write_points(p, utm):
    e0, n0 = float(p["e"].mean()), float(p["n"].mean())
    zmin = float(p["z"].min())
    to_ll = pyproj.Transformer.from_crs(utm, 4326, always_xy=True)
    lon0, lat0 = to_ll.transform(e0, n0)
    pos = np.stack([p["e"] - e0, p["n"] - n0, p["z"] - zmin], axis=1).astype(np.float32)
    pos.tofile(os.path.join(OUT, "points_pos.bin"))
    p["rgb"].tofile(os.path.join(OUT, "points_rgb.bin"))
    p["cls"].tofile(os.path.join(OUT, "points_cls.bin"))
    return dict(count=int(len(pos)), origin=[lon0, lat0], z_min_m=zmin, z_max_m=float(p["z"].max()))


# ---------------------------------------------------------------- rasters
def grid_surface(e, n, z, cell, reducer, bounds):
    minx, miny, maxx, maxy = bounds
    w, h = int(np.ceil((maxx - minx) / cell)), int(np.ceil((maxy - miny) / cell))
    col = np.clip(((e - minx) / cell).astype(int), 0, w - 1)
    row = np.clip(((maxy - n) / cell).astype(int), 0, h - 1)
    idx = row * w + col
    out = np.full(w * h, np.nan)
    if reducer == "max":
        np.fmax.at(out, idx, z)
    else:                                      # mean
        s = np.bincount(idx, weights=z, minlength=w * h)
        c = np.bincount(idx, minlength=w * h)
        out = np.where(c > 0, s / np.maximum(c, 1), np.nan)
    return out.reshape(h, w), from_origin(minx, maxy, cell, cell)


def fill(surface, footprint):
    """Interpolate empty cells inside the footprint (linear, then nearest at the edges)."""
    h, w = surface.shape
    rr, cc = np.mgrid[0:h, 0:w]
    known = ~np.isnan(surface)
    need = footprint & ~known
    if need.any():
        pts = np.column_stack([rr[known], cc[known]])
        vals = surface[known]
        lin = griddata(pts, vals, (rr[need], cc[need]), method="linear")
        miss = np.isnan(lin)
        if miss.any():
            lin[miss] = griddata(pts, vals, (rr[need][miss], cc[need][miss]), method="nearest")
        surface = surface.copy()
        surface[need] = lin
    surface[~footprint] = np.nan
    return surface


def hillshade(z, cell, azimuth=315, altitude=45, exaggeration=2.0):
    zf = np.where(np.isnan(z), np.nanmean(z), z) * exaggeration
    gy, gx = np.gradient(zf, cell)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az, alt = np.radians(360 - azimuth + 90), np.radians(altitude)
    hs = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    return np.clip(hs, 0, 1)


def write_cog(arr, transform, crs, path):
    profile = dict(driver="COG", width=arr.shape[1], height=arr.shape[0], count=1, dtype="float32",
                   crs=crs.to_wkt(), transform=transform, nodata=-9999.0, compress="deflate")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_tif = os.path.join(tmp, "x.tif")
        with rasterio.open(tmp_tif, "w", **{**profile, "driver": "GTiff"}) as dst:
            dst.write(np.where(np.isnan(arr), -9999.0, arr).astype("float32"), 1)
        with rasterio.open(tmp_tif) as src:
            rasterio.shutil.copy(src, path, driver="COG", compress="deflate")


def rgba_to_mercator_png(rgba, transform, crs, name):
    """Reproject an RGBA array to Web Mercator, save a PNG and return its corner coordinates."""
    h, w = rgba.shape[:2]
    left, top = transform.c, transform.f
    right, bottom = left + w * transform.a, top + h * transform.e
    dst_t, dw, dh = calculate_default_transform(crs.to_wkt(), "EPSG:3857", w, h, left, bottom, right, top)
    out = np.zeros((4, dh, dw), dtype=np.uint8)
    for b in range(4):
        reproject(rgba[:, :, b], out[b], src_transform=transform, src_crs=crs.to_wkt(),
                  dst_transform=dst_t, dst_crs="EPSG:3857", resampling=Resampling.nearest)
    Image.fromarray(np.moveaxis(out, 0, -1), "RGBA").save(os.path.join(OUT, f"{name}.png"), optimize=True)
    to_ll = pyproj.Transformer.from_crs(3857, 4326, always_xy=True)
    x0, y0 = dst_t.c, dst_t.f
    x1, y1 = x0 + dw * dst_t.a, y0 + dh * dst_t.e
    corners = [to_ll.transform(x, y) for x, y in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]]
    return [[round(a, 7), round(b, 7)] for a, b in corners]


def write_view_cog(rgba, transform, crs, name):
    """Save a rendered RGBA layer as a Web Mercator COG on the Google Maps tile grid (256 px blocks,
    overviews), so the browser can stream just the tiles in view with HTTP range requests."""
    h, w = rgba.shape[:2]
    out = os.path.join(OUT, f"{name}.tif")
    with tempfile.TemporaryDirectory() as tmp:
        src_path = os.path.join(tmp, "src.tif")
        with rasterio.open(src_path, "w", driver="GTiff", width=w, height=h, count=4, dtype="uint8",
                           crs=crs.to_wkt(), transform=transform) as dst:
            for b in range(4):
                dst.write(rgba[:, :, b], b + 1)
            dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
        with rasterio.open(src_path) as src:
            rasterio.shutil.copy(src, out, driver="COG", TILING_SCHEME="GoogleMapsCompatible", BLOCKSIZE=256,
                                 COMPRESS="DEFLATE", RESAMPLING="NEAREST", OVERVIEWS="IGNORE_EXISTING")
    with rasterio.open(out) as c:
        b = c.bounds
    to_ll = pyproj.Transformer.from_crs(3857, 4326, always_xy=True)
    corners = [to_ll.transform(x, y) for x, y in [(b.left, b.top), (b.right, b.top), (b.right, b.bottom), (b.left, b.bottom)]]
    return [[round(x, 7), round(y, 7)] for x, y in corners]


def write_3dtiles(p, utm):
    """Point cloud as OGC 3D Tiles (pnts) with py3dtiles: heights relative to the lowest point, as in
    the binary layer, so both sit on the basemap."""
    zmin = float(p["z"].min())
    with tempfile.TemporaryDirectory() as tmp:
        las_path = os.path.join(tmp, "cloud.las")
        header = laspy.LasHeader(point_format=3, version="1.2")
        header.scales = [0.001, 0.001, 0.001]
        header.offsets = [float(p["e"].min()), float(p["n"].min()), 0.0]
        header.add_crs(utm)
        las = laspy.LasData(header)
        las.x, las.y, las.z = p["e"], p["n"], p["z"] - zmin
        las.red, las.green, las.blue = (p["rgb"][:, i].astype(np.uint16) * 256 for i in range(3))
        las.classification = p["cls"]
        las.write(las_path)
        out = os.path.join(OUT, "tiles3d")
        subprocess.run(["py3dtiles", "convert", las_path, "--out", out, "--overwrite", "--jobs", "2",
                        "--srs_in", str(utm.to_epsg()), "--srs_out", "4978"], check=True, capture_output=True)
    return "tiles3d/tileset.json"


def write_contours(dtm, transform, utm, interval=1.0, path=None):
    """Contour lines from the bare earth model, as GeoJSON in WGS 84."""
    from skimage import measure
    valid = ~np.isnan(dtm)
    z = np.where(valid, dtm, np.nanmin(dtm))
    to_ll = pyproj.Transformer.from_crs(utm, 4326, always_xy=True)
    feats = []
    for level in np.arange(np.ceil(np.nanmin(dtm) / interval) * interval, np.nanmax(dtm), interval):
        for c in measure.find_contours(z, level, mask=valid):
            if len(c) < 4:
                continue
            x = transform.c + (c[:, 1] + 0.5) * transform.a
            y = transform.f + (c[:, 0] + 0.5) * transform.e
            lon, lat = to_ll.transform(x, y)
            feats.append({"type": "Feature", "properties": {"elev_m": round(float(level), 1),
                                                            "major": bool(round(level) % 5 == 0)},
                          "geometry": {"type": "LineString",
                                       "coordinates": [[round(a, 7), round(b, 7)] for a, b in zip(lon, lat)]}})
    with open(path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f)
    return len(feats)


def write_vector_tiles(layers: dict[str, str]):
    """Pack GeoJSON layers into one PMTiles archive of vector tiles with tippecanoe (zooms 12 to 19)."""
    with tempfile.TemporaryDirectory() as tmp:
        for name, path in layers.items():
            shutil.copy(path, os.path.join(tmp, f"{name}.geojson"))
        args = ["tippecanoe", "-o", "qa.pmtiles", "--force", "-Z", "12", "-z", "19", "-n", "LiDAR QA layers",
                "-N", "Contours, voids and low density areas, planned survey area",
                "--no-tile-size-limit", "--no-feature-limit", "--quiet"]
        for name in layers:
            args += ["-L", f"{name}:{name}.geojson"]
        env = {**os.environ, "PATH": os.path.dirname(TIPPECANOE) + os.pathsep + os.environ["PATH"]} if os.sep in TIPPECANOE else None
        subprocess.run(args, check=True, cwd=tmp, env=env)
        shutil.copy(os.path.join(tmp, "qa.pmtiles"), os.path.join(OUT, "qa.pmtiles"))


TIPPECANOE = os.environ.get("TIPPECANOE", "tippecanoe")


def colorize(values, cmap, vmin, vmax, shade=None, alpha_mask=None):
    norm = np.clip((values - vmin) / max(vmax - vmin, 1e-9), 0, 1)
    rgba = (colormaps[cmap](np.nan_to_num(norm)) * 255).astype(np.uint8)
    if shade is not None:                       # blend colour with hillshade
        rgba[..., :3] = (rgba[..., :3] * (0.35 + 0.65 * shade[..., None])).astype(np.uint8)
    valid = ~np.isnan(values) if alpha_mask is None else alpha_mask
    rgba[..., 3] = np.where(valid, 255, 0)
    return rgba


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("las")
    ap.add_argument("--aoi")
    ap.add_argument("--utm", default="EPSG:3740", help="Projected CRS in metres for the rasters")
    ap.add_argument("--cell", type=float, default=1.0)
    ap.add_argument("--min-density", type=float, default=2.0)
    a = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    utm = pyproj.CRS.from_user_input(a.utm)
    p = load_points(a.las, utm)
    meta = {"points": write_points(p, utm), "layers": {}}
    meta["points"]["tileset"] = write_3dtiles(p, utm)

    pad = 2 * a.cell
    bounds = (p["e"].min() - pad, p["n"].min() - pad, p["e"].max() + pad, p["n"].max() + pad)
    dsm, t = grid_surface(p["e"], p["n"], p["z"], a.cell, "max", bounds)
    footprint = ndimage.binary_fill_holes(ndimage.binary_closing(~np.isnan(dsm), iterations=3))
    ground = p["cls"] == 2
    dtm, _ = grid_surface(p["e"][ground], p["n"][ground], p["z"][ground], a.cell, "mean", bounds)
    dsm, dtm = fill(dsm, footprint), fill(dtm, footprint)
    dtm = ndimage.gaussian_filter(np.nan_to_num(dtm, nan=np.nanmean(dtm)), 1.0)
    dtm[~footprint] = np.nan
    ndsm = np.clip(dsm - dtm, 0, None)

    write_cog(dtm, t, utm, os.path.join(OUT, "dtm_cog.tif"))
    write_cog(dsm, t, utm, os.path.join(OUT, "dsm_cog.tif"))

    zlo, zhi = np.nanpercentile(dtm, 1), np.nanpercentile(dtm, 99)
    layers = {
        "dtm": (colorize(dtm, "terrain", zlo, zhi, hillshade(dtm, a.cell, exaggeration=3)),
                "Bare earth (DTM) hillshade", f"{zlo:.0f} to {zhi:.0f} m elevation"),
        "dsm": (colorize(hillshade(dsm, a.cell), "gray", 0, 1, alpha_mask=footprint),
                "Surface (DSM) hillshade", "buildings, trees and ground"),
        "ndsm": (colorize(np.where(ndsm < 1.0, np.nan, ndsm), "viridis", 1, float(np.nanpercentile(ndsm, 99))),
                 "Height above ground", f"1 to {np.nanpercentile(ndsm, 99):.0f} m"),
    }
    for key, (rgba, title, legend) in layers.items():
        meta["layers"][key] = {"title": title, "legend": legend, "cog": f"{key}.tif",
                               "corners": write_view_cog(rgba, t, utm, key)}

    # QA layers from lidar-qa-toolkit, run on the original file
    qa = analyse(a.las, cell_m=2.0, aoi_path=a.aoi)
    gaps = gap_polygons(qa, a.min_density, 16)
    checks = run_checks(qa, a.min_density, 95, 1.0, gaps)
    with tempfile.TemporaryDirectory() as tmp:
        summary = write_report(qa, checks, gaps, a.min_density, tmp)
        shutil.copy(os.path.join(tmp, "gaps.geojson"), os.path.join(OUT, "gaps.geojson"))
    d = np.where(qa.counts > 0, qa.density, np.nan)
    dens_rgba = colorize(d, "viridis", 0, a.min_density * 2)
    low = qa.analysis_mask & (qa.counts > 0) & (qa.density < a.min_density)
    dens_rgba[low] = [228, 87, 46, 255]
    meta["layers"]["density"] = {"title": "Point density (QA)",
                                 "legend": f"0 to {a.min_density * 2:g} pts/m², red below {a.min_density:g}",
                                 "cog": "density.tif",
                                 "corners": write_view_cog(dens_rgba, qa.grid.transform, qa.crs, "density")}

    # Vector tiles: contours, QA voids and low density areas, and the planned survey area
    with tempfile.TemporaryDirectory() as tmp:
        vt = {"contours": os.path.join(tmp, "contours.geojson"), "gaps": os.path.join(OUT, "gaps.geojson")}
        n = write_contours(dtm, t, utm, 1.0, vt["contours"])
        if a.aoi:
            import geopandas as gpd
            vt["aoi"] = os.path.join(tmp, "aoi.geojson")
            gpd.read_file(a.aoi).to_crs(4326).to_file(vt["aoi"], driver="GeoJSON")
        write_vector_tiles(vt)
    meta["vector_tiles"] = {"file": "qa.pmtiles", "layers": list(vt), "contour_interval_m": 1.0, "contours": n}
    with open(os.path.join(OUT, "qa_summary.json"), "w") as f:
        json.dump({"checks": summary["checks"], "density": summary["density_stats_pts_per_m2"],
                   "min_density": a.min_density, "gaps": summary["gaps"]}, f, indent=1, default=str)

    meta["source"] = {"file": os.path.basename(a.las), "crs": p["src"].name, "raster_crs": utm.name,
                      "cell_m": a.cell}
    with open(os.path.join(OUT, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1)
    print(json.dumps({k: v for k, v in meta.items() if k != "layers"}, indent=1))
    print("layers:", list(meta["layers"]))


if __name__ == "__main__":
    main()
