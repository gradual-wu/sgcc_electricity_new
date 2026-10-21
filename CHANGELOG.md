# Changelog

## [v1.15.1] - 2026-10-21

### Documentation

- 为 `config.yaml`、`example.env` 的每个配置项补充中文注释。
- 新增 HA 加载项 `translations/en.yaml`、`translations/zh-Hans.yaml`，配置界面逐项显示名称和说明。
- README / FLOW 补充配置参考入口。

## [v1.15.0] - 2026-10-21

### Added

- 新增 `FILL_MISSING_DATA`：扫描数据库，只补缺失的日数据、官方账单、交费记录和阶梯快照，不重拉已有数据。
- 启动时如果 `FILL_MISSING_DATA=true` 会立即执行补缺，不必等到定时任务。
- 日数据按缺口日期段抓取；缺失的 calculated 月度行由已有日数据补算，并刷新年度汇总。
- `FORCE_FULL_REFRESH` 仍保留为真正的全量重拉；两种模式跑完都建议改回 `false`。
- `db_status.py --missing` 可只读扫描数据库，输出缺失日期段、缺失官方账单、交费/阶梯状态。

## [v1.14.0] - 2026-10-21

### Added

- 新增 `FORCE_FULL_REFRESH`：一次性强制刷新全年日数据、官方账单、交费记录和阶梯快照；完成后请设回 `false`。
- 全年回补默认分段从 90 天改为 30 天，降低单次查询漏天数概率。
- 回补结束后会对比数据库实际日期，自动找出缺失日期段并单独补齐。
- 回补完成后同步当月月度行和年度汇总，避免出现“有日数据但没有 10 月月度/年度”的情况。

## [v1.13.0] - 2026-10-21

### Added

- 二维码登录时识别二维码内容，并本地重新生成干净二维码，保存到 `HASS_WWW_DIR`（默认 `/config/www`）。
- HA 持久通知直接显示 `/local/sgcc_login_qr.png`，并附带 data URI 兜底；二维码内容不发送到第三方。
- 新增 `HASS_WWW_DIR` 加载项配置。

## [v1.12.0] - 2026-10-21

### Changed

- 新增 `ENTITY_ID_STYLE`，默认 `none`：实体 ID 不再带户号后缀，例如 `sensor.last_electricity_usage`。
- 可选 `full`（完整户号，多户号唯一）或 `last4`（旧版后 4 位）。
- 从 `last4` 切换时，`CLEANUP_LEGACY_ENTITY_IDS=true` 会自动删除旧的后 4 位 MQTT Discovery 配置。
- 每个户号仍然是独立的 HA 设备，设备标识和 `serial_number` 使用完整户号。

## [v1.11.0] - 2026-10-21

### Added

- 读取交费记录并保存到 MySQL `sg_payments`，推送上次缴费时间和金额到 HA。
- 每月 1 号（以及账单缺失时）查询上个月官方电费账单，保存本月电量、本月电费、分时电量和阶梯字段到 `sg_monthly_bill`。
- 读取“阶梯用电”页面并保存官方阶梯状态到 `sg_tier_snapshot`。
- 新增当前阶梯实体，按历史月份账单 + 当月实时日用电量动态计算（默认 0-2160 / 2161-4200 / 4201 以上）。
- 首次运行按 90 天分段回补当年日用电量，并补查当年缺失的官方账单；`DATA_RETENTION_DAYS=0` 时永久保留。
- 抓取与 HA 发布解耦：日常只核对最近 7 天，HA 统一从 `sg_*` 表读取并发布；新增 `scripts/db_status.py` 查看/修复数据库和从数据库发布 HA，不访问国网。
- 新增 `sg_users.data_updated_at` 和 `sensor.data_updated_at_xxxx`，记录并推送每个户号最后一次成功写库时间。

## [v1.6.8][v1.6.8] - 2025-07-10

### 更换默认浏览器

- 更换默认浏览器，减小系统空间
- 修复v1.6.7出现不能加载网页的情况

## [v1.6.7][v1.6.7] - 2025-07-06

### 添加随机延迟执行

- 默认时间为07:00,19:00 现在改为这个时间随机加减10分钟，避免同时访问....

## [v1.6.4][v1.6.4] - 2025-01-06

### Fixed

- add-on功能优化.

## [v1.6.3][v1.6.3] - 2025-01-05

### Added

- 初步测试了addon功能.

### Fixed

- 修复了一些小bug.

[v1.6.4]: https://github.com/ARC-MX/sgcc_electricity_new/compare/v1.6.3...v1.6.4
[v1.6.3]: https://github.com/ARC-MX/sgcc_electricity_new/releases/tag/v1.6.3
