import fs from "node:fs";
import path from "node:path";
import { topology } from "topojson-server";
import { presimplify, quantile, simplify } from "topojson-simplify";
import { mesh, quantize } from "topojson-client";
import { geoArea, geoCentroid } from "d3-geo";

const [tractPath, countyPath, outputPath] = process.argv.slice(2);
if (!tractPath || !countyPath || !outputPath) throw new Error("usage: build-topologies tract.geojson county.geojson output-dir");

const read = (file) => JSON.parse(fs.readFileSync(file, "utf8"));
const write = (name, value) => fs.writeFileSync(path.join(outputPath, `${name}.topojson`), JSON.stringify(value));
const collection = (features) => ({ type: "FeatureCollection", features });
const sorted = (features) => features.sort((a, b) => String(a.id).localeCompare(String(b.id)));
const make = (features, quantization = 100000) => topology({ geography: collection(sorted(features)) }, quantization);
const stripWeights = (value) => ({ ...value, arcs: value.arcs.map((arc) => arc.map((point) => point.slice(0, 2))) });
const simplified = (value, percentile, quantization) => quantize(
  stripWeights(simplify(value, quantile(value, percentile))),
  quantization,
);
const normalizePolygon = (rings) => geoArea({ type: "Polygon", coordinates: rings }) > 2 * Math.PI
  ? rings.map((ring) => [...ring].reverse())
  : rings;
const normalizeGeometry = (geometry) => geometry.type === "Polygon"
  ? { ...geometry, coordinates: normalizePolygon(geometry.coordinates) }
  : { ...geometry, coordinates: geometry.coordinates.map(normalizePolygon) };

fs.mkdirSync(outputPath, { recursive: true });
const normalizeFeature = (feature) => ({ ...feature, geometry: normalizeGeometry(feature.geometry) });
const tracts = read(tractPath).features.map(normalizeFeature);
const counties = read(countyPath).features.map(normalizeFeature);

const tractNational = presimplify(make(tracts, 16000));
write("tracts-national", simplified(tractNational, 0.99, 16000));
const countyNational = presimplify(make(counties, 30000));
write("counties-national", simplified(countyNational, 0.65, 30000));

const countyTopology = make(counties, 140000);
const states = [...new Set(counties.map((feature) => feature.properties.state))].sort();
const stateFeatures = states.map((state) => ({
  type: "Feature",
  id: state,
  properties: {
    state,
    name: counties.find((feature) => feature.properties.state === state)?.properties.state_name || state,
    label: geoCentroid(collection(counties.filter((feature) => feature.properties.state === state))),
  },
  geometry: mesh(countyTopology, countyTopology.objects.geography, (a, b) => {
    const left = a?.properties?.state;
    const right = b?.properties?.state;
    return (left === state && (a === b || right !== state)) || (right === state && left !== state);
  }),
}));
const stateNational = presimplify(make(stateFeatures, 30000));
write("states-national", simplified(stateNational, 0.80, 16000));

const tractsByState = new Map();
for (const tract of tracts) {
  const state = tract.properties.state;
  const group = tractsByState.get(state) || [];
  group.push(tract);
  tractsByState.set(state, group);
}
for (const state of [...tractsByState.keys()].sort()) {
  write(`tracts-${state.toLowerCase()}`, make(tractsByState.get(state), 180000));
}
