# OSM to GeoJSON

The compiler accepts a GeoJSON `FeatureCollection`; it does not query or
convert OpenStreetMap data itself. To prepare an extract:

1. Open [Overpass Turbo](https://overpass-turbo.eu/), replace the example place
with the area you need, and run this query:

   ```overpass
   [out:json][timeout:60];
   {{geocodeArea:Knoxville, Tennessee}}->.searchArea;
   (
     way["building"](area.searchArea);
     relation["building"](area.searchArea);
     way["highway"](area.searchArea);
     way["railway"](area.searchArea);
   );
   out body;
   >;
   out skel qt;
   ```

2. Export the raw Overpass JSON as `extract.osm.json`.
3. Convert it with `osmtogeojson`:

   ```sh
   npx osmtogeojson extract.osm.json > converted-map.geojson
   ```

4. Run the compiler on that GeoJSON as shown in the project quick-start.

Building footprints require the `building` tag. `building:levels` is optional;
when present, positive numeric values and semicolon-separated values are
supported. The compiler uses `highway` and `railway` ways for the road network
and building frontage. Preserve relevant OSM properties such as `bridge`,
`tunnel`, and `layer` for grade separation, along with useful building identity
tags such as `id`, `@id`, or `osm_id` when available. Features still pass
through the normal geometry validation and generation-eligibility stages.