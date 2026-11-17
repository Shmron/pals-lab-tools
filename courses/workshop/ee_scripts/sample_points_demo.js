/**
 * sample_points_demo.js
 * Google Earth Engine Code Editor — sample ERA5-Land temperature at village points
 *
 * Paste this into https://code.earthengine.google.com and click Run.
 * To use your own village CSV:
 *   1. Assets panel → New → Table upload → upload villages.csv
 *   2. Replace the FeatureCollection below with:
 *      var villages = ee.FeatureCollection('users/YOUR_USERNAME/villages');
 */

// ── 1. Define village point locations ──────────────────────────────────────
var villages = ee.FeatureCollection([
  ee.Feature(ee.Geometry.Point([27.80, -20.80]), {village: 'Riverside'}),
  ee.Feature(ee.Geometry.Point([28.10, -21.10]), {village: 'Hilltop'}),
  ee.Feature(ee.Geometry.Point([29.40, -19.20]), {village: 'Greenfield'}),
  ee.Feature(ee.Geometry.Point([30.00, -18.90]), {village: 'Ironstone'}),
  ee.Feature(ee.Geometry.Point([31.20, -17.80]), {village: 'Sunridge'}),
  ee.Feature(ee.Geometry.Point([30.90, -17.50]), {village: 'Westgate'}),
  ee.Feature(ee.Geometry.Point([32.10, -19.00]), {village: 'Eastview'}),
  ee.Feature(ee.Geometry.Point([31.80, -19.40]), {village: 'Ferndale'}),
  ee.Feature(ee.Geometry.Point([28.60, -20.10]), {village: 'Kopje'}),
  ee.Feature(ee.Geometry.Point([28.90, -20.40]), {village: 'Ntabazinduna'})
]);

// ── 2. Load ERA5-Land daily aggregates, filter to date range ───────────────
var era5 = ee.ImageCollection('ECMWF/ERA5_LAND/DAILY_AGGR')
  .filterDate('2023-01-01', '2024-07-31')
  .select(['temperature_2m_max', 'temperature_2m_min']);

// ── 3. Convert Kelvin to Celsius ───────────────────────────────────────────
var era5_c = era5.map(function(img) {
  return img
    .subtract(273.15)
    .rename(['temp_max_c', 'temp_min_c'])
    .copyProperties(img, ['system:time_start']);
});

// ── 4. For each image, sample at all village points ────────────────────────
var sampled = era5_c.map(function(img) {
  var date_str = ee.Date(img.get('system:time_start')).format('YYYY-MM-dd');
  return img
    .reduceRegions({
      collection: villages,
      reducer: ee.Reducer.first(),
      scale: 11132    // ERA5-Land native resolution ~11 km
    })
    .map(function(f) {
      return f.set('date', date_str);
    });
}).flatten();

// ── 5. Preview in the Console panel ───────────────────────────────────────
print('Total sample rows:', sampled.size());
print('First 5 rows:', sampled.limit(5));

// ── 6. Export to Google Drive (uncomment to run as a batch Task) ───────────
// Export.table.toDrive({
//   collection: sampled,
//   description: 'village_era5_temperature',
//   fileFormat: 'CSV',
//   selectors: ['village', 'date', 'temp_max_c', 'temp_min_c', '.geo']
// });

// ── Notes for facilitators ────────────────────────────────────────────────
// - Click the Tasks panel (top-right) after uncommenting Export to submit the job.
// - The job appears in Drive as a CSV when complete (typically 2-5 minutes for this size).
// - For larger date ranges or more points, increase the export description to something
//   descriptive and monitor the Tasks panel for completion.
// - To load from an uploaded asset instead of the hardcoded FeatureCollection:
//     var villages = ee.FeatureCollection('users/YOUR_USERNAME/villages');
