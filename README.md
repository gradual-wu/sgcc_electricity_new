# ⚡️ 国家电网电力获取 · HA Add-on 版

[![Docker Image CI](https://github.com/gradual-wu/sgcc_electricity_new/actions/workflows/docker-image.yml/badge.svg)](https://github.com/gradual-wu/sgcc_electricity_new/actions/workflows/docker-image.yml)

> 本仓库 Fork 自 [ARC-MX/sgcc_electricity_new](https://github.com/ARC-MX/sgcc_electricity_new)，在其基础上针对 **Home Assistant Add-on** 使用场景做了专项修复，在此感谢原作者的贡献。
>
> ⚠️ **重要**：HA Add-on 默认拉取 `config.yaml` 里的 `image`。本分支已改为
> `ghcr.io/gradual-wu/sgcc_electricity-{arch}`；如果你修改了代码但没有重新构建/推送镜像，
> 加载项仍然会运行旧镜像。下面「本地运行」和「发布自己的镜像」两节说明如何让修改真正生效。

<p align="center">
<img src="assets/image-20230730135540291.png" alt="mini-graph-card" width="400">
<img src="assets/image-20240514.jpg" alt="mini-graph-card" width="400">
</p>

---

## 此分支修复内容

| 问题 | 修复 |
|------|------|
| Chrome 150 在 HA OS 容器中崩溃（`session not created`） | 固定使用 Chrome for Testing **120.0.6099.109**（仅 amd64），彻底解决崩溃 |
| 腾讯风控 RK001 登录被拦截 | CDP 注入反指纹脚本，伪装为 Windows Chrome 136，绕过容器指纹检测 |
| 验证码控件检测到隐藏的预加载 DOM 元素 | 改用 `visibility_of_element_located` 替代 `presence_of_element_located` |
| 风控触发时自动切换二维码登录 | 检测到 RK001 → 自动切换二维码模式，通过 HA 持久通知展示二维码 |
| 二维码无法送达（PushPlus 需要实名认证） | 改用 HA 自带的通知系统：图片保存至 `/config/www/`，HA 界面直接显示 |
| GitHub Actions 推送镜像权限不足 | 使用带 `packages: write` 的 `GITHUB_TOKEN`；组织策略不允许时改用 `CR_PAT` |
| 旧版通过 HA REST API 逐个写 sensor | 改用 **MQTT Discovery**，自动创建带设备分组的实体 |
| 峰谷电量只在日志里看到 | 新增日/月谷、平、峰、尖分时实体，并写入 MySQL |
| MySQL 每户一套 `daily{户号}` 表 | 改为 `sg_users` / `sg_daily_usage` / `sg_monthly_usage` / `sg_yearly_usage` / `sg_balance_log` 业务表，默认永久保留 |

---

## 简介

通过 Python Selenium 自动化获取国家电网官网的电费、电量数据，接入 Home Assistant 实体。登录时的腾讯点击/滑块验证码通过**大模型（LLM）视觉识别**自动解算，无需人工干预；若遭遇风控拦截则自动切换二维码登录并通过 **HA 通知**推送二维码图片。

**提供以下 HA 实体（通过 MQTT Discovery 自动创建）：**

| 实体 | 说明 |
|------|------|
| `sensor.last_electricity_usage_xxxx` | 最近一天用电量（kWh） |
| `sensor.electricity_charge_balance_xxxx` | 电费余额 / 上月应交电费（元） |
| `sensor.yearly_electricity_usage_xxxx` | 今年总用电量（kWh） |
| `sensor.yearly_electricity_charge_xxxx` | 今年总电费（元） |
| `sensor.month_electricity_usage_xxxx` | 最近一月用电量（kWh） |
| `sensor.month_electricity_charge_xxxx` | 上月总电费（元） |
| `sensor.last_valley_electricity_usage_xxxx` | 最近一天谷时电量（kWh） |
| `sensor.last_flat_electricity_usage_xxxx` | 最近一天平时电量（kWh） |
| `sensor.last_peak_electricity_usage_xxxx` | 最近一天峰时电量（kWh） |
| `sensor.last_tip_electricity_usage_xxxx` | 最近一天尖时电量（kWh） |
| `sensor.month_valley_usage_xxxx` | 当月谷时用电量（kWh） |
| `sensor.month_flat_usage_xxxx` | 当月平时用电量（kWh） |
| `sensor.month_peak_usage_xxxx` | 当月峰时用电量（kWh） |
| `sensor.month_tip_usage_xxxx` | 当月尖时用电量（kWh） |
| `sensor.prepay_balance_xxxx` | 预付费余额 / 应交金额（元） |

> 原有 6 个实体 ID 与旧版 REST API 创建的实体保持一致，升级后原有仪表盘/自动化无需修改；
> 新增的日/月分时实体沿用 ha-95598 的命名风格。
> 程序首次启动时会自动删除旧版 REST 创建的同名状态（旧实体不在 HA 实体注册表中，
> 不删除会导致 MQTT 实体被重命名为 `*_2`）。
>
> 月度分时电量优先使用官方账单数据；当月账单未出时，用当月已获取的日数据累加，
> 实体属性 `source` 会显示 `official` 或 `daily_sum`。

**适用范围**：除南方电网覆盖省份（广东、广西、云南、贵州、海南）外的用户。

**支持架构**：
- `linux/amd64`：x86-64，使用 Chrome for Testing 120（已修复崩溃问题）
- `linux/arm64`：树莓派 3+、N1 盒子等，使用系统 Chromium

---

## 让修改真正生效的两种方式

### 方式 A：本地 Docker Compose（推荐先验证）

`docker-compose.yml` 已改为**从当前目录构建镜像**，不会再去拉别人的镜像：

```bash
docker compose up -d --build
docker compose logs -f
```

- 直接使用当前 `scripts/` 代码，包含本次 MQTT、峰谷、MySQL 修改
- 自动加载 `.env` 里的 MQTT/MySQL/HA 配置
- `network_mode: host`，可直接访问 MQTT broker 和 MySQL 所在主机
- 适合先验证 MQTT、峰谷推送和 MySQL，再决定是否切到 Add-on

> 如果旧版加载项/容器还在运行，先停掉，避免两个进程同时写 HA 实体和 MySQL。

### 方式 B：发布自己的 Add-on 镜像

1. 把代码推送到 `gradual-wu/sgcc_electricity_new` 的 `master` 分支
2. GitHub Actions 会构建并推送：
   - `ghcr.io/gradual-wu/sgcc_electricity-amd64:latest`
   - `ghcr.io/gradual-wu/sgcc_electricity-aarch64:latest`
3. **首次推送后，把这两个 GHCR 包设为 Public**：
   GitHub → 个人头像 → Packages → `sgcc_electricity-amd64` / `...-aarch64`
   → Package settings → Change visibility → Public。
   否则 HA 加载项拉取镜像会提示未授权。
4. 在 HA 里先停止旧加载项；如果之前添加过 `wuwweizn` 的仓库，先删除旧仓库。
   再添加仓库 `https://github.com/gradual-wu/sgcc_electricity_new`，刷新后
   更新/重新安装 **SGCC Electricity**。如果 HA 因同名 `slug` 不显示新仓库，
   先卸载旧加载项再安装（MySQL 数据不受影响，加载项 `/data` 缓存会重建）。
5. 只有新的镜像构建完成后，HA 更新加载项才会真正用上你的修改

如果 Actions 推送 GHCR 失败，检查仓库 **Settings → Actions → General →
Workflow permissions** 是否允许 read and write；或者在 Secrets 里创建 `CR_PAT`
（需要 `write:packages`），并把 `.github/workflows/docker-image.yml` 的密码改回
`secrets.CR_PAT`。

### 方式 C：让 HA 本地构建 Add-on（不用 GHCR）

如果不想配置 GHCR，也可以让 Supervisor 直接构建：

1. 删除 `config.yaml` 里的 `image:` 行
2. 把代码推送到 fork
3. 在 HA 添加仓库并安装/更新加载项，Supervisor 会用根目录 `Dockerfile` 构建

> 首次构建会下载 Chromium/Chrome，耗时较长，并且 HA 主机需要能访问外网。
> 构建失败时优先使用方式 A 或 B。

---

## 安装方式：HA Add-on（推荐）

### 1. 添加仓库

在 Home Assistant 中依次进入：**设置 → 加载项 → 加载项商店 → 右上角菜单 → 仓库**

添加以下地址：

```
https://github.com/gradual-wu/sgcc_electricity_new
```

### 2. 安装加载项

仓库添加成功后，在加载项商店搜索 **SGCC Electricity**，点击安装，等待镜像拉取完成（镜像来自
`ghcr.io/gradual-wu/sgcc_electricity-amd64` 或 `...-aarch64`）。

### 3. 获取大模型 API Key

本项目使用大模型自动解算腾讯验证码，需要配置 LLM API。

推荐使用**火山引擎豆包**（注册即赠免费额度，个人使用基本免费）：

1. 注册 [火山引擎](https://www.volcengine.com/) 并完成实名认证
2. 进入 [火山方舟控制台](https://console.volcengine.com/ark) → 在线推理 → 创建推理接入点
   - 选择模型：`Doubao-Seed-2.0-pro-260215`（或其他多模态视觉模型）
   - 记录生成的**接入点 ID**（格式：`ep-2025xxxxxx-xxxxx`）
3. 左侧菜单 → API Key 管理 → 创建并复制 API Key

也可使用其他兼容 OpenAI 接口的视觉模型（GPT-4o、Claude 等），自行配置 `LLM_BASE_URL` 和 `LLM_MODEL`。

### 4. 获取 HA 长期访问令牌

HA 界面 → 左下角个人头像 → 安全 → 长期访问令牌 → 创建令牌，复制保存。

### 5. 准备 MQTT 账号

安装并启动 **Mosquitto broker** 加载项。官方加载项默认拒绝匿名连接，需要在
HA 的「设置 → 人员 → 用户」中新建一个普通用户（例如 `sgcc_mqtt`），
或者在该加载项的 `logins` 配置里添加账号。把用户名/密码填入下面的
`MQTT_USERNAME` / `MQTT_PASSWORD`。

### 6. 配置加载项

在加载项的「配置」页面填写以下参数：

| 参数 | 说明 |
|------|------|
| `PHONE_NUMBER` | 国网登录手机号 |
| `PASSWORD` | 国网登录密码 |
| `HASS_URL` | HA 地址，如 `http://homeassistant.local:8123/` |
| `HASS_TOKEN` | HA 长期访问令牌（MQTT 接入后用于二维码通知和旧实体清理） |
| `MQTT_HOST` | Mosquitto broker 地址，HA Add-on 默认 `127.0.0.1` |
| `MQTT_PORT` | broker 端口，默认 `1883` |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | Mosquitto 认证账号；官方 Mosquitto 加载项使用 HA 用户认证 |
| `MQTT_DISCOVERY_PREFIX` | HA MQTT Discovery 前缀，默认 `homeassistant` |
| `MQTT_STATE_PREFIX` | 状态 topic 前缀，默认 `sgcc_electricity` |
| `DB_TYPE` | `NONE` / `SQLITE` / `MYSQL`，长期存储推荐 `MYSQL` |
| `MYSQL_HOST` / `MYSQL_PORT` | MySQL 地址和端口 |
| `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DATABASE` | MySQL 账号、密码、库名 |
| `DATA_RETENTION_DAYS` | 日数据和余额历史保留天数；`0` 表示永久保留（推荐） |
| `DAILY_FETCH_DAYS` | 每次拉取的日用电量天数，`7` 或 `30`，建议 `30` |
| `LLM_API_KEY` | 大模型 API Key |
| `LLM_BASE_URL` | 大模型接口地址（火山引擎留空使用默认值） |
| `LLM_MODEL` | 模型名称，如 `ep-2025xxxxxx-xxxxx`（火山引擎接入点 ID） |
| `JOB_START_TIME` | 每日执行时间，默认 `07:00`，每 12 小时执行一次 |
| `IGNORE_USER_ID` | 需要忽略的户号，多个用英文逗号分隔 |
| `BALANCE` | 余额低于此值时触发通知（元） |

> `config.yaml` 里保留的是通用默认值（空主机、空账号），避免把个人内网地址提交到
> 公开仓库。请按自己的环境填写 `DB_TYPE`、`MYSQL_*`、`MQTT_*`、`HASS_*`。

#### MQTT 认证说明

官方 **Mosquitto broker** 加载项默认拒绝匿名连接，所有客户端必须使用
Home Assistant 用户认证（不是加载项 `logins` 里的账号也可以）。有两种做法：

1. 在 HA 的「设置 → 人员 → 用户」中新建一个普通用户（例如 `sgcc_mqtt`），
   把用户名/密码填入 `MQTT_USERNAME` / `MQTT_PASSWORD`；
2. 在 Mosquitto 加载项配置的 `logins` 中添加账号，并填入同样的环境变量。

> 建议使用一个**专用普通用户**（例如 `sgcc_mqtt`），只用于 MQTT，避免把管理员账号
> 写进配置文件。用户名/密码只放在本机 `.env` 或加载项配置里，不要提交到 git。

#### 从旧版 REST 接入升级

1. **先停止旧版加载项/容器**，否则旧版会不断重新创建 REST 状态，和新版 MQTT 实体冲突；
2. 更新镜像/加载项，在配置中填写 `MQTT_*` 以及 `DB_TYPE=MYSQL`、`MYSQL_*`；
3. 启动新版。首次运行会：
   - 删除旧版 REST 创建的同名状态（`sensor.*_后4位`），避免 MQTT 实体被改名成 `*_2`；
   - 通过 MQTT Discovery 创建 15 个实体，原有实体 ID 和 `state_class` 保持不变；
   - 把旧版 `daily{户号}`、`data{户号}` 表迁移到新的业务表（旧表保留）。
4. 在 HA「设置 → 设备与服务 → MQTT → 设备」中确认 `国网电费 ...` 设备及其实体。

> 旧版 REST 实体的长期统计（长期统计 ID 就是原 entity_id）会继续沿用，
> 不需要删除历史数据。

### 7. 启动加载项

保存配置后，启动加载项，查看日志确认运行正常。正常运行的日志类似：

```
Google Chrome for Testing 120.0.6099.109
ChromeDriver 120.0.6099.109
浏览器驱动已初始化。
打开登录页面: https://95598.cn/osgweb/login
通过点击验证码登录成功。
用户 [xxxxxxx] 数据获取完成: 余额=xxx元, 最近日用电=xxx度
```

---

## 二维码登录说明

若遭遇腾讯风控拦截（RK001 错误），加载项会**自动切换二维码登录模式**：

1. 二维码图片保存至 `/config/www/sgcc_login_qr.png`
2. HA 界面左下角铃铛出现红点通知，标题为「⚡ 国网需要扫码登录」
3. 点开通知，用**国家电网 App** 扫描二维码即可
4. 加载项默认等待 **180 秒**，请在此时间内完成扫码

> 需要正确填写 `HASS_URL` / `HASS_TOKEN`。加载项配置中 `LOGIN_FALLBACK` 默认
> `qrcode`，`QR_CODE_LOGIN_WAIT_COUNT=18`（18 × 10 秒 = 180 秒）。
> 图片写入 `/config/www` 失败时，HA 通知仍会发送文字提示，二维码同时保存在
> `/data/login_qr_code.png`。
>
> 二维码 Session 通常持续 7～30 天，无需频繁扫码。

---

## HA 数据展示

传感器通过 **MQTT Discovery** 自动创建，不需要再在 `configuration.yaml` 里写
template sensor。程序启动后，HA 的「设置 → 设备与服务 → MQTT」下会出现
`国网电费 家庭用电` 设备，15 个实体全部挂在设备下。

实体 ID 与旧版一致，例如：

```
sensor.last_electricity_usage_xxxx
sensor.last_valley_electricity_usage_xxxx
sensor.month_peak_usage_xxxx
sensor.electricity_charge_balance_xxxx
```

MQTT topic 规则：

```
homeassistant/sensor/<sensor_key>_<户号后4位>/config     # 发现配置（retained）
sgcc_electricity/<完整户号>/<sensor_key>/state              # 状态 + 属性（retained）
sgcc_electricity/status                                     # 在线状态（LWT）
```

状态属性里会带上 `last_reset`、`period`、`source`、`last_daily_date` 等信息，
方便在自动化里判断数据属于哪一天/哪个月。

---

## 数据存储（MySQL）

`DB_TYPE=MYSQL` 时使用以下业务表（SQLite 使用同样的结构）：

| 表 | 说明 |
|------|------|
| `sg_users` | 户号、登录账号、备注名、首次/最近出现时间 |
| `sg_daily_usage` | 日用电量 + 谷/平/峰/尖分时 + 来源 |
| `sg_monthly_usage` | 月度用电量/电费 + 分时，区分 `official`（官方账单）和 `calculated`（页面计算） |
| `sg_yearly_usage` | 年度用电量/电费 |
| `sg_balance_log` | 余额/应交金额历史 |

设计要点：

- **按业务对象建表**，不再为每个户号创建 `daily{户号}` / `data{户号}` 表；
- 金额使用 `DECIMAL(12,2)`，电量使用 `DECIMAL(12,3)`；
- 主键为 `(user_id, 日期/月份/年份)`，重复抓取自动 upsert；
- 官方账单优先级高于页面计算值，后续计算值不会覆盖官方账单；
- `DATA_RETENTION_DAYS=0` 表示**永久保留**；大于 0 时只清理日数据和余额历史，
  月/年数据始终保留；
- 首次连接时会自动把旧版 `daily{户号}`、`data{户号}` 表迁移到新表，
  旧表原样保留，可随时回滚。

旧表迁移示例（9 个月度、1 个年度、7 天日数据、1 条余额）：

```
旧表迁移完成: 日数据 7 条，月数据 9 条，年数据 1 条，余额 1 条。
```

---

## 模拟数据测试（不访问国家电网）

想先验证 MQTT/HA 是否调通，可以运行模拟脚本。它使用模拟户号
`9999999999999`，不会请求国网，也不会覆盖真实户号的实体：

```bash
python3 scripts/mock_publish.py
```

脚本会推送余额、日用电、月度/年度数据以及谷/平/峰/尖分时数据。运行后在 HA 中
检查 `sensor.last_electricity_usage_9999` 等实体是否出现、数值是否正确。

验证完成后删除模拟实体：

```bash
python3 scripts/mock_publish.py --cleanup
```

> 模拟脚本退出时不会把 MQTT 在线状态改为 offline，方便你在 HA 里查看结果。
> 真实程序退出时会通过 LWT/offline 标记实体不可用。

运行单元测试：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest -q
```

---

结合 [mini-graph-card](https://github.com/kalkih/mini-graph-card) 可实现可视化效果：

```yaml
type: vertical-stack
cards:
  - type: custom:mini-graph-card
    entities:
      - entity: sensor.last_electricity_usage_xxxx
        name: 国网每日用电量
        aggregate_func: first
        show_state: true
        show_points: true
        icon: mdi:lightning-bolt-outline
      - entity: sensor.electricity_charge_balance_xxxx
        name: 电费余额
        aggregate_func: first
        show_state: true
        show_points: true
        color: "#e74c3c"
        icon: mdi:cash
        y_axis: secondary
    group_by: date
    hour24: true
    hours_to_show: 240
```

---

## 更新日志

- **2026-10-21**：接入方式改为 MQTT Discovery；新增谷/平/峰/尖分时实体；
  MySQL 改为业务表结构并默认永久保留；新增模拟数据测试脚本。
- **2026-07-06**：Fork 自 ARC-MX 分支，针对 HA Add-on 场景专项修复：
  - 固定 Chrome for Testing 120.0.6099.109（解决 Chromium 150 在 HA OS 中崩溃）
  - CDP 反指纹注入（解决 RK001 腾讯风控拦截）
  - 验证码隐藏元素检测修复
  - 二维码登录通知改用 HA 持久通知（告别 PushPlus 实名认证）
  - GitHub Actions 自动构建并推送至 ghcr.io
- **2025-05-20**（原仓库）：弃用 CloakBrowser，改用原生 Selenium + Chrome 反检测方案
- **2025-05-01**（原仓库）：验证码识别升级为大模型（LLM）视觉方案
- **2025-01-05**（原仓库）：新增 HA Add-on 部署方式
- **2024-12-10**（原仓库）：新增 `IGNORE_USER_ID` 忽略指定户号
- **2024-07-05**（原仓库）：新增余额不足提醒功能
- **2024-07-03**（原仓库）：新增每天执行两次，支持 7/30 天历史数据
- **2024-06-13**（原仓库）：SQLite 替换 MongoDB

---

## 致谢

- 原始作者：[louisslee/sgcc_electricity](https://github.com/louisslee/sgcc_electricity)
- 主要维护：[ARC-MX/sgcc_electricity_new](https://github.com/ARC-MX/sgcc_electricity_new)
- 本分支维护：[gradual-wu/sgcc_electricity_new](https://github.com/gradual-wu/sgcc_electricity_new)
