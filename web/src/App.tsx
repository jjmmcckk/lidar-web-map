import { useEffect, useMemo, useRef, useState } from "react";
import maplibregl, { Map as MlMap, StyleSpecification } from "maplibre-gl";
import { MapboxOverlay } from "@deck.gl/mapbox";
import { PointCloudLayer } from "@deck.gl/layers";
import { COORDINATE_SYSTEM } from "@deck.gl/core";
import {
  CLASS_COLORS, CLASS_NAMES, ColorMode, Meta, PointData, QaSummary,
  loadMeta, loadPoints, loadQa, pointColors, presentClasses,
} from "./data";

type LayerKey = "points" | "dtm" | "dsm" | "ndsm" | "density" | "gaps" | "aoi";

const RASTERS = ["dtm", "dsm", "ndsm", "density"] as const;
const RASTER_OPACITY: Record<string, number> = { dtm: 0.95, dsm: 0.85, ndsm: 0.9, density: 0.85 };

const BASE_STYLE: StyleSpecification = {
  version: 8,
  sources: {
    osm: {
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      maxzoom: 19,
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    },
  },
  layers: [
    { id: "bg", type: "background", paint: { "background-color": "#e6e9ee" } },
    { id: "osm", type: "raster", source: "osm", paint: { "raster-saturation": -0.4 } },
  ],
};

const LAYER_LABELS: Record<LayerKey, string> = {
  points: "LiDAR point cloud",
  dtm: "Bare earth (DTM) hillshade",
  dsm: "Surface (DSM) hillshade",
  ndsm: "Height above ground",
  density: "Point density (QA)",
  gaps: "Voids and low density (QA)",
  aoi: "Planned survey area",
};

