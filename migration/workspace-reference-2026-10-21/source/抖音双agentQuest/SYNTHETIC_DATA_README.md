# Synthetic Mock Data — Douyin Business Cockpit

## ⚠️ SYNTHETIC DATA WARNING
All data in the `workspace.douyin` tables listed below is **100% synthetic mock data**, generated for testing and development purposes. It does NOT represent real business data.

All tables are marked with the following TBLPROPERTIES:
- `data_origin = 'synthetic_mock'`
- `generated_by = 'genie_code'`
- `generated_date = '2025-10-12'`
- `synthetic_data = 'true'`

## Data Source
Mock data was generated from the Power BI template file `Douyin Business Cockpit - Mock Data.pbit`, which contained table schemas, relationships, and parameter values (but no embedded row data). The data was generated to match the schema and relationships defined in both the Excel data dictionary (`DF数据字典.xlsx`) and the Power BI model.

## 17 MVP Tables (A1/A2 Genie Agents)

### DIM Layer (7 tables)
| Table | Rows | Description |
| --- | --- | --- |
| dim_haleon_brand_adhoc_all | 10 | Haleon brand dimension (Sensodyne, Caltrate, Centrum, etc.) |
| dim_channel_adhoc_all | 5 | Douyin channel types |
| dim_dy_biz_model_adhoc_all | 4 | Business models (自播/达播/短视频/商品卡) |
| dim_chanmama_biz_model_adhoc_all | 4 | Chanmama business models |
| dim_store_adhoc_all | 16 | Store dimension (8 brands × 2 stores) |
| dim_account_adhoc_all | 32 | Account dimension (2 accounts per store) |
| dim_product_day_ups | 12 | Product dimension |

### RPT Layer (10 tables)
| Table | Rows | Description |
| --- | --- | --- |
| rpt_dy_sku_sales_day_ups | 5,760 | Daily SKU sales (Jan–Jun 2025, partitioned by ds) |
| rpt_dy_self_live_channel_day_ups | 480 | Self-livestream channel metrics (partitioned by ds) |
| rpt_dy_gmv_target_year_ups | 24 | Annual GMV targets (partitioned by ds) |
| rpt_market_sales_top_brand_month_all | 180 | Monthly brand market rankings |
| rpt_market_sales_top_brand_productline_month_all | 72 | Monthly product line rankings |
| rpt_dy_consumer_5a_asset_day_ups | 36 | 5A consumer asset snapshots |
| rpt_dy_compete_commerce_day_ups | 260 | Competition commerce data (partitioned by ds) |
| rpt_dy_compete_top_product_link_day_ups | 468 | Top product links (partitioned by ds) |
| rpt_dy_compete_campaign_day_ups | 30 | Campaign data (partitioned by ds) |
| rpt_chanmama_brand_aweme_detail_day_ups | 150 | Video/content detail (partitioned by ds) |

**Total: 7,543 rows across 17 tables**

## Referential Integrity
All 16 referential integrity checks pass:
- rpt_dy_sku_sales_day_ups → dim_store, dim_account, dim_dy_biz_model, dim_product
- rpt_dy_self_live_channel_day_ups → dim_store, dim_account
- dim_store → dim_haleon_brand, dim_channel
- rpt_dy_gmv_target_year_ups → dim_channel, dim_haleon_brand
- rpt_market_sales_* → dim_haleon_brand
- rpt_dy_consumer_5a_asset → dim_haleon_brand
- rpt_dy_compete_* → dim_haleon_brand
- dim_account → dim_store

## Report Analysis Coverage (10 pages)
1. **总览 (Overview)** — GMV by business model, YoY growth ✓
2. **自播 (Self-Live)** — Live streaming KPIs, GPM, conversion ✓
3. **商品 (Product)** — Product rankings, SKU performance ✓
4. **竞争-Overview** — Competition GMV summary ✓
5. **竞争-Market** — Brand market share, rankings ✓
6. **竞争-Consumer** — 5A consumer asset breakdown ✓
7. **竞争-Commerce** — Competition commerce by biz model ✓
8. **竞争-Content** — Video/content analysis by type ✓
9. **竞争-Category** — Product line market share ✓
10. **竞争-Campaign** — Campaign analysis ✓

## Date Range
- Daily tables: Jan 1 – Jun 30, 2025 (sampled every 3rd day)
- Monthly tables: Jan – Jun 2025
- Yearly tables: 2025

## Generation Method
- Random seed: 42 (reproducible)
- Brand names: Real Haleon portfolio (Sensodyne, Caltrate, Centrum, Advil, Voltaren, etc.)
- Competitor brands: Real market competitors (云南白药, 冷酸灵, 斯维诗, 汤臣倍健, etc.)
- Metrics: Realistic ranges for GMV, views, engagement, market share