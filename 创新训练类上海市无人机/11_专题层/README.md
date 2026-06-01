# 上海专题层说明

本目录中的文件均从 `01_基础地理数据/shanghai_osm_gpkg/shanghai.gpkg` 拆分导出，可直接在 ArcGIS / QGIS 中打开。

## 文件列表

- `上海_医院.gpkg`
  - 图层名：`hospitals`
  - 要素数：75
- `上海_消防站.gpkg`
  - 图层名：`fire_stations`
  - 要素数：15
- `上海_社区中心.gpkg`
  - 图层名：`community_centres`
  - 要素数：91
- `上海_道路网络.gpkg`
  - 图层名：`roads`
  - 要素数：206936
- `上海_土地利用.gpkg`
  - 图层名：`landuse`
  - 要素数：27199

## 数据来源

- 原始数据源：OpenStreetMap 上海提取包（Geofabrik）
- 原始文件：`01_基础地理数据/shanghai_osm_gpkg/shanghai.gpkg`

## 备注

- 医院、消防站、社区中心来自 `gis_osm_pois_free` 图层的 `fclass` 筛选结果。
- 道路网络来自 `gis_osm_roads_free`。
- 土地利用来自 `gis_osm_landuse_a_free`。