export default function App() {
  const mapDiv = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MlMap | null>(null);
  const overlayRef = useRef<MapboxOverlay | null>(null);
  const fitRef = useRef<((pitch: number, bearing: number, duration: number) => void) | null>(null);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [qa, setQa] = useState<QaSummary | null>(null);
  const [points, setPoints] = useState<PointData | null>(null);
  const [mapReady, setMapReady] = useState(false);
  const [panelOpen, setPanelOpen] = useState(true);
  const [is3d, setIs3d] = useState(true);
  const [colorMode, setColorMode] = useState<ColorMode>("rgb");
  const [pointSize, setPointSize] = useState(2);
  const [zScale, setZScale] = useState(1.5);
  const [visible, setVisible] = useState<Record<LayerKey, boolean>>({
    points: true, dtm: false, dsm: true, ndsm: false, density: false, gaps: true, aoi: true,
  });

  // Load data
  useEffect(() => {
    loadMeta().then((m) => {
      setMeta(m);
      loadPoints(m.points.count).then(setPoints);
    });
    loadQa().then(setQa);
  }, []);

  // Create the map once metadata is available
  useEffect(() => {
    if (!meta || !mapDiv.current || mapRef.current) return;
    const c = meta.layers.dtm.corners;
    const lons = c.map((p) => p[0]), lats = c.map((p) => p[1]);
    const map = new maplibregl.Map({
      container: mapDiv.current,
      style: BASE_STYLE,
      center: [(Math.min(...lons) + Math.max(...lons)) / 2, (Math.min(...lats) + Math.max(...lats)) / 2],
      zoom: 16,
      maxPitch: 75,
      attributionControl: { compact: true, customAttribution: "Point cloud: Autzen sample, PDAL project" },
    });
    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");

    const fitData = (pitch: number, bearing: number, duration: number) => {
      const wide = window.innerWidth > 700;
      map.fitBounds([[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]], {
        padding: wide ? { top: 60, bottom: 60, left: 380, right: 60 } : { top: 60, bottom: window.innerHeight * 0.5, left: 20, right: 20 },
        pitch, bearing, duration,
      });
    };
    fitRef.current = fitData;
    map.on("load", () => {
      fitData(50, -20, 0);
      for (const key of RASTERS) {
        const l = meta.layers[key];
        map.addSource(key, { type: "image", url: `data/${key}.png`, coordinates: l.corners });
        map.addLayer({ id: key, type: "raster", source: key, paint: { "raster-opacity": RASTER_OPACITY[key], "raster-resampling": "nearest" } });
      }
      map.addSource("gaps", { type: "geojson", data: "data/gaps.geojson" });
      map.addLayer({
        id: "gaps", type: "fill", source: "gaps",
        paint: {
          "fill-color": ["match", ["get", "type"], "void", "#3b3b3b", "#e4572e"],
          "fill-opacity": 0.75, "fill-outline-color": "#ffffff",
        },
      });
      map.addSource("aoi", { type: "geojson", data: "data/aoi.geojson" });
      map.addLayer({ id: "aoi", type: "line", source: "aoi", paint: { "line-color": "#00b4d8", "line-width": 2.5, "line-dasharray": [2, 1] } });

      const overlay = new MapboxOverlay({ interleaved: false, layers: [] });
      map.addControl(overlay);
      overlayRef.current = overlay;
      setMapReady(true);
    });
    mapRef.current = map;
    return () => { map.remove(); mapRef.current = null; };
  }, [meta]);

  // Map layer visibility
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;
    for (const key of [...RASTERS, "gaps", "aoi"] as LayerKey[]) {
      map.setLayoutProperty(key, "visibility", visible[key] ? "visible" : "none");
    }
  }, [visible, mapReady]);

  // 2D and 3D view
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;
    fitRef.current?.(is3d ? 50 : 0, is3d ? -20 : 0, 800);
  }, [is3d, mapReady]);

  // Point cloud layer
  const colors = useMemo(() => (points ? pointColors(points, colorMode) : null), [points, colorMode]);
  useEffect(() => {
    const overlay = overlayRef.current;
    if (!overlay || !meta || !points || !colors) return;
    overlay.setProps({
      layers: [
        new PointCloudLayer({
          id: "points",
          visible: visible.points,
          data: {
            length: points.count,
            attributes: {
              getPosition: { value: points.positions, size: 3 },
              getColor: { value: colors, size: 3, normalized: true },
            },
          },
          coordinateSystem: COORDINATE_SYSTEM.METER_OFFSETS,
          coordinateOrigin: [meta.points.origin[0], meta.points.origin[1], 0],
          modelMatrix: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, is3d ? zScale : 0.0001, 0, 0, 0, 0, 1],
          pointSize,
          sizeUnits: "pixels",
          material: false,
          updateTriggers: { getColor: colorMode },
        }),
      ],
    });
  }, [meta, points, colors, visible.points, pointSize, zScale, is3d, colorMode, mapReady]);

  const classes = useMemo(() => (points ? presentClasses(points) : []), [points]);
  const toggle = (k: LayerKey) => setVisible((v) => ({ ...v, [k]: !v[k] }));
  const failed = qa?.checks.filter((c) => c.pass === false).length ?? 0;

  return (
    <div className="app">
      <div ref={mapDiv} className="map" />
      <button className={`panel-toggle ${panelOpen ? "shifted" : ""}`} onClick={() => setPanelOpen((o) => !o)} aria-expanded={panelOpen}>
        {panelOpen ? "Hide panel" : "Layers and QA"}
      </button>
      <aside className={`panel ${panelOpen ? "open" : ""}`}>
        <header>
          <h1>LiDAR Web Map</h1>
          <p className="sub">
            Point cloud, terrain models and QA results from one LAS file, prepared in Python and drawn with
            MapLibre and deck.gl.
          </p>
        </header>

        <section>
          <h2>View</h2>
          <div className="seg">
            <button className={is3d ? "on" : ""} onClick={() => setIs3d(true)}>3D</button>
            <button className={!is3d ? "on" : ""} onClick={() => setIs3d(false)}>2D</button>
          </div>
        </section>

        <section>
          <h2>Layers</h2>
          {(Object.keys(LAYER_LABELS) as LayerKey[]).map((k) => (
            <label key={k} className="layer">
              <input type="checkbox" checked={visible[k]} onChange={() => toggle(k)} />
              <span>
                {LAYER_LABELS[k]}
                {meta && (RASTERS as readonly string[]).includes(k) && (
                  <small>{meta.layers[k].legend}</small>
                )}
                {k === "gaps" && <small><i className="sw void" /> void <i className="sw low" /> below target</small>}
              </span>
            </label>
          ))}
        </section>

        <section>
          <h2>Point cloud</h2>
          <div className="seg">
            {(["rgb", "elevation", "class"] as ColorMode[]).map((m) => (
              <button key={m} className={colorMode === m ? "on" : ""} onClick={() => setColorMode(m)}>
                {m === "rgb" ? "Colour" : m === "elevation" ? "Elevation" : "Class"}
              </button>
            ))}
          </div>
          {colorMode === "class" && (
            <div className="classes">
              {classes.map((c) => (
                <span key={c}><i className="sw" style={{ background: `rgb(${(CLASS_COLORS[c] ?? [255, 255, 255]).join(",")})` }} />{CLASS_NAMES[c] ?? `Class ${c}`}</span>
              ))}
            </div>
          )}
          {colorMode === "elevation" && meta && (
            <div className="ramp"><span>{meta.points.z_min_m.toFixed(0)} m</span><i /><span>{meta.points.z_max_m.toFixed(0)} m</span></div>
          )}
          <label className="slider">Point size {pointSize}px
            <input type="range" min={1} max={5} step={1} value={pointSize} onChange={(e) => setPointSize(+e.target.value)} />
          </label>
          <label className="slider">Vertical scale {zScale.toFixed(1)}x
            <input type="range" min={1} max={4} step={0.5} value={zScale} onChange={(e) => setZScale(+e.target.value)} disabled={!is3d} />
          </label>
          {points && <p className="note">{points.count.toLocaleString()} points</p>}
        </section>

        {qa && (
          <section>
            <h2>QA results <span className={failed ? "badge fail" : "badge pass"}>{failed ? `${failed} failed` : "all passed"}</span></h2>
            <table>
              <tbody>
                {qa.checks.map((c) => (
                  <tr key={c.check}>
                    <td>{c.check}</td>
                    <td>{c.value}</td>
                    <td><span className={`badge ${c.pass === null ? "info" : c.pass ? "pass" : "fail"}`}>{c.pass === null ? "info" : c.pass ? "pass" : "fail"}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="note">
              Produced with <a href="https://github.com/jjmmcckk/lidar-qa-toolkit">lidar-qa-toolkit</a>: median density{" "}
              {qa.density.median} pts/m² against a {qa.min_density} pts/m² target.
            </p>
          </section>
        )}

        <section>
          <h2>Downloads</h2>
          <ul className="downloads">
            <li><a href="data/dtm_cog.tif" download>Bare earth DTM, Cloud Optimized GeoTIFF</a></li>
            <li><a href="data/dsm_cog.tif" download>Surface DSM, Cloud Optimized GeoTIFF</a></li>
            <li><a href="data/gaps.geojson" download>Void and low density polygons, GeoJSON</a></li>
          </ul>
          {meta && <p className="note">Rasters: {meta.source.cell_m} m cells in {meta.source.raster_crs}.</p>}
        </section>

        <footer>
          <p>
            Point cloud: Autzen sample from the{" "}
            <a href="https://github.com/PDAL/PDAL/tree/master/test/data/las">PDAL project test data</a>. Basemap ©{" "}
            <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors.
          </p>
          <p>
            Built by Jordan McKenzie · <a href="https://github.com/jjmmcckk/lidar-web-map">source code</a>
          </p>
        </footer>
      </aside>
    </div>
  );
}
