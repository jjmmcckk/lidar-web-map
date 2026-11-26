// Loading and colouring helpers for the prepared data in public/data

export type Corners = [[number, number], [number, number], [number, number], [number, number]];

export interface RasterLayerMeta {
  title: string;
  legend: string;
  corners: Corners;
}

export interface Meta {
  points: { count: number; origin: [number, number]; z_min_m: number; z_max_m: number };
  layers: Record<string, RasterLayerMeta>;
  source: { file: string; crs: string; raster_crs: string; cell_m: number };
}

export interface QaCheck {
  check: string;
  value: string;
  target: string;
  pass: boolean | null;
}

export interface QaSummary {
  checks: QaCheck[];
  density: { mean: number; median: number; p5: number; p95: number; cells: number; empty_cells: number };
  min_density: number;
  gaps: { area_m2: number; type: string }[];
}

export interface PointData {
  count: number;
  positions: Float32Array;
  rgb: Uint8Array;
  classes: Uint8Array;
}

const base = "data/";

export async function loadMeta(): Promise<Meta> {
  return (await fetch(base + "meta.json")).json();
}

export async function loadQa(): Promise<QaSummary> {
  return (await fetch(base + "qa_summary.json")).json();
}

export async function loadPoints(count: number): Promise<PointData> {
  const [pos, rgb, cls] = await Promise.all(
    ["points_pos.bin", "points_rgb.bin", "points_cls.bin"].map((f) => fetch(base + f).then((r) => r.arrayBuffer()))
  );
  return { count, positions: new Float32Array(pos), rgb: new Uint8Array(rgb), classes: new Uint8Array(cls) };
}

// Viridis colour stops for elevation colouring
const VIRIDIS: [number, number, number][] = [
  [68, 1, 84], [72, 40, 120], [62, 74, 137], [49, 104, 142], [38, 130, 142],
  [31, 158, 137], [53, 183, 121], [109, 205, 89], [180, 222, 44], [253, 231, 37],
];

function ramp(t: number): [number, number, number] {
  const x = Math.min(Math.max(t, 0), 1) * (VIRIDIS.length - 1);
  const i = Math.floor(x);
  const f = x - i;
  const a = VIRIDIS[i];
  const b = VIRIDIS[Math.min(i + 1, VIRIDIS.length - 1)];
  return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f];
}

// ASPRS class colours
export const CLASS_COLORS: Record<number, [number, number, number]> = {
  1: [170, 170, 170], 2: [166, 118, 60], 3: [180, 220, 120], 4: [110, 190, 80], 5: [40, 130, 50],
  6: [220, 80, 60], 7: [255, 0, 255], 9: [60, 120, 220], 17: [240, 200, 60], 18: [255, 0, 255],
};
export const CLASS_NAMES: Record<number, string> = {
  1: "Unclassified", 2: "Ground", 3: "Low vegetation", 4: "Medium vegetation", 5: "High vegetation",
  6: "Building", 7: "Low noise", 9: "Water", 17: "Bridge deck", 18: "High noise",
};

export type ColorMode = "rgb" | "elevation" | "class";

export function pointColors(p: PointData, mode: ColorMode): Uint8Array {
  if (mode === "rgb") return p.rgb;
  const out = new Uint8Array(p.count * 3);
  if (mode === "elevation") {
    let zmin = Infinity, zmax = -Infinity;
    for (let i = 0; i < p.count; i++) {
      const z = p.positions[i * 3 + 2];
      if (z < zmin) zmin = z;
      if (z > zmax) zmax = z;
    }
    for (let i = 0; i < p.count; i++) {
      const c = ramp((p.positions[i * 3 + 2] - zmin) / (zmax - zmin || 1));
      out[i * 3] = c[0]; out[i * 3 + 1] = c[1]; out[i * 3 + 2] = c[2];
    }
  } else {
    for (let i = 0; i < p.count; i++) {
      const c = CLASS_COLORS[p.classes[i]] ?? [255, 255, 255];
      out[i * 3] = c[0]; out[i * 3 + 1] = c[1]; out[i * 3 + 2] = c[2];
    }
  }
  return out;
}

export function presentClasses(p: PointData): number[] {
  const seen = new Set<number>();
  for (let i = 0; i < p.count; i++) seen.add(p.classes[i]);
  return [...seen].sort((a, b) => a - b);
}
